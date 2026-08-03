"""
Maps this app's actual threat/status data to the real PNG icon asset that
represents it on screen -- used by the movement map, the movement list,
dialogs, and status panels, so every place a threat glyph appears draws
the exact same icon for the exact same threat/status. One place to look
when adding a new icon or checking one is wired up correctly, instead of
several call sites each keeping their own (possibly drifting) copy of
this logic.

Every icon referenced here is a real file under ``assets/icons/`` (see
``tools/generate_icons.py``), served through ``assets_dir`` (set in
``main.py``'s ``ft.app(...)`` call). This module intentionally only
distinguishes threat sub-types this project's parser can actually
determine (Shahed/UAV, cruise missile, ballistic missile, generic
aircraft) -- it does not invent a separate icon for a MiG-31K vs. a
strategic bomber vs. a helicopter, since ``ThreatType`` has no such
distinction in the underlying data; showing a different glyph for
something the app cannot actually tell apart would be misleading rather
than informative. A "group" report (``group_count`` > 1) is shown via
the existing "×N" badge next to the single icon, not a second icon.
"""

from __future__ import annotations

from app.models.movement_models import ThreatMovement, ThreatType
from app.ui.theme import colors as theme

ICON_SHAHED_UAV = "icons/shahed_uav.png"
ICON_CRUISE_MISSILE = "icons/cruise_missile.png"
ICON_BALLISTIC_MISSILE = "icons/ballistic_missile.png"
ICON_AIRCRAFT = "icons/aircraft.png"
#: A target reported destroyed/intercepted (``ThreatMovement.status_label``
#: set) -- shown instead of the weapon glyph once an outcome is known.
ICON_EXPLOSION = "icons/explosion.png"
#: A region's own air-raid alert state (Огляд map info dialogs, region
#: status rows) -- replaces the old 🔴/🟢 emoji.
ICON_SIREN_ACTIVE = "icons/siren_active.png"
ICON_SIREN_CLEAR = "icons/siren_clear.png"
#: A nationwide alert/cancellation banner (see
#: ``app.services.region_alert_parser.parse_nationwide_alert``).
ICON_NATIONWIDE_WARNING = "icons/nationwide_warning.png"

#: Word stems that identify a missile report as specifically ballistic or
#: cruise -- same list ``movement_map._icon_geometry`` used to pick a
#: vector color before the PNG-icon change; kept here now since this is
#: the one place threat-type -> icon decisions are made.
_BALLISTIC_KEYWORDS = frozenset({"балістик", "кинджал", "іскандер", "ballistic missile"})
_CRUISE_KEYWORDS = frozenset({"калібр", "х-101", "х-555", "cruise missile"})


def missile_subtype(movement: ThreatMovement) -> str:
    """"ballistic", "cruise", or "unknown" for a ``ThreatType.MISSILE`` movement.

    Never guesses: only returns something other than "unknown" when a
    specific missile-type keyword was actually present in the source
    message (``movement.matched_keywords``).
    """
    keywords = set(movement.matched_keywords)
    if keywords & _BALLISTIC_KEYWORDS:
        return "ballistic"
    if keywords & _CRUISE_KEYWORDS:
        return "cruise"
    return "unknown"


def icon_for_movement(movement: ThreatMovement) -> str:
    """Return the real PNG asset path for a movement's threat-type icon.

    A movement with an outcome already reported (``status_label`` set)
    shows the explosion/destroyed glyph instead of its weapon type --
    the outcome is the more relevant thing to communicate at a glance
    once it's known. ``ThreatType.UNKNOWN`` (no specific weapon named)
    uses the UAV silhouette as the most neutral generic "unidentified
    aerial threat" glyph, never a missing/blank icon.
    """
    if movement.status_label is not None:
        return ICON_EXPLOSION
    kind = missile_subtype(movement) if movement.threat_type == ThreatType.MISSILE else "unknown"
    return icon_for_threat_type(movement.threat_type, kind)


def icon_for_threat_type(threat_type: ThreatType, missile_kind: str = "unknown") -> str:
    """Return the real PNG asset path for a bare ``ThreatType`` (+ missile
    kind, when already known/computed elsewhere -- see ``missile_subtype``).
    """
    if threat_type in (ThreatType.SHAHED, ThreatType.UAV):
        return ICON_SHAHED_UAV
    if threat_type is ThreatType.AIRCRAFT:
        return ICON_AIRCRAFT
    if threat_type is ThreatType.MISSILE:
        return ICON_BALLISTIC_MISSILE if missile_kind == "ballistic" else ICON_CRUISE_MISSILE
    return ICON_SHAHED_UAV


def color_for_movement(movement: ThreatMovement) -> str:
    """Return the accent color matching ``icon_for_movement``'s icon --
    used for that icon's glow tint (see ``app.ui.components.icon_glyph``).
    """
    if movement.status_label is not None:
        return theme.REGION_ACTIVE_COLOR
    kind = missile_subtype(movement) if movement.threat_type == ThreatType.MISSILE else "unknown"
    return color_for_threat_type(movement.threat_type, kind)


def color_for_threat_type(threat_type: ThreatType, missile_kind: str = "unknown") -> str:
    """Return the accent color matching ``icon_for_threat_type``'s icon."""
    if threat_type in (ThreatType.SHAHED, ThreatType.UAV):
        return theme.THREAT_ICON_UAV
    if threat_type is ThreatType.AIRCRAFT:
        return theme.THREAT_ICON_AIRCRAFT
    if threat_type is ThreatType.MISSILE:
        return theme.THREAT_ICON_BALLISTIC if missile_kind == "ballistic" else theme.THREAT_ICON_CRUISE
    return theme.THREAT_ICON_UAV
