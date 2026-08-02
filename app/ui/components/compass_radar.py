"""
Radar/compass canvas for the "Компас загроз" (Threat Compass) page.

Draws:
  * A set of concentric distance rings (evenly spaced fractions of
    ``max_range_km``), each labeled with its real km value -- this is
    axis/grid chrome, the same kind of thing a chart's axis labels are,
    not a claim about any actual detected threat.
  * The eight compass directions (N, NE, E, SE, S, SW, W, NW) around the
    rim.
  * A miniature silhouette of Ukraine at the center, built from this
    project's own real region geometry (``app.ukraine_geo.REGION_RINGS``)
    scaled down to fit -- reusing real data already in the app rather
    than a new placeholder shape.
  * Zero or more ``CompassTarget`` markers, each plotted at its stated
    bearing/distance and colored by threat type (same marker colors as
    the Рух загроз map, see ``app.ui.components.movement_map``). An
    empty target list (the default, until a real analyzer is wired in)
    simply draws the empty radar -- never a fabricated blip.
"""

from __future__ import annotations

import math

import flet as ft
import flet.canvas as canvas

from app.models.compass_models import CompassTarget
from app.models.movement_models import ThreatType
from app.ukraine_geo import REGION_RINGS
from app.ui.theme import colors as theme

_VIEWBOX_SIZE = 400.0
_CENTER = _VIEWBOX_SIZE / 2
_MAX_RADIUS = 150.0
_RING_COUNT = 4
_DIRECTION_LABEL_RADIUS = _MAX_RADIUS + 24.0
_RING_STROKE_COLOR = theme.BORDER
_RING_LABEL_COLOR = theme.TEXT_MUTED
_SILHOUETTE_RADIUS = 32.0
_SILHOUETTE_COLOR = "#2A3142"

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


def _build_silhouette_rings() -> list[list[tuple[float, float]]]:
    """Flatten + normalize the app's real region geometry into a small,
    centered silhouette (viewBox units) -- reused geometry, not a new
    placeholder shape.
    """
    all_points = [point for rings in REGION_RINGS.values() for ring in rings for point in ring]
    min_x = min(p[0] for p in all_points)
    max_x = max(p[0] for p in all_points)
    min_y = min(p[1] for p in all_points)
    max_y = max(p[1] for p in all_points)
    width = max_x - min_x
    height = max_y - min_y
    scale = (2 * _SILHOUETTE_RADIUS) / max(width, height)
    offset_x = _CENTER - (width * scale) / 2
    offset_y = _CENTER - (height * scale) / 2

    normalized: list[list[tuple[float, float]]] = []
    for rings in REGION_RINGS.values():
        for ring in rings:
            if not ring:
                continue
            normalized.append(
                [((x - min_x) * scale + offset_x, (y - min_y) * scale + offset_y) for x, y in ring]
            )
    return normalized


#: Computed once at import time -- the underlying region geometry never
#: changes at runtime, so there's no reason to rebuild this per redraw.
_SILHOUETTE_RINGS = _build_silhouette_rings()


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
    """The circular radar visual: rings, directions, silhouette, targets."""

    def __init__(self, max_range_km: float = 200.0) -> None:
        self._max_range_km = max_range_km
        self._targets: list[CompassTarget] = []
        self._render_size = 320.0

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

    # --- Internal --------------------------------------------------------

    def _scale_factor(self) -> float:
        return self._render_size / _VIEWBOX_SIZE

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

        # Silhouette (normalized to viewBox units above; scale to pixels here).
        for ring in _SILHOUETTE_RINGS:
            if not ring:
                continue
            elements: list[canvas.Path.PathElement] = []
            x0, y0 = self._to_pixels(ring[0])
            elements.append(canvas.Path.MoveTo(x0, y0))
            for point in ring[1:]:
                px, py = self._to_pixels(point)
                elements.append(canvas.Path.LineTo(px, py))
            elements.append(canvas.Path.Close())
            shapes.append(canvas.Path(elements, paint=ft.Paint(color=_SILHOUETTE_COLOR, style=ft.PaintingStyle.FILL)))

        # Target markers.
        for target in self._targets:
            distance_fraction = min(target.distance_km / self._max_range_km, 1.0) if self._max_range_km else 0.0
            radius = _MAX_RADIUS * distance_fraction
            px, py = self._to_pixels(_bearing_to_xy(target.bearing_degrees, radius))
            color = _TARGET_COLOR.get(target.threat_type, theme.TEXT_MUTED)
            shapes.extend(_target_marker_shapes(px, py, color))

        return shapes

    def _build_labels(self) -> list[ft.Control]:
        controls: list[ft.Control] = []

        for label, bearing in _DIRECTIONS:
            px, py = self._to_pixels(_bearing_to_xy(bearing, _DIRECTION_LABEL_RADIUS))
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
            px, py = self._to_pixels(_bearing_to_xy(target.bearing_degrees, radius))
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
