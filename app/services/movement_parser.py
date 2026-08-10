"""
Rule-based extraction of "Рух загроз" (threat movement) entries from raw
Telegram message text.

Deliberately does not use any AI/LLM, does not predict a route, and does
not guess a target: it only recognizes a fixed set of Ukrainian phrasings
("з <місто> у напрямку <місто>", "курсом на <місто>", ...) that explicitly
name a place, and only accepts a place name if it resolves against the
known city gazetteer (:mod:`app.data.ukraine_cities`). If a message
doesn't explicitly name a recognized place, that field is left ``None``
and nothing is invented.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from app.data.ukraine_cities import resolve_city, resolve_city_name
from app.models.alert_models import Region
from app.models.movement_models import ThreatMovement, ThreatType
from app.services.region_alert_parser import find_mentioned_regions
from app.ukraine_geo import project_lat_lon, region_at_point


@dataclass(frozen=True, slots=True)
class _ThreatKeyword:
    """A single keyword (or short phrase) mapped to a threat type."""

    pattern: str
    threat_type: ThreatType


#: Keyword -> threat type. Order matters: more specific terms are checked
#: first so e.g. "шахед" wins over a generic "бпла" mention in the same
#: message. Matching is case-insensitive.
THREAT_KEYWORDS: tuple[_ThreatKeyword, ...] = (
    _ThreatKeyword("шахед", ThreatType.SHAHED),
    _ThreatKeyword("shahed", ThreatType.SHAHED),
    _ThreatKeyword("камікадзе", ThreatType.SHAHED),
    _ThreatKeyword("герань", ThreatType.SHAHED),
    _ThreatKeyword("ballistic missile", ThreatType.MISSILE),
    _ThreatKeyword("cruise missile", ThreatType.MISSILE),
    _ThreatKeyword("балістик", ThreatType.MISSILE),
    _ThreatKeyword("кинджал", ThreatType.MISSILE),
    _ThreatKeyword("калібр", ThreatType.MISSILE),
    _ThreatKeyword("іскандер", ThreatType.MISSILE),
    _ThreatKeyword("х-101", ThreatType.MISSILE),
    _ThreatKeyword("х-555", ThreatType.MISSILE),
    _ThreatKeyword("ракет", ThreatType.MISSILE),  # stem: matches ракета/ракети/ракетний/ракетного/...
    _ThreatKeyword("missile", ThreatType.MISSILE),
    _ThreatKeyword("міг-31к", ThreatType.AIRCRAFT),
    _ThreatKeyword("ту-95", ThreatType.AIRCRAFT),
    _ThreatKeyword("зліт", ThreatType.AIRCRAFT),
    _ThreatKeyword("aircraft", ThreatType.AIRCRAFT),
    _ThreatKeyword("бпла", ThreatType.UAV),
    _ThreatKeyword("безпілотник", ThreatType.UAV),
    _ThreatKeyword("дрон", ThreatType.UAV),
    _ThreatKeyword("uav", ThreatType.UAV),
    _ThreatKeyword("drone", ThreatType.UAV),
)

#: A single word in either script a place name might be written in
#: (Cyrillic or Latin), including the punctuation Ukrainian/English place
#: names actually use (apostrophe, hyphen).
_WORD = r"[A-Za-zА-ЯҐЄІЇа-яґєії'’\-]+"

#: English connector/direction words that must NOT themselves be swallowed
#: into a place-name capture (see ``_PLACE_TOKEN``'s second-word guard
#: below) -- e.g. so "Boryspil to Kyiv" doesn't capture "Boryspil to" as
#: one place. Deliberately just the small, closed set of words this
#: module's own patterns use as connectors, not a general stop-word list.
_CONNECTOR_WORDS = (
    "to", "towards", "toward", "heading", "moving", "course",
    "from", "direction", "of", "the", "in",
    # Destroy/interception status words -- see _STATUS_PHRASES below.
    # Without these, a trailing "Voronkiv збили" / "Voronkiv shot down"
    # would get captured as one 2-word place-name candidate (the same
    # way "Bila Tserkva" legitimately is), fail to resolve as a whole,
    # and silently drop the destination this message's own route/status
    # actually stated -- with no origin present to fall back to (a bare
    # "Shahed" isn't a place), that left the message with no location
    # at all.
    "shot", "down", "destroyed", "intercepted", "eliminated", "neutralized",
    "збили", "збито", "збила", "збив",
    "знищено", "знищили", "знищила", "знищив",
    "перехоплено", "перехопили",
    "нейтралізовано", "нейтралізували",
    "ліквідовано",
)
_CONNECTOR_ALT = "|".join(_CONNECTOR_WORDS)

#: One place name, up to two words (covers "Bila Tserkva", "Nova Kakhovka",
#: "Біла Церква", ...), stopping before a connector word so it never
#: swallows the phrasing around it. This is a *candidate* extractor only
#: -- every match still has to resolve via ``resolve_city`` to be
#: accepted, so an accidental match against ordinary text is harmless.
_PLACE_TOKEN = rf"{_WORD}(?:\s+(?!(?:{_CONNECTOR_ALT})\b){_WORD})?"

#: Movement-direction connector phrases (English), checked as a single
#: alternation rather than one pattern per phrase -- covers "->"/"→"/"➜"
#: style arrows, plus every worded phrasing requested: "to", "towards",
#: "heading to", "moving to", "moving towards", "course to", "in the
#: direction of". General on purpose: any new phrase can be added to this
#: one alternation instead of hardcoding a whole new pattern per format.
_ARROW_SYMBOLS = r"(?:→|->|➜|➞|=>)"
_EN_DIRECTION_PHRASE = (
    r"(?:heading to|heading towards|moving to|moving towards|course to|"
    r"in the direction of|towards|toward|to)"
)

#: Patterns whose single capture group is an ORIGIN place name, checked in
#: order. Each is tried against the raw text; the first one that both
#: matches AND resolves via ``resolve_city`` wins.
_ORIGIN_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"з\s+([А-ЯҐЄІЇа-яґєії'\-]+)\s+у\s+напрямку", re.IGNORECASE),
    re.compile(r"з\s+([А-ЯҐЄІЇа-яґєії'\-]+)\s+в\s+напрямку", re.IGNORECASE),
    re.compile(r"з\s+району\s+([А-ЯҐЄІЇа-яґєії'\-]+)", re.IGNORECASE),
    re.compile(r"з\s+([А-ЯҐЄІЇа-яґєії'\-]+)\s+на\s+", re.IGNORECASE),
    re.compile(r"з\s+([А-ЯҐЄІЇа-яґєії'\-]+)", re.IGNORECASE),
    re.compile(rf"from\s+({_PLACE_TOKEN})", re.IGNORECASE),
)

#: Patterns whose single capture group is a DESTINATION place name.
_DESTINATION_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"у\s+напрямку\s+(?:до\s+)?([А-ЯҐЄІЇа-яґєії'\-]+)", re.IGNORECASE),
    re.compile(r"в\s+напрямку\s+(?:до\s+)?([А-ЯҐЄІЇа-яґєії'\-]+)", re.IGNORECASE),
    re.compile(r"напрямок[уі]?\s+(?:на\s+)?([А-ЯҐЄІЇа-яґєії'\-]+)", re.IGNORECASE),
    re.compile(r"курс(?:ом)?\s+на\s+([А-ЯҐЄІЇа-яґєії'\-]+)", re.IGNORECASE),
    re.compile(r"на\s+([А-ЯҐЄІЇа-яґєії'\-]+)", re.IGNORECASE),
    re.compile(rf"{_ARROW_SYMBOLS}\s*({_PLACE_TOKEN})", re.IGNORECASE),
    re.compile(rf"{_EN_DIRECTION_PHRASE}\s+({_PLACE_TOKEN})", re.IGNORECASE),
)

#: Patterns whose TWO capture groups are (origin, destination) together --
#: tried before the independent origin/destination extraction above so an
#: explicit "X → Y" / "from X to Y" route is read as one coherent pair
#: rather than two separately-guessed places. General connector
#: alternation (arrow symbols, "from ... to/towards ...", or a bare
#: "X to Y") -- not one pattern per specific city pair.
#: Wider word-run (up to 5 words) used only for the arrow pattern's two
#: sides -- an arrow has no leading word like "from"/"на" to bound where
#: the place name starts, so a preceding threat-name word ("Shahed
#: Boryspil → Kyiv") would otherwise get pulled into the same capture.
#: ``_extract_place_pair`` resolves the actual place from within this
#: wider span (see ``_best_resolvable_span``) rather than relying on the
#: regex alone to find the exact boundary.
_WORDS_RUN = rf"(?:{_WORD}(?:\s+{_WORD}){{0,4}})"

_ROUTE_PAIR_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(rf"({_WORDS_RUN})\s*{_ARROW_SYMBOLS}\s*({_WORDS_RUN})", re.IGNORECASE),
    re.compile(rf"from\s+({_PLACE_TOKEN})\s+(?:to|towards|toward)\s+({_PLACE_TOKEN})", re.IGNORECASE),
)


def _best_resolvable_span(words: list[str], from_end: bool) -> Optional[str]:
    """Return the longest (2-word, then 1-word) resolvable place name
    found at the start or end of ``words``, or ``None``.

    Tries 2 words first so a multi-word place name ("Bila Tserkva") is
    preferred over accidentally matching just one of its words, then
    falls back to 1 word. Never invents a place -- only ever returns a
    span that ``resolve_city`` actually resolves.
    """
    for length in (2, 1):
        if len(words) < length:
            continue
        candidate_words = words[-length:] if from_end else words[:length]
        candidate = " ".join(candidate_words)
        if resolve_city(candidate) is not None:
            return candidate
    return None


#: Phrases reporting a target was shot down/neutralized, as opposed to a
#: message reporting a NEW threat in flight. Deliberately conservative --
#: stems that unambiguously mean "this specific aerial target is gone",
#: not general wartime destruction language (which would risk false
#: positives against unrelated news). Case-insensitive, matched with
#: THREAT_KEYWORDS the same way -- see ``parse_destroyed_report``.
_DESTROYED_KEYWORDS: tuple[str, ...] = (
    "збито",
    "збили",
    "збила",
    "збив",
    "знищено",
    "знищили",
    "нейтралізовано",
    "нейтралізували",
    "ліквідовано",
    "shot down",
    "destroyed",
    "intercepted",
    "eliminated",
    "neutralized",
)


#: Patterns for "where" a destroyed-target report is talking about --
#: distinct from _ORIGIN_PATTERNS/_DESTINATION_PATTERNS above, which are
#: tuned for movement-direction phrasing ("з X у напрямку Y") that a
#: destroy report doesn't use. These cover the common "over/near/in the
#: area of <place>" phrasings destroy reports actually use.
_DESTROYED_LOCATION_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"над\s+([А-ЯҐЄІЇа-яґєії'\-]+)", re.IGNORECASE),
    re.compile(r"поблизу\s+([А-ЯҐЄІЇа-яґєії'\-]+)", re.IGNORECASE),
    re.compile(r"в\s+районі\s+([А-ЯҐЄІЇа-яґєії'\-]+)", re.IGNORECASE),
    re.compile(r"у\s+районі\s+([А-ЯҐЄІЇа-яґєії'\-]+)", re.IGNORECASE),
    re.compile(rf"over\s+({_PLACE_TOKEN})", re.IGNORECASE),
    re.compile(rf"near\s+({_PLACE_TOKEN})", re.IGNORECASE),
    re.compile(rf"in the area of\s+({_PLACE_TOKEN})", re.IGNORECASE),
)


#: Explicit numeric quantity immediately before/after a threat keyword,
#: e.g. "2 шахеди", "3х БПЛА", "шахед x2". Deliberately narrow (digits
#: only, tight to the keyword) rather than scanning the whole message for
#: any number, so an unrelated figure elsewhere in the text (a channel
#: post number, a time, a casualty count) is never mistaken for a threat
#: quantity.
_GROUP_COUNT_NUMBER_PATTERN = re.compile(
    r"(\d{1,2})\s*(?:х|x|×)?\s*(?:" + "|".join(re.escape(k.pattern) for k in THREAT_KEYWORDS) + r")"
    r"|(?:" + "|".join(re.escape(k.pattern) for k in THREAT_KEYWORDS) + r")\s*(?:х|x|×)?\s*(\d{1,2})",
    re.IGNORECASE,
)
#: Phrasing that clearly states "more than one" without giving an exact
#: number ("групою шахедів", "кілька БПЛА"). Treated as a floor estimate
#: of 2 -- "a group" unambiguously means more than one, but this module
#: never invents a precise count beyond that when none was actually
#: stated (see ``_detect_group_count``'s docstring).
_VAGUE_GROUP_PHRASES: tuple[str, ...] = ("групою", "групу", "групи", "декілька", "кілька")


def _detect_group_count(text: str) -> int:
    """Return the number of targets this message reports, or 1 if unstated.

    Prefers an explicit digit tightly bound to a threat keyword (e.g. "2
    шахеди"). Falls back to a floor estimate of 2 for phrasing that
    clearly states a group/several without a number ("групою шахедів") --
    documented as a floor, not a guessed exact count: this module still
    never invents a specific number beyond what "more than one" means.
    Returns 1 (ordinary single-target default) for everything else,
    keeping every existing single-target message's behavior unchanged.
    """
    match = _GROUP_COUNT_NUMBER_PATTERN.search(text)
    if match:
        digits = match.group(1) or match.group(2)
        count = int(digits)
        if 2 <= count <= 50:  # sanity bound -- reject an obviously-unrelated number
            return count
    lowered = text.lower()
    if any(phrase in lowered for phrase in _VAGUE_GROUP_PHRASES):
        return 2
    return 1


def _detect_threat_type(text: str) -> Optional[ThreatType]:
    """Return the first matching threat type, or ``None`` if not threat-related."""
    lowered = text.lower()
    for keyword in THREAT_KEYWORDS:
        if keyword.pattern in lowered:
            return keyword.threat_type
    return None


def _extract_place_pair(text: str, patterns: tuple[re.Pattern, ...]) -> Optional[tuple[str, str]]:
    """Try each (origin, destination) pair pattern in order; return the
    first pair where BOTH captured names resolve in the gazetteer.

    Same "never guess" policy as ``_extract_place``: a pattern matching
    text that doesn't name two real, resolvable places is not accepted,
    so an unrelated arrow/word never fabricates a fake route.
    """
    for pattern in patterns:
        match = pattern.search(text)
        if not match:
            continue
        origin_words = match.group(1).strip("'\"., ").split()
        destination_words = match.group(2).strip("'\"., ").split()
        origin_candidate = _best_resolvable_span(origin_words, from_end=True)
        destination_candidate = _best_resolvable_span(destination_words, from_end=False)
        if origin_candidate is not None and destination_candidate is not None:
            return origin_candidate, destination_candidate
    return None


#: Phrases stating what happened to a target, mapped to one canonical
#: Ukrainian display label. Longer/more specific phrases are listed
#: first within each language so e.g. "shot down" (two words) is matched
#: as a whole before any shorter, coincidental overlap. This list is
#: intentionally separate from ``_DESTROYED_KEYWORDS`` above: that one
#: means "this message is ONLY reporting an existing target is gone, no
#: fresh route" (see ``parse_destroyed_report``); this one means "this
#: specific route/threat report also states an outcome for it", used by
#: ``parse_message`` to set ``ThreatMovement.status_label`` without
#: discarding the route itself.
_STATUS_PHRASES: tuple[tuple[str, str], ...] = (
    ("shot down", "Збито"),
    ("intercepted", "Перехоплено"),
    ("neutralized", "Нейтралізовано"),
    ("eliminated", "Знищено"),
    ("destroyed", "Знищено"),
    ("збито", "Збито"),
    ("збили", "Збито"),
    ("збила", "Збито"),
    ("збив", "Збито"),
    ("знищено", "Знищено"),
    ("знищили", "Знищено"),
    ("перехоплено", "Перехоплено"),
    ("нейтралізовано", "Нейтралізовано"),
    ("нейтралізували", "Нейтралізовано"),
    ("ліквідовано", "Знищено"),
)


def _detect_status(text: str) -> Optional[str]:
    """Return the canonical outcome label if ``text`` states one, else ``None``.

    Deliberately independent of origin/destination extraction -- a
    trailing "shot down"/"збито" is recognized regardless of where in
    the message it appears, and never changes how the route itself is
    parsed (see ``_CONNECTOR_WORDS``/``_PLACE_TOKEN``, which already
    stop a place capture before these words in practice since they
    aren't in that connector list, but the two concerns are checked
    fully separately here for clarity).
    """
    lowered = text.lower()
    for phrase, label in _STATUS_PHRASES:
        if phrase in lowered:
            return label
    return None


def _extract_place(text: str, patterns: tuple[re.Pattern, ...]) -> Optional[str]:
    """Try each pattern in order; return the first captured name that
    resolves to a real place in the gazetteer. Returns ``None`` if no
    pattern matches or no match resolves -- never guesses.
    """
    for pattern in patterns:
        match = pattern.search(text)
        if not match:
            continue
        candidate = match.group(1).strip("'\"., ")
        if resolve_city(candidate) is not None:
            return candidate
    return None


def parse_message(
    text: str,
    channel_username: str,
    received_at: datetime,
) -> Optional[ThreatMovement]:
    """Extract a :class:`ThreatMovement` from one Telegram message.

    Returns ``None`` only when the message names neither a recognized
    threat keyword NOR any resolvable place/route -- i.e. it's just
    unrelated chatter, not a movement report. A message with an explicit
    route or destination but no named threat word (e.g. a bare
    "Brovary → Boryspil") is still tracked, as ``ThreatType.UNKNOWN``,
    rather than dropped. Origin/destination are left ``None`` whenever
    the text doesn't explicitly name a place this module can resolve --
    this function never infers a location that isn't stated.
    """
    if not text or not text.strip():
        return None

    threat_type = _detect_threat_type(text)

    route_pair = _extract_place_pair(text, _ROUTE_PAIR_PATTERNS)
    if route_pair is not None:
        origin_name, destination_name = route_pair
    else:
        origin_name = _extract_place(text, _ORIGIN_PATTERNS)
        destination_name = _extract_place(text, _DESTINATION_PATTERNS)

    if threat_type is None:
        # No named threat keyword -- only worth tracking if the message
        # still explicitly states a route/destination/origin place; a
        # message with neither a threat word nor any resolvable place is
        # just unrelated chatter, not a movement report.
        if origin_name is None and destination_name is None:
            return None
        threat_type = ThreatType.UNKNOWN

    # A single "з X" match can occasionally also satisfy a weak "на Y"
    # pattern on the same word; guard against origin and destination
    # accidentally resolving to the exact same place.
    if origin_name and destination_name and origin_name.strip().lower() == destination_name.strip().lower():
        destination_name = None

    origin_point = None
    origin_region = None
    origin_settlement = None
    if origin_name is not None:
        coords = resolve_city(origin_name)
        if coords is not None:
            origin_point = project_lat_lon(*coords)
            origin_region = region_at_point(origin_point)
        origin_settlement = resolve_city_name(origin_name)

    destination_point = None
    destination_region = None
    destination_settlement = None
    if destination_name is not None:
        coords = resolve_city(destination_name)
        if coords is not None:
            destination_point = project_lat_lon(*coords)
            destination_region = region_at_point(destination_point)
        destination_settlement = resolve_city_name(destination_name)

    matched_keywords = [k.pattern for k in THREAT_KEYWORDS if k.pattern in text.lower()]
    group_count = _detect_group_count(text)
    status_label = _detect_status(text)

    movement_id = hashlib.sha1(
        f"{channel_username}|{received_at.isoformat()}|{text}".encode("utf-8")
    ).hexdigest()[:16]

    return ThreatMovement(
        id=movement_id,
        threat_type=threat_type,
        channel_username=channel_username,
        received_at=received_at,
        text=text,
        origin_name=origin_name,
        destination_name=destination_name,
        origin_point=origin_point,
        destination_point=destination_point,
        matched_keywords=matched_keywords,
        group_count=group_count,
        origin_region=origin_region,
        destination_region=destination_region,
        origin_settlement=origin_settlement,
        destination_settlement=destination_settlement,
        status_label=status_label,
    )


@dataclass(frozen=True, slots=True)
class DestroyedReport:
    """A message reporting that a tracked aerial target was shot down/
    neutralized, as opposed to a new threat entering the map.

    ``threat_type`` is ``None`` when the message doesn't name a specific
    type (e.g. "Збито повітряну ціль над Полтавщиною") -- callers should
    then match against any tracked movement in ``regions``, not just one
    type. ``regions`` is never empty (see ``parse_destroyed_report``).
    """

    threat_type: Optional[ThreatType]
    regions: tuple[Region, ...]
    text: str


def _detect_destroyed_regions(text: str) -> tuple[Region, ...]:
    """Regions a destroyed-target report is talking about.

    Prefers an oblast explicitly named in the text (most common phrasing:
    "...над Одеською областю"/"...на Харківщині"). Falls back to
    resolving a named settlement (same gazetteer/patterns used for
    origin/destination elsewhere in this module) to the oblast it
    physically falls in, for reports that name a city instead
    ("Збито шахед над Кременчуком").
    """
    regions = tuple(find_mentioned_regions(text))
    if regions:
        return regions

    place_name = (
        _extract_place(text, _DESTROYED_LOCATION_PATTERNS)
        or _extract_place(text, _ORIGIN_PATTERNS)
        or _extract_place(text, _DESTINATION_PATTERNS)
    )
    if place_name is None:
        return ()
    coords = resolve_city(place_name)
    if coords is None:
        return ()
    region = region_at_point(project_lat_lon(*coords))
    return (region,) if region is not None else ()


def movement_matches_destroyed_report(movement: ThreatMovement, report: DestroyedReport) -> bool:
    """True if ``movement`` is plausibly the target ``report`` is about.

    Conservative by design, matching this module's "never guess" policy:
    requires the movement to have at least one extracted location (a
    movement with neither an origin nor a destination has nothing to
    correlate against, so it's left to expire via the existing
    time-based prune rather than removed on a guess), and -- when the
    report names a specific threat type -- requires the same type.
    Location match is by oblast: either of the movement's origin/
    destination points falling in any of the report's named regions.
    """
    if report.threat_type is not None and movement.threat_type != report.threat_type:
        return False
    if not movement.has_any_location:
        return False
    movement_regions = {
        region
        for point in (movement.origin_point, movement.destination_point)
        if point is not None
        for region in (region_at_point(point),)
        if region is not None
    }
    return bool(movement_regions & set(report.regions))


def movement_matches_route_report(tracked: ThreatMovement, reported: ThreatMovement) -> bool:
    """True if ``tracked`` is plausibly the same target ``reported`` describes.

    Used when a destroy/intercept message ALSO states its own explicit
    route (e.g. "Шахед Бориспіль → Вороньків збили") -- more precise
    than ``movement_matches_destroyed_report``'s oblast-level match,
    since here the exact settlement(s) are known on both sides. Requires
    the same threat type (when both are known/specific) and at least one
    matching settlement name -- never removes a tracked target on a
    guess when neither settlement lines up.
    """
    if (
        tracked.threat_type != reported.threat_type
        and tracked.threat_type != ThreatType.UNKNOWN
        and reported.threat_type != ThreatType.UNKNOWN
    ):
        return False
    if reported.destination_settlement and tracked.destination_settlement == reported.destination_settlement:
        return True
    if reported.origin_settlement and tracked.origin_settlement == reported.origin_settlement:
        return True
    return False


def parse_destroyed_report(text: str) -> Optional[DestroyedReport]:
    """Detect a "target shot down/neutralized" report in ``text``.

    Returns ``None`` unless the message both (a) contains one of
    ``_DESTROYED_KEYWORDS`` AND (b) names at least one recognizable
    oblast or resolvable settlement -- deliberately conservative, same
    "never guess" policy as the rest of this module: a destroy report
    with no identifiable location can't be safely matched against a
    specific tracked movement, so it's left unhandled rather than
    guessed at (the movement will still expire normally via the existing
    time-based prune).
    """
    if not text or not text.strip():
        return None
    lowered = text.lower()
    if not any(keyword in lowered for keyword in _DESTROYED_KEYWORDS):
        return None

    regions = _detect_destroyed_regions(text)
    if not regions:
        return None

    threat_type = _detect_threat_type(text)
    return DestroyedReport(threat_type=threat_type, regions=regions, text=text)


#: Max age gap between a tracked movement's last update and a new
#: message for the new message to be considered a possible continuation
#: of the SAME target, rather than an unrelated new one. Deliberately
#: shorter than the movements list's own overall TTL prune window --
#: continuation is a stronger, more specific claim ("this is the same
#: physical target updating") than "still worth showing on the map", so
#: it gets a tighter bar.
CONTINUATION_MAX_GAP_SECONDS = 600.0  # 10 minutes


def movement_continues(previous: ThreatMovement, new: ThreatMovement, now: datetime) -> bool:
    """True if ``new`` plausibly reports an update on the same target as
    ``previous``, rather than an unrelated new one.

    Conservative by design -- same "never guess" philosophy as
    ``movement_matches_destroyed_report``: requires the same threat type,
    a recent-enough previous update (within ``CONTINUATION_MAX_GAP_SECONDS``),
    and an overlapping oblast between the two reports' known location(s).
    A previous entry with no location at all can't be correlated and
    always returns ``False`` here (it's tracked as its own independent
    entry, same as before this feature existed).
    """
    if previous.threat_type != new.threat_type:
        return False
    if (now - previous.received_at).total_seconds() > CONTINUATION_MAX_GAP_SECONDS:
        return False
    if not previous.has_any_location or not new.has_any_location:
        return False

    def _regions(movement: ThreatMovement) -> set[Region]:
        return {
            region
            for point in (movement.origin_point, movement.destination_point)
            if point is not None
            for region in (region_at_point(point),)
            if region is not None
        }

    return bool(_regions(previous) & _regions(new))
