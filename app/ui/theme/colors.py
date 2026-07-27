"""
Dark theme palette for the whole application.

Kept in one module so every screen looks consistent and so the palette
can be tuned in a single place. Only real ``ft.Colors`` members are used
here — every name below was verified to exist in Flet 0.28.3.
"""

from __future__ import annotations

import flet as ft

from app.models.alert_models import RiskLevel

# --- Base surfaces -----------------------------------------------------
BACKGROUND = "#0A0E14"
SURFACE = "#12161F"
SURFACE_ELEVATED = "#171C27"
BORDER = "#232A38"

# --- Text ----------------------------------------------------------------
TEXT_PRIMARY = ft.Colors.WHITE
TEXT_SECONDARY = "#8A93A6"
TEXT_MUTED = "#5B637A"

# --- Accents ---------------------------------------------------------
ACCENT_BLUE = "#3B82F6"
ACCENT_PURPLE = "#8B5CF6"
WARNING_COLOR = "#F59E0B"

# --- Risk-level colors -----------------------------------------------
RISK_COLORS: dict[RiskLevel, str] = {
    RiskLevel.NONE: "#22C55E",
    RiskLevel.LOW: "#84CC16",
    RiskLevel.MEDIUM: "#F59E0B",
    RiskLevel.HIGH: "#F97316",
    RiskLevel.CRITICAL: "#EF4444",
}

# --- Threat status colors (binary active/clear indicator) ------------
#: Deliberately just green/red -- this indicator answers only "is there
#: an active threat right now", not "how severe" (that's RISK_COLORS).
THREAT_STATUS_CLEAR_COLOR = "#22C55E"
THREAT_STATUS_ACTIVE_COLOR = "#EF4444"

# --- Threat Movement map: per-threat-type marker colors ----------------
#: Used by the vector threat-track icons on the "Рух загроз" map
#: (replaces the old flat-emoji markers). Kept distinct from RISK_COLORS
#: -- these identify *what kind* of threat a marker is, not how
#: dangerous the overall situation is.
THREAT_ICON_UAV = "#D4A24C"        # UAV / Shahed -- amber, "loitering munition"
THREAT_ICON_CRUISE = "#38BDF8"     # cruise missile -- cyan, matches the arrow color
THREAT_ICON_BALLISTIC = "#EF4444"  # ballistic missile -- red, most severe
THREAT_ICON_AIRCRAFT = "#94A3B8"   # carrier aircraft -- silver/gray

REGION_INACTIVE_COLOR = "#F1F3F7"
REGION_ACTIVE_COLOR = "#EF4444"
REGION_STROKE = "#0A0E14"

#: Border color used on the "Рух загроз" map while it's cropped/zoomed
#: to a user-selected set of oblasts, so it's visually clear that a
#: filtered view (not the whole country) is being shown.
REGION_WATCHED_STROKE = ACCENT_PURPLE

# --- Shared layout constants ------------------------------------------
# Kept here (not re-declared per file) so any control that must reason
# about pixel geometry -- currently only the Ukraine map, which does its
# own hit-testing math -- can stay in sync with the actual chrome sizes
# set in ``main.py`` and in the card containers around it.
PAGE_PADDING = 16
CARD_PADDING = 16

# --- Log level colors --------------------------------------------------
LOG_LEVEL_COLORS = {
    "DEBUG": "#5B637A",
    "INFO": "#3B82F6",
    "WARNING": "#F59E0B",
    "ERROR": "#EF4444",
}


def risk_color(level: RiskLevel) -> str:
    """Return the accent color associated with a risk level."""
    return RISK_COLORS.get(level, RISK_COLORS[RiskLevel.NONE])


def build_page_theme() -> ft.Theme:
    """Build the shared dark ``ft.Theme`` applied to the whole page."""
    return ft.Theme(
        color_scheme_seed=ACCENT_BLUE,
        color_scheme=ft.ColorScheme(
            primary=ACCENT_BLUE,
            surface=SURFACE,
            background=BACKGROUND,
        ),
    )


#: Splash screen text color -- reuses the same red already used for
#: critical risk / active-region indicators, so the splash matches the
#: app's own palette instead of introducing a new color.
SPLASH_TEXT_COLOR = "#EF4444"
