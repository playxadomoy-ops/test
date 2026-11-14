"""
Data model for the "Компас загроз" (Threat Compass) page.

This module intentionally holds NO threat-detection logic of its own --
it is just the shape of data the compass page renders. Per the current
project requirement, only the page's complete UI structure is being
built right now; nothing here computes a real bearing/distance/threat
level from live messages yet. ``CompassSnapshot.empty()`` is the neutral,
explicitly-empty state the page starts in (matching the same "empty
state, not fake data" pattern already used elsewhere in this project,
e.g. ``StatsPanel``'s "0"/"—" defaults) -- a real analyzer can later
construct a populated ``CompassSnapshot`` and hand it to
``ThreatCompassView.set_snapshot`` without any change to this shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from app.models.movement_models import ThreatType


@dataclass(frozen=True, slots=True)
class CompassTarget:
    """One tracked target plotted on the radar.

    ``bearing_degrees`` is compass bearing from the observer (0 = N, 90 =
    E, ...); ``distance_km`` is its plotted radial distance. Both must be
    explicitly known for a target to be plotted -- there is no
    inference of either value anywhere in this module.
    """

    threat_type: ThreatType
    bearing_degrees: float
    distance_km: float
    label: Optional[str] = None


@dataclass(frozen=True, slots=True)
class CompassCategorySummary:
    """One top-row category card (e.g. "Shahed: 14 цілей, 15–120 км")."""

    threat_type: ThreatType
    target_count: int = 0
    min_distance_km: Optional[float] = None
    max_distance_km: Optional[float] = None

    @property
    def range_label(self) -> str:
        """Human-readable distance range, or an explicit "no data" dash."""
        if self.min_distance_km is None or self.max_distance_km is None:
            return "—"
        if self.min_distance_km == self.max_distance_km:
            return f"{self.min_distance_km:.0f} км"
        return f"{self.min_distance_km:.0f} – {self.max_distance_km:.0f} км"


@dataclass(frozen=True, slots=True)
class CompassPrimaryDirection:
    """The single most-active compass direction, if any is determined."""

    direction_label: Optional[str] = None  # e.g. "Північний схід"
    share_percent: Optional[float] = None
    activity_label: Optional[str] = None  # e.g. "Активність висока"


@dataclass(frozen=True, slots=True)
class CompassSnapshot:
    """Everything the Threat Compass page needs to render one refresh."""

    targets: tuple[CompassTarget, ...] = field(default_factory=tuple)
    categories: tuple[CompassCategorySummary, ...] = field(default_factory=tuple)
    primary_direction: CompassPrimaryDirection = field(default_factory=CompassPrimaryDirection)
    total_targets: int = 0
    nearest_km: Optional[float] = None
    nearest_label: Optional[str] = None
    farthest_km: Optional[float] = None
    farthest_label: Optional[str] = None
    average_km: Optional[float] = None
    threat_level_label: Optional[str] = None
    updated_at: Optional[datetime] = None
    is_online: bool = False

    @classmethod
    def empty(cls) -> "CompassSnapshot":
        """The neutral starting state: zero targets, no data anywhere.

        Still lists all four categories (so the top-row cards always
        show every threat type, just with a "0 цілей" / "—" empty
        state) rather than omitting cards until data exists.
        """
        return cls(
            categories=(
                CompassCategorySummary(ThreatType.SHAHED),
                CompassCategorySummary(ThreatType.MISSILE),
                CompassCategorySummary(ThreatType.AIRCRAFT),
            ),
        )
