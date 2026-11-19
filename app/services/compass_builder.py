"""
Builds a real :class:`~app.models.compass_models.CompassSnapshot` from the
application's actual currently-tracked "Рух загроз" movement list.

Every number here is computed directly from ``ThreatMovement`` entries
already produced by :mod:`app.services.movement_parser` -- there is no
separate/fabricated threat feed. A movement only contributes to the
compass if it has at least one real, resolved location (``has_any_location``);
a movement with neither an origin nor a destination has nothing to plot
and is simply excluded, not guessed at.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.models.compass_models import (
    CompassCategorySummary,
    CompassPrimaryDirection,
    CompassSnapshot,
    CompassTarget,
)
from app.models.movement_models import ThreatMovement, ThreatType
from app.ukraine_geo import bearing_and_distance_km, unproject_to_lat_lon

#: Reference point the compass measures bearing/distance FROM. Kyiv, as
#: the capital and the app's own default/most-referenced settlement
#: (see app.data.ukraine_cities) -- the same convention real-world
#: "compass загроз" Telegram bots use, since it's the single point of
#: greatest common interest for the app's users.
_REFERENCE_POINT_LATLON = (50.4501, 30.5234)  # Kyiv

#: The eight compass octants used for the "primary direction" summary,
#: as (label, center bearing degrees). Matches the radar widget's own
#: N/NE/E/SE/S/SW/W/NW labels exactly.
_OCTANTS: tuple[tuple[str, float], ...] = (
    ("Північний", 0.0), ("Північно-східний", 45.0), ("Східний", 90.0), ("Південно-східний", 135.0),
    ("Південний", 180.0), ("Південно-західний", 225.0), ("Західний", 270.0), ("Північно-західний", 315.0),
)

#: Threat types shown as their own top-row category card, in display order.
_CATEGORY_TYPES: tuple[ThreatType, ...] = (ThreatType.SHAHED, ThreatType.MISSILE, ThreatType.AIRCRAFT)


def _octant_label(bearing_degrees: float) -> str:
    """Nearest of the 8 compass octants for a given bearing."""
    closest = min(_OCTANTS, key=lambda o: min(abs(bearing_degrees - o[1]), 360.0 - abs(bearing_degrees - o[1])))
    return closest[0]


def _movement_reference_point(movement: ThreatMovement) -> Optional[tuple[float, float]]:
    """The point on ``movement`` to plot on the compass, or ``None``.

    Prefers the destination (the most recently-stated location in a
    typical "A → B" report, and the best available estimate of where a
    still-in-flight target currently is), falling back to the origin
    when only that is known. Never invents a location.
    """
    return movement.destination_point or movement.origin_point


def _threat_type_for_category(threat_type: ThreatType) -> ThreatType:
    """Fold UAV into the SHAHED category card (same grouping the map's
    icon legend already uses -- see ``movement_map._icon_geometry``,
    which draws SHAHED and UAV with the identical icon)."""
    return ThreatType.SHAHED if threat_type == ThreatType.UAV else threat_type


def build_compass_snapshot(
    movements: list[ThreatMovement],
    is_online: bool,
    threat_level_label: Optional[str] = None,
) -> CompassSnapshot:
    """Derive a full :class:`CompassSnapshot` from the current movement list.

    ``is_online`` and ``threat_level_label`` are passed in rather than
    recomputed here so this stays a pure function of already-existing
    application state (channel-connection status, the same
    ``ThreatSnapshot.overall_risk`` label already shown on the Огляд
    tab) instead of a second, separate notion of either.
    """
    targets: list[CompassTarget] = []
    distances_by_category: dict[ThreatType, list[float]] = {t: [] for t in _CATEGORY_TYPES}
    octant_counts: dict[str, int] = {label: 0 for label, _ in _OCTANTS}
    all_distances: list[float] = []
    nearest: Optional[tuple[float, str]] = None  # (distance_km, label)
    farthest: Optional[tuple[float, str]] = None

    for movement in movements:
        point = _movement_reference_point(movement)
        if point is None:
            continue

        target_latlon = unproject_to_lat_lon(point)
        bearing, distance_km = bearing_and_distance_km(_REFERENCE_POINT_LATLON, target_latlon)

        label = movement.display_destination_name or movement.display_origin_name
        targets.append(
            CompassTarget(
                threat_type=movement.threat_type,
                bearing_degrees=bearing,
                distance_km=distance_km,
                label=label,
            )
        )

        category = _threat_type_for_category(movement.threat_type)
        if category in distances_by_category:
            distances_by_category[category].append(distance_km)

        octant_counts[_octant_label(bearing)] += 1
        all_distances.append(distance_km)

        entry_label = f"{movement.threat_type.label_uk}" + (f" ({label})" if label else "")
        if nearest is None or distance_km < nearest[0]:
            nearest = (distance_km, entry_label)
        if farthest is None or distance_km > farthest[0]:
            farthest = (distance_km, entry_label)

    categories = tuple(
        CompassCategorySummary(
            threat_type=category,
            target_count=len(distances_by_category[category]),
            min_distance_km=min(distances_by_category[category]) if distances_by_category[category] else None,
            max_distance_km=max(distances_by_category[category]) if distances_by_category[category] else None,
        )
        for category in _CATEGORY_TYPES
    )

    primary_direction = CompassPrimaryDirection()
    total = len(targets)
    if total > 0:
        top_label, top_count = max(octant_counts.items(), key=lambda item: item[1])
        if top_count > 0:
            share = 100.0 * top_count / total
            # A disclosed, fixed threshold on the real computed share --
            # not a fabricated number, just how "high/moderate/low" is
            # worded for a given concentration of real current targets.
            if share >= 50.0:
                activity_label = "Активність висока"
            elif share >= 25.0:
                activity_label = "Активність помірна"
            else:
                activity_label = "Активність низька"
            primary_direction = CompassPrimaryDirection(
                direction_label=top_label, share_percent=share, activity_label=activity_label
            )

    average_km = sum(all_distances) / len(all_distances) if all_distances else None

    return CompassSnapshot(
        targets=tuple(targets),
        categories=categories,
        primary_direction=primary_direction,
        total_targets=total,
        nearest_km=nearest[0] if nearest else None,
        nearest_label=nearest[1] if nearest else None,
        farthest_km=farthest[0] if farthest else None,
        farthest_label=farthest[1] if farthest else None,
        average_km=average_km,
        threat_level_label=threat_level_label,
        updated_at=datetime.now(),
        is_online=is_online,
    )
