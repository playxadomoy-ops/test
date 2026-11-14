"""
Radar/compass canvas for the "Компас загроз" (Threat Compass) page.

Draws:
  * A set of concentric distance rings (evenly spaced fractions of
    ``max_range_km``), each labeled with its real km value -- this is
    axis/grid chrome, the same kind of thing a chart's axis labels are,
    not a claim about any actual detected threat.
  * The eight compass directions (N, NE, E, SE, S, SW, W, NW) around the
    rim.
  * Zero or more ``CompassTarget`` markers, each plotted at its stated
    bearing/distance and colored by threat type (same marker colors as
    the Рух загроз map, see ``app.ui.components.movement_map``). An
    empty target list (the default, until a real analyzer is wired in)
    simply draws the empty radar -- never a fabricated blip.
"""

from __future__ import annotations

import math
from typing import Optional

import flet as ft
import flet.canvas as canvas

from app.models.compass_models import CompassTarget
from app.models.movement_models import ThreatType
from app.ui.theme import colors as theme

_VIEWBOX_SIZE = 400.0
_CENTER = _VIEWBOX_SIZE / 2
_MAX_RADIUS = 150.0
_RING_COUNT = 4
_DIRECTION_LABEL_RADIUS = _MAX_RADIUS + 24.0
_RING_STROKE_COLOR = theme.BORDER
_RING_LABEL_COLOR = theme.TEXT_MUTED

_DIRECTIONS: tuple[tuple[str, float], ...] = (
    ("N", 0.0), ("NE", 45.0), ("E", 90.0), ("SE", 135.0),
    ("S", 180.0), ("SW", 225.0), ("W", 270.0), ("NW", 315.0),
)

#: Same per-threat-type marker colors as the movement map, reused here so
#: a target reads as "the same kind of thing" on both pages.
_TARGET_COLOR: dict[ThreatType, str] = {
    ThreatType.SHAHED: theme.THREAT_ICON_UAV,
    ThreatType.UAV: theme.THREAT_ICON_UAV,
    ThreatType.MISSILE: theme.THREAT_ICON_BALLISTIC,
    ThreatType.AIRCRAFT: theme.THREAT_ICON_AIRCRAFT,
    ThreatType.UNKNOWN: theme.TEXT_MUTED,
}


def _bearing_to_xy(bearing_degrees: float, radius: float) -> tuple[float, float]:
    """Convert a compass bearing (0=N, clockwise) + radius to viewBox x/y."""
    angle = math.radians(bearing_degrees)
    return _CENTER + radius * math.sin(angle), _CENTER - radius * math.cos(angle)


def _target_marker_shapes(x: float, y: float, color: str) -> list[canvas.Shape]:
    """Small filled circle + thin contrasting ring for one plotted target."""
    return [
        canvas.Circle(x, y, 7.0, paint=ft.Paint(color=color, style=ft.PaintingStyle.FILL)),
        canvas.Circle(
            x, y, 7.0,
            paint=ft.Paint(color=theme.BACKGROUND, style=ft.PaintingStyle.STROKE, stroke_width=1.5),
        ),
    ]


class CompassRadar(ft.Container):
    """The circular radar visual: rings, directions, and threat targets."""

    def __init__(self, max_range_km: float = 200.0) -> None:
        self._max_range_km = max_range_km
        self._targets: list[CompassTarget] = []
        self._render_size = 320.0
        #: Real device heading from CompassSensorService, in degrees
        #: (0=N, clockwise), or None whenever a real reading isn't
        #: available -- None means "no rotation applied", i.e. this
        #: radar renders exactly as it always has (fixed N-up), never a
        #: guessed/simulated rotation. Set via set_heading().
        self._device_heading: Optional[float] = None

        self._canvas = canvas.Canvas(shapes=[], width=self._render_size, height=self._render_size)
        self._overlay_stack = ft.Stack(controls=[self._canvas], width=self._render_size, height=self._render_size)

        super().__init__(alignment=ft.alignment.center, content=self._overlay_stack)
        self._rebuild()

    # --- Lifecycle -----------------------------------------------------

    def did_mount(self) -> None:
        if self.page is not None and self.page.width:
            self.resize(float(self.page.width))

    # --- Public API ------------------------------------------------------

    def resize(self, page_width: float) -> None:
        """Recompute the radar's pixel size from the current page width."""
        available = max(220.0, min(page_width - 2 * (theme.PAGE_PADDING + theme.CARD_PADDING), 420.0))
        if available == self._render_size:
            return
        self._render_size = available
        self._rebuild()

    def set_max_range(self, max_range_km: float) -> None:
        """Change the outer ring's real-world distance (axis rescale)."""
        if max_range_km == self._max_range_km:
            return
        self._max_range_km = max_range_km
        self._rebuild()

    def set_targets(self, targets: list[CompassTarget]) -> None:
        """Replace the currently-plotted targets and redraw."""
        self._targets = list(targets)
        self._rebuild()

    def set_heading(self, heading_degrees: Optional[float]) -> None:
        """Rotate the whole disk (direction labels + target markers
        together, as one rigid unit) so N points to the device's actual
        real-world North -- purely a rendering transform.

        ``heading_degrees`` must be a REAL reading from
        ``CompassSensorService`` (or ``None`` when no real reading is
        available); this method never validates that, so it's the
        caller's responsibility not to invent a value. ``None`` restores
        the exact original fixed N-up rendering -- no rotation is ever
        applied without a real heading behind it.

        This never changes ``CompassTarget.bearing_degrees`` itself or
        any of the category/stat calculations elsewhere on the page --
        those are computed from real geography (see
        ``app.services.compass_builder``) and stay exactly as they are;
        only where each bearing is DRAWN on screen shifts.
        """
        if heading_degrees == self._device_heading:
            return
        self._device_heading = heading_degrees
        self._rebuild()

    # --- Internal --------------------------------------------------------

    def _scale_factor(self) -> float:
        return self._render_size / _VIEWBOX_SIZE

    def _display_bearing(self, real_bearing_degrees: float) -> float:
        """A real-world bearing, rotated for display so N points to the
        device's actual heading (see set_heading) -- identity (no
        change) when no real heading is available.
        """
        if self._device_heading is None:
            return real_bearing_degrees
        return (real_bearing_degrees - self._device_heading) % 360.0

    def _to_pixels(self, point: tuple[float, float]) -> tuple[float, float]:
        factor = self._scale_factor()
        return point[0] * factor, point[1] * factor

    def _rebuild(self) -> None:
        self._canvas.width = self._render_size
        self._canvas.height = self._render_size
        self._canvas.shapes = self._build_shapes()
        self._overlay_stack.width = self._render_size
        self._overlay_stack.height = self._render_size
        self._overlay_stack.controls = [self._canvas, *self._build_labels()]
        if self.page is not None:
            self._overlay_stack.update()

    def _build_shapes(self) -> list[canvas.Shape]:
        shapes: list[canvas.Shape] = []
        factor = self._scale_factor()
        cx, cy = self._to_pixels((_CENTER, _CENTER))

        for i in range(1, _RING_COUNT + 1):
            r = (_MAX_RADIUS * i / _RING_COUNT) * factor
            shapes.append(
                canvas.Circle(
                    cx, cy, r,
                    paint=ft.Paint(color=_RING_STROKE_COLOR, style=ft.PaintingStyle.STROKE, stroke_width=1.0),
                )
            )

        # Faint N-S / E-W crosshair, matching the reference layout.
        edge = _MAX_RADIUS * factor
        cross_paint = ft.Paint(color=_RING_STROKE_COLOR, style=ft.PaintingStyle.STROKE, stroke_width=1.0)
        shapes.append(canvas.Line(cx, cy - edge, cx, cy + edge, paint=cross_paint))
        shapes.append(canvas.Line(cx - edge, cy, cx + edge, cy, paint=cross_paint))

        # Target markers.
        for target in self._targets:
            distance_fraction = min(target.distance_km / self._max_range_km, 1.0) if self._max_range_km else 0.0
            radius = _MAX_RADIUS * distance_fraction
            px, py = self._to_pixels(_bearing_to_xy(self._display_bearing(target.bearing_degrees), radius))
            color = _TARGET_COLOR.get(target.threat_type, theme.TEXT_MUTED)
            shapes.extend(_target_marker_shapes(px, py, color))

        return shapes

    def _build_labels(self) -> list[ft.Control]:
        controls: list[ft.Control] = []

        for label, bearing in _DIRECTIONS:
            px, py = self._to_pixels(_bearing_to_xy(self._display_bearing(bearing), _DIRECTION_LABEL_RADIUS))
            controls.append(
                ft.Container(
                    left=px - 12,
                    top=py - 8,
                    width=24,
                    content=ft.Text(
                        label, size=11, weight=ft.FontWeight.W_600,
                        color=theme.TEXT_PRIMARY, text_align=ft.TextAlign.CENTER,
                    ),
                )
            )

        for i in range(1, _RING_COUNT + 1):
            ring_km = self._max_range_km * i / _RING_COUNT
            px, py = self._to_pixels((_CENTER, _CENTER - (_MAX_RADIUS * i / _RING_COUNT)))
            controls.append(
                ft.Container(
                    left=px + 4,
                    top=py - 7,
                    content=ft.Text(f"{ring_km:.0f} км", size=9, color=_RING_LABEL_COLOR),
                )
            )

        for target in self._targets:
            distance_fraction = min(target.distance_km / self._max_range_km, 1.0) if self._max_range_km else 0.0
            radius = _MAX_RADIUS * distance_fraction
            px, py = self._to_pixels(_bearing_to_xy(self._display_bearing(target.bearing_degrees), radius))
            color = _TARGET_COLOR.get(target.threat_type, theme.TEXT_MUTED)
            controls.append(
                ft.Container(
                    left=px - 24,
                    top=py + 8,
                    width=48,
                    content=ft.Text(
                        f"{target.distance_km:.0f} км", size=9, color=color,
                        text_align=ft.TextAlign.CENTER, weight=ft.FontWeight.W_600,
                    ),
                )
            )

        return controls
