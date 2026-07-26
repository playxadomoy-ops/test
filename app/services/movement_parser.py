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
from app.models.movement_models import ThreatMovement, ThreatType
from app.ukraine_geo import project_lat_lon


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
    )
