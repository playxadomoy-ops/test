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

from app.data.ukraine_cities import resolve_city
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
    _ThreatKeyword("балістик", ThreatType.MISSILE),
    _ThreatKeyword("кинджал", ThreatType.MISSILE),
    _ThreatKeyword("калібр", ThreatType.MISSILE),
    _ThreatKeyword("іскандер", ThreatType.MISSILE),
    _ThreatKeyword("х-101", ThreatType.MISSILE),
    _ThreatKeyword("х-555", ThreatType.MISSILE),
    _ThreatKeyword("ракет", ThreatType.MISSILE),  # stem: matches ракета/ракети/ракетний/ракетного/...
    _ThreatKeyword("міг-31к", ThreatType.AIRCRAFT),
    _ThreatKeyword("ту-95", ThreatType.AIRCRAFT),
    _ThreatKeyword("зліт", ThreatType.AIRCRAFT),
    _ThreatKeyword("бпла", ThreatType.UAV),
    _ThreatKeyword("безпілотник", ThreatType.UAV),
    _ThreatKeyword("дрон", ThreatType.UAV),
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
)

#: Patterns whose single capture group is a DESTINATION place name.
_DESTINATION_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"у\s+напрямку\s+(?:до\s+)?([А-ЯҐЄІЇа-яґєії'\-]+)", re.IGNORECASE),
    re.compile(r"в\s+напрямку\s+(?:до\s+)?([А-ЯҐЄІЇа-яґєії'\-]+)", re.IGNORECASE),
    re.compile(r"напрямок[уі]?\s+(?:на\s+)?([А-ЯҐЄІЇа-яґєії'\-]+)", re.IGNORECASE),
    re.compile(r"курс(?:ом)?\s+на\s+([А-ЯҐЄІЇа-яґєії'\-]+)", re.IGNORECASE),
    re.compile(r"на\s+([А-ЯҐЄІЇа-яґєії'\-]+)", re.IGNORECASE),
)


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

    Returns ``None`` if the message doesn't mention any recognized threat
    keyword at all (this parser only concerns itself with threat-movement
    messages, not general chatter). Origin/destination are left ``None``
    whenever the text doesn't explicitly name a place this module can
    resolve -- this function never infers a location that isn't stated.
    """
    if not text or not text.strip():
        return None

    threat_type = _detect_threat_type(text)
    if threat_type is None:
        return None

    origin_name = _extract_place(text, _ORIGIN_PATTERNS)
    destination_name = _extract_place(text, _DESTINATION_PATTERNS)

    # A single "з X" match can occasionally also satisfy a weak "на Y"
    # pattern on the same word; guard against origin and destination
    # accidentally resolving to the exact same place.
    if origin_name and destination_name and origin_name.strip().lower() == destination_name.strip().lower():
        destination_name = None

    origin_point = None
    if origin_name is not None:
        coords = resolve_city(origin_name)
        if coords is not None:
            origin_point = project_lat_lon(*coords)

    destination_point = None
    if destination_name is not None:
        coords = resolve_city(destination_name)
        if coords is not None:
            destination_point = project_lat_lon(*coords)

    matched_keywords = [k.pattern for k in THREAT_KEYWORDS if k.pattern in text.lower()]
    group_count = _detect_group_count(text)

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
