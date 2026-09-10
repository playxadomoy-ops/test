"""
Data model for "Рух загроз" (threat movement) entries.

Every :class:`ThreatMovement` is derived strictly from what a Telegram
message *explicitly states* -- a origin city, a destination city, a
threat type keyword. There is no prediction, no AI inference, no
route-building, and no target-guessing anywhere in this model or in the
parser that builds it (:mod:`app.services.movement_parser`). If a message
doesn't explicitly name a place, that field stays ``None`` and nothing is
drawn for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from app.models.alert_models import Region


#: Cap on how many prior points ``ThreatMovement.position_history`` keeps
#: -- both a memory bound (an update-heavy target could otherwise grow
#: unboundedly for as long as it's tracked) and a rendering bound (the
#: map only ever needs to draw a short, readable trail, not a full log).
MAX_POSITION_HISTORY = 5


class ThreatType(str, Enum):
    """Type of threat explicitly named in the source message."""

    SHAHED = "shahed"        # "шахед", "камікадзе", "дрон-камікадзе"
    MISSILE = "missile"      # "ракета", "каліб р", "кинджал", "іскандер", "х-101" ...
    UAV = "uav"               # generic "бпла" / "безпілотник" / "дрон" without shahed wording
    AIRCRAFT = "aircraft"     # "міг-31к", "ту-95" (missile carrier aircraft mentions)
    UNKNOWN = "unknown"       # threat-related message where a specific type wasn't named

    @property
    def label_uk(self) -> str:
        """Human readable Ukrainian label for this threat type."""
        labels = {
            ThreatType.SHAHED: "Шахед",
            ThreatType.MISSILE: "Ракета",
            ThreatType.UAV: "БПЛА",
            ThreatType.AIRCRAFT: "Літак-носій",
            ThreatType.UNKNOWN: "Загроза",
        }
        return labels[self]


@dataclass(slots=True)
class ThreatMovement:
    """One threat-movement entry, built only from explicit message text."""

    id: str
    threat_type: ThreatType
    channel_username: str
    received_at: datetime
    text: str
    #: When this target was FIRST detected -- unlike ``received_at``
    #: (which is overwritten on every continuation update, see
    #: main.py's continuation-matching), this never changes after
    #: creation, so "when did this threat appear" stays answerable for
    #: as long as the entry itself exists (which per the "never delete
    #: history" requirement is effectively its whole life, active or
    #: destroyed). Defaults to ``received_at`` for ordinary construction
    #: (a brand-new entry's "first seen" IS its received time).
    first_seen_at: Optional[datetime] = None
    #: When a destroy/intercept report was matched against this entry
    #: (see main.py's outcome-report handling) -- ``None`` while still
    #: active. Together with ``status_label`` this is the entry's
    #: Active/Destroyed lifecycle state: ``destroyed_at is None`` means
    #: active, set means destroyed, at that exact recorded time.
    destroyed_at: Optional[datetime] = None
    origin_name: Optional[str] = None
    destination_name: Optional[str] = None
    origin_point: Optional[tuple[float, float]] = None       # projected (x, y) in map viewBox units
    destination_point: Optional[tuple[float, float]] = None  # projected (x, y) in map viewBox units
    matched_keywords: list[str] = field(default_factory=list)
    #: Number of targets this single entry represents (e.g. "2 шахеди" ->
    #: 2). Defaults to 1 for ordinary, backward-compatible construction
    #: (every existing call site that doesn't pass this keeps working
    #: unchanged) -- see app.services.movement_parser's group-detection
    #: for how a value >1 gets set. The map draws ONE marker with a "×N"
    #: badge for a grouped entry, never N overlapping markers.
    group_count: int = 1
    #: Prior confirmed positions for this same tracked target, oldest
    #: first, capped at a small length by whoever appends to it (see
    #: main.py's continuation-matching) -- NOT a GPS track of a real
    #: object, just this entry's own previously-reported point(s) before
    #: a later message was matched as continuing it. Expires automatically
    #: together with the entry itself (it's a field on it, not separate
    #: storage), never persisted or re-derived. Empty for an entry that
    #: hasn't (yet) been matched as a continuation of anything.
    position_history: list[tuple[float, float]] = field(default_factory=list)
    #: Owning oblast for ``origin_point``/``destination_point``, computed
    #: once at parse time (``app.services.movement_parser.parse_message``)
    #: instead of every consumer recomputing ``region_at_point`` itself.
    #: Purely a cache of the same projected point already stored above --
    #: never a separate source of truth, and ``None`` under the exact
    #: same conditions the corresponding ``*_point`` is ``None``.
    origin_region: Optional[Region] = None
    destination_region: Optional[Region] = None
    #: Presentable, capitalized canonical settlement name (e.g. "Бориспіль"),
    #: resolved via ``app.data.ukraine_cities.resolve_city_name`` from the
    #: SAME raw text already stored in ``origin_name``/``destination_name``
    #: -- added alongside those existing fields, not a replacement for
    #: them (see ``short_description`` below for how the two combine).
    origin_settlement: Optional[str] = None
    destination_settlement: Optional[str] = None
    #: Canonical Ukrainian label ("Збито", "Знищено", "Перехоплено", ...)
    #: when the SAME message that reported this route/threat also
    #: explicitly stated the target was shot down/destroyed/intercepted
    #: (e.g. "Shahed Boryspil → Voronkiv shot down") -- set by
    #: ``app.services.movement_parser.parse_message``. ``None`` for an
    #: ordinary in-flight report. This is distinct from the separate
    #: "target gone, remove from map" report handled by
    #: ``parse_destroyed_report``/``DestroyedReport`` -- a message with
    #: both a fresh route AND a terminal status still gets added here
    #: (with this field set), it is not silently removed.
    status_label: Optional[str] = None

    def __post_init__(self) -> None:
        if self.first_seen_at is None:
            self.first_seen_at = self.received_at

    @property
    def has_direction(self) -> bool:
        """True only if BOTH an origin and a destination were explicitly found."""
        return self.origin_point is not None and self.destination_point is not None

    @property
    def has_any_location(self) -> bool:
        """True if at least one place (origin or destination) was found."""
        return self.origin_point is not None or self.destination_point is not None

    @property
    def display_origin_name(self) -> Optional[str]:
        """Best available name for the origin: canonical settlement name
        if it resolved, else the raw matched text, else ``None``."""
        return self.origin_settlement or self.origin_name

    @property
    def display_destination_name(self) -> Optional[str]:
        """Best available name for the destination -- see ``display_origin_name``."""
        return self.destination_settlement or self.destination_name

    @property
    def short_description(self) -> str:
        """One-line summary for the side list, built only from extracted fields."""
        origin = self.display_origin_name
        destination = self.display_destination_name
        if origin and destination:
            base = f"{origin} → {destination}"
        elif destination:
            # No origin was explicitly stated in the source message -- say so
            # plainly rather than silently omitting it (a destination-only
            # report must still read as a real, visible threat entry).
            base = f"Unknown origin, moving towards {destination}"
        elif origin:
            base = f"з {origin}"
        else:
            base = "напрямок не вказано"
        return f"{base} ({self.status_label})" if self.status_label else base
