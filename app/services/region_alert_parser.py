"""
Best-effort, zero-API-key detector of per-oblast air-raid alert
announcements inside Telegram messages the app *already* monitors.

Why this exists: ``api.alerts.in.ua`` (see ``app/config.py``) is free but
gated behind an approval-request token, which is not always obtainable.
Without a token, ``AlertService`` had no other automatic way to ever set
a region's ``is_active`` flag -- only that API and a manual map tap could
do it -- so "Активних областей" was structurally guaranteed to stay 0
forever for a user without a token, no matter how real the situation was.

Many public Ukrainian "air alert" aggregator channels/bots post in a very
regular, line-per-oblast format that names the affected oblast directly,
e.g.::

    🔴 Дніпропетровська область: Повітряна тривога
    🟢 Харківська область - відбій
    Київщина: оголошено тривогу

This module recognizes that pattern in text the app already receives
from the user's monitored channels -- no network call, no token, no new
external dependency. It is intentionally conservative: a line must
contain BOTH a recognizable oblast name AND a clear тривога/відбій
marker to produce a result; anything ambiguous is skipped rather than
guessed. This is a second, independent, free reading of the same stream
-- it does not replace or remove alerts.in.ua support, which stays
available (and takes priority when configured, see ``alert_service.py``)
for users who do have a token.

Limitation, stated plainly: this only works for regions actually
mentioned by name in a monitored channel's messages. If none of the
user's channels post oblast-level text, this source simply reports
nothing for that oblast, same as if alerts.in.ua were never configured
-- it is not a substitute for a channel that names regions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.models.alert_models import Region

_END_MARKER = "відбій"
_START_MARKER = "тривог"  # stem: тривога/тривогу/тривоги/оголошено тривогу


@dataclass(frozen=True, slots=True)
class _RegionPattern:
    """One region and the compiled pattern that identifies it in text."""

    region: Region
    pattern: re.Pattern[str]


def _stems_to_pattern(stems: tuple[str, ...]) -> re.Pattern[str]:
    """Compile a case-insensitive "any of these stems, as a word" pattern."""
    alternation = "|".join(re.escape(stem) for stem in stems)
    return re.compile(rf"\b(?:{alternation})", re.IGNORECASE)


# Region -> word stems that identify it: the official adjective stem
# (e.g. "черкаськ" matches Черкаська/Черкаської/...) plus, where it is a
# standard and unambiguous part of everyday Ukrainian (not invented here,
# just the ordinary colloquial oblast name every speaker uses), the
# common "-щина"/historical short form. м. Київ and м. Севастополь use
# their bare city names, since that's simply what those two are called.
#
# Kyiv needs special handling: "київщин"/"київська обл" -> the OBLAST,
# but the bare city name "київ" on its own -> the CITY. Order in
# _REGION_PATTERNS matters (checked in order, first match wins per line),
# so the oblast pattern is listed before the city pattern.
_REGION_STEMS: tuple[tuple[Region, tuple[str, ...]], ...] = (
    (Region.KYIV_OBLAST, ("київщин", "київська обл", "київській обл", "київську обл")),
    (Region.KYIV_CITY, ("м. київ", "місто київ", "київ", "києва", "києву", "києвом", "києві")),
    (Region.CHERKASY, ("черкаськ", "черкащин")),
    (Region.CHERNIHIV, ("чернігівськ", "чернігівщин")),
    (Region.CHERNIVTSI, ("чернівецьк",)),
    (Region.CRIMEA, ("крим", "автономна республіка крим")),
    (Region.DNIPRO, ("дніпропетровськ", "дніпропетровщин")),
    (Region.DONETSK, ("донецьк", "донеччин")),
    (Region.IVANO_FRANKIVSK, ("івано-франківськ",)),
    (Region.KHARKIV, ("харківськ", "харківщин")),
    (Region.KHERSON, ("херсонськ", "херсонщин")),
    (Region.KHMELNYTSKYI, ("хмельницьк",)),
    (Region.KIROVOHRAD, ("кіровоградськ", "кіровоградщин")),
    (Region.LUHANSK, ("луганськ", "луганщин")),
    (Region.LVIV, ("львівськ", "львівщин")),
    (Region.MYKOLAIV, ("миколаївськ", "миколаївщин")),
    (Region.ODESA, ("одеськ", "одещин")),
    (Region.POLTAVA, ("полтавськ", "полтавщин")),
    (Region.RIVNE, ("рівненськ",)),
    (Region.SEVASTOPOL, ("севастопол",)),
    (Region.SUMY, ("сумськ", "сумщин")),
    (Region.TERNOPIL, ("тернопільськ",)),
    (Region.VINNYTSIA, ("вінницьк",)),
    (Region.VOLYN, ("волинськ", "волинщин")),
    (Region.ZAKARPATTIA, ("закарпатськ", "закарпатт")),
    (Region.ZAPORIZHZHIA, ("запорізьк",)),
    (Region.ZHYTOMYR, ("житомирськ", "житомирщин")),
)

_REGION_PATTERNS: tuple[_RegionPattern, ...] = tuple(
    _RegionPattern(region=region, pattern=_stems_to_pattern(stems))
    for region, stems in _REGION_STEMS
)


def find_mentioned_regions(text: str) -> list[Region]:
    """Return every oblast explicitly named anywhere in ``text``, in the
    order this module checks for them (first-match-per-segment isn't
    applied here -- a caller like the destroyed-target matcher genuinely
    wants every region a message names, not just one per line).

    Reuses the exact same name/stem table as :func:`parse_region_alerts`
    (one source of truth for "how do we recognize an oblast's name in
    Ukrainian text", not two copies of it drifting apart) but without
    requiring a тривога/відбій marker -- useful anywhere a caller just
    needs to know which oblast(s) a message is talking about.
    """
    lowered = text.lower()
    found: list[Region] = []
    for region_pattern in _REGION_PATTERNS:
        if region_pattern.pattern.search(lowered):
            found.append(region_pattern.region)
    return found


def parse_region_alerts(text: str) -> list[tuple[Region, bool]]:
    """Detect explicit per-oblast alert start/end lines in ``text``.

    Splits the message into lines/segments (aggregator channels commonly
    post one oblast per line in a multi-region update) and, for each
    segment, looks for a recognizable oblast name together with a
    тривога/відбій marker. Returns one ``(region, is_active)`` pair per
    segment where both were found unambiguously; segments with neither,
    or with an unrecognized region name, contribute nothing. Never
    raises, never guesses a region that isn't actually named.
    """
    results: list[tuple[Region, bool]] = []
    segments = re.split(r"[\n\r;|•]+", text)

    for segment in segments:
        segment_lower = segment.lower()
        if _END_MARKER not in segment_lower and _START_MARKER not in segment_lower:
            continue

        # "Відбій тривоги" contains both stems -- an ended alert always
        # mentions the тривога it ended, so a literal "відбій" anywhere
        # in the segment means "end", regardless of "тривог" also
        # matching. Only fall back to "start" when there is no end
        # marker at all.
        is_active = _END_MARKER not in segment_lower

        for region_pattern in _REGION_PATTERNS:
            if region_pattern.pattern.search(segment_lower):
                results.append((region_pattern.region, is_active))
                break  # one region per segment -- avoids double-counting
                # a region name that incidentally also matches a looser
                # pattern later in the table (e.g. a city name inside an
                # oblast's own text).

    return results
