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


class ThreatType(str, Enum):
    """Type of threat explicitly named in the source message."""

    SHAHED = "shahed"        # "шахед", "камікадзе", "дрон-камікадзе"
    MISSILE = "missile"      # "ракета", "каліб р", "кинджал", "іскандер", "х-101" ...
    UAV = "uav"               # generic "бпла" / "безпілотник" / "дрон" without shahed wording
    AIRCRAFT = "aircraft"     # "міг-31к", "ту-95" (missile carrier aircraft mentions)
    UNKNOWN = "unknown"       # threat-related message where a specific type wasn't named

    @property
    def icon(self) -> str:
        """Emoji icon for this threat type, per the project's reference spec."""
        icons = {
            ThreatType.SHAHED: "🛸",
            ThreatType.MISSILE: "🚀",
            ThreatType.UAV: "🛸",
            ThreatType.AIRCRAFT: "✈️",
            ThreatType.UNKNOWN: "⚠️",
        }
        return icons[self]

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
    origin_name: Optional[str] = None
    destination_name: Optional[str] = None
    origin_point: Optional[tuple[float, float]] = None       # projected (x, y) in map viewBox units
    destination_point: Optional[tuple[float, float]] = None  # projected (x, y) in map viewBox units
    matched_keywords: list[str] = field(default_factory=list)

    @property
    def has_direction(self) -> bool:
        """True only if BOTH an origin and a destination were explicitly found."""
        return self.origin_point is not None and self.destination_point is not None

    @property
    def has_any_location(self) -> bool:
        """True if at least one place (origin or destination) was found."""
        return self.origin_point is not None or self.destination_point is not None

    @property
    def short_description(self) -> str:
        """One-line summary for the side list, built only from extracted fields."""
        if self.origin_name and self.destination_name:
            return f"{self.origin_name} → {self.destination_name}"
        if self.destination_name:
            return f"курс на {self.destination_name}"
        if self.origin_name:
            return f"з {self.origin_name}"
        return "напрямок не вказано"
