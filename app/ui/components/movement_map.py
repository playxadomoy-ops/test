"""
Map for the "Рух загроз" (threat movement) tab.

Draws the same real Ukraine geometry as the main map (``app.ukraine_geo``),
but statically colored (this map does not show alert status) with an
overlay of the currently active :class:`ThreatMovement` entries:

  * If a movement has BOTH an origin and a destination explicitly stated
    in its source message, an arrow is drawn between them and the threat
    icon sits at the midpoint.
  * If only one place is known, the icon sits at that single point with
    no arrow (per spec: never draw an arrow unless the direction is
    explicit).
  * If no place at all could be resolved from the message, nothing is
    drawn on the map for it (it still appears in the side list).

Rendering mirrors ``UkraineMap``'s approach: the base map and the arrows
are native ``flet.canvas`` shapes (``Path``/``Line``), not an SVG image --
this avoids depending on the Flutter SVG renderer at all. Threat icons
and place-name labels are a ``ft.Stack`` of real ``ft.Text`` controls
layered on top (positioned with pixel left/top, computed from the same
viewBox-to-pixel scale used for hit-testing on the main map) -- emoji
render natively and reliably this way.
"""

from __future__ import annotations

import math
from typing import Callable, Optional

import flet as ft
import flet.canvas as canvas

from app.data.oblast_districts import DISTRICT_RINGS
from app.data.oblast_settlements import CHERKASY_OBLAST_SETTLEMENTS, KYIV_OBLAST_SETTLEMENTS
from app.models.alert_models import Region
from app.models.movement_models import ThreatMovement
from app.ui.theme import colors as theme
from app.ui.theme.colors import CARD_PADDING, PAGE_PADDING
from app.ukraine_geo import (
    REGION_RINGS,
    VIEWBOX_HEIGHT,
    VIEWBOX_WIDTH,
    project_lat_lon,
    region_at_point,
    regions_bounding_box,
)

OnMovementTapped = Callable[[ThreatMovement], None]
OnRegionTapped = Callable[[Region], None]

_MAP_ASPECT_RATIO = VIEWBOX_WIDTH / VIEWBOX_HEIGHT
_MIN_RENDER_WIDTH = 240.0
_MAX_RENDER_HEIGHT = 640.0
#: Extra breathing room around a cropped selection's bounding box (as a
#: fraction of its width/height) so the oblast's own border isn't drawn
#: flush against the card's edge.
_CROP_MARGIN_RATIO = 0.10
#: Slightly tighter margin + taller cap used ONLY when the crop includes
#: a settlement-special-case oblast (Kyiv/Cherkasy) -- requested so their
#: settlement dots/labels have more room and overlap less. Every other
#: oblast keeps using the constants above, unchanged.
_SETTLEMENT_CROP_MARGIN_RATIO = 0.015
_SETTLEMENT_MAX_RENDER_HEIGHT = 900.0
#: Minimum pixel distance between two settlement labels (not dots -- the
#: dot is always drawn for every settlement) before the later one is
#: drawn as a dot only, no text. Keeps "all settlements visible" (every
#: dot is always there, tappable-adjacent) while cutting down label
#: clutter at this zoom level, per the "reduce overlap" request.
_SETTLEMENT_LABEL_MIN_SPACING = 26.0
_ARROW_COLOR = "#38BDF8"
_MAP_NEUTRAL_FILL = "#1C2333"
_MAP_STROKE_WIDTH = 1.0
_ARROW_STROKE_WIDTH = 2.5
_ARROWHEAD_LENGTH = 8.0
_ARROWHEAD_ANGLE = math.radians(28)

#: Special-case, display-only settlement gazetteers -- see
#: app/data/oblast_settlements.py for the source/limitations. Deliberately
#: only these two oblasts, per the Threat Movement redesign: every other
#: oblast keeps its exact current cropped-map behavior (region outline +
#: movement pins only, no settlement dots).
_OBLAST_SETTLEMENTS: dict[Region, dict[str, tuple[float, float]]] = {
    Region.KYIV_OBLAST: KYIV_OBLAST_SETTLEMENTS,
    Region.CHERKASY: CHERKASY_OBLAST_SETTLEMENTS,
}
#: Raion-center towns (see app/data/oblast_districts.py's DISTRICT_RINGS
#: keys) -- labeled first when decluttering overlapping labels, since
#: they're the most locally-significant settlements in each oblast and
#: should stay readable even when nearby smaller villages have to drop
#: their label (their dot is still always drawn either way).
_PRIORITY_SETTLEMENTS: dict[Region, frozenset[str]] = {
    Region.KYIV_OBLAST: frozenset(
        {"бориспіль", "біла церква", "вишгород", "обухів", "бровари", "буча", "фастів"}
    ),
    Region.CHERKASY: frozenset({"звенигородка", "умань", "золотоноша", "черкаси"}),
}
_SETTLEMENT_DOT_RADIUS = 2.2
_SETTLEMENT_DOT_COLOR = theme.TEXT_MUTED
_SETTLEMENT_LABEL_SIZE = 9.0
#: Internal raion ("district") boundary lines, drawn only inside a
#: settlement-special-case oblast -- same purple as that oblast's own
#: focused-mode outline, but thinner/unfilled so it reads as an interior
#: subdivision, not a second oblast border.
_DISTRICT_STROKE_COLOR = theme.REGION_WATCHED_STROKE
_DISTRICT_STROKE_WIDTH = 0.8


class MovementMap(ft.Container):
    """The map card shown at the top of the "Рух загроз" tab.

    Supports two display modes, switched via :meth:`set_selected_regions`:
      * no selection -> the whole country, as before;
      * one or more selected oblasts -> only those oblasts' geometry is
        drawn (the rest of Ukraine is not drawn at all, not merely
        dimmed), zoomed so the selection's own bounding box fills most
        of the available width/height while preserving its true aspect
        ratio -- the card's height is recomputed to match the selected
        geometry's shape rather than staying locked to the whole
        country's aspect ratio.
    """

    def __init__(
        self,
        on_movement_tap: Optional[OnMovementTapped] = None,
        on_region_tap: Optional[OnRegionTapped] = None,
    ) -> None:
        """Build the map card; callbacks fire for a tapped movement icon / oblast."""
        self._on_movement_tap = on_movement_tap
        self._on_region_tap = on_region_tap
        self._movements: list[ThreatMovement] = []
        self._selected_regions: set[Region] = set()
        self._page_width: float = 340.0 + (2 * PAGE_PADDING) + (2 * CARD_PADDING)

        # Current viewBox-unit -> pixel transform: pixel = (point - origin) * scale.
        # Recomputed by _compute_layout() from page width + selected regions.
        self._origin_x: float = 0.0
        self._origin_y: float = 0.0
        self._scale: float = 1.0
        self._render_width: float = 340.0
        self._render_height: float = 340.0 / _MAP_ASPECT_RATIO
        self._compute_layout()

        self._canvas = canvas.Canvas(
            shapes=self._build_map_and_arrow_shapes(),
            width=self._render_width,
            height=self._render_height,
        )
        self._gesture_detector = ft.GestureDetector(
            content=self._canvas,
            width=self._render_width,
            height=self._render_height,
            on_tap_up=self._handle_map_tap_up,
        )
        self._overlay_stack = ft.Stack(
            controls=[self._gesture_detector],
            width=self._render_width,
            height=self._render_height,
            animate_size=ft.Animation(320, ft.AnimationCurve.EASE_OUT),
        )
        self._map_frame = ft.Container(
            content=self._overlay_stack,
            alignment=ft.alignment.center,
            width=self._page_width - (2 * PAGE_PADDING) - (2 * CARD_PADDING),
            animate_size=ft.Animation(320, ft.AnimationCurve.EASE_OUT),
        )
        self._empty_hint = ft.Text(
            "Активних повідомлень з відомим напрямком немає.",
            size=12,
            color=theme.TEXT_MUTED,
        )

        super().__init__(
            padding=CARD_PADDING,
            border_radius=20,
            bgcolor=theme.SURFACE_ELEVATED,
            border=ft.border.all(1, theme.BORDER),
            content=ft.Column(
                spacing=10,
                controls=[
                    ft.Text(
                        "Карта руху загроз",
                        size=14,
                        weight=ft.FontWeight.W_600,
                        color=theme.TEXT_SECONDARY,
                    ),
                    self._map_frame,
                    self._empty_hint,
                ],
            ),
        )

    # --- Lifecycle --------------------------------------------------

    def did_mount(self) -> None:
        """Size the map correctly as soon as the real page width is known."""
        if self.page is not None and self.page.width:
            self.resize(float(self.page.width))

    # --- Public API ---------------------------------------------------

    def resize(self, page_width: float) -> None:
        """Recompute the map's pixel size from the current page width."""
        self._page_width = page_width
        self._apply_layout()

    def update_movements(self, movements: list[ThreatMovement]) -> None:
        """Replace the set of currently active movements and redraw."""
        self._movements = movements
        self._rebuild_overlay()
        if self.page is not None:
            self._overlay_stack.update()
            self._empty_hint.update()

    def set_selected_regions(self, regions: set[Region]) -> None:
        """Show only ``regions`` (zoomed to fit), or the whole country if empty.

        This fully replaces the previous "highlight while showing all of
        Ukraine" behavior: a non-empty selection now hides every other
        oblast and re-shapes the map to that selection's own bounding
        box, per the Threat Movement redesign.
        """
        if regions == self._selected_regions:
            return
        self._selected_regions = set(regions)
        self._apply_layout()

    # --- Layout / transform ------------------------------------------------

    def _compute_layout(self) -> None:
        """Recompute origin/scale/render size for the current page width + selection."""
        available_width = max(
            _MIN_RENDER_WIDTH, self._page_width - (2 * PAGE_PADDING) - (2 * CARD_PADDING)
        )

        if not self._selected_regions:
            self._origin_x = 0.0
            self._origin_y = 0.0
            self._scale = available_width / VIEWBOX_WIDTH
            self._render_width = available_width
            self._render_height = available_width / _MAP_ASPECT_RATIO
            return

        min_x, min_y, max_x, max_y = regions_bounding_box(self._selected_regions)
        box_w = max(1.0, max_x - min_x)
        box_h = max(1.0, max_y - min_y)

        is_settlement_case = bool(self._selected_regions & _OBLAST_SETTLEMENTS.keys())
        margin_ratio = _SETTLEMENT_CROP_MARGIN_RATIO if is_settlement_case else _CROP_MARGIN_RATIO
        max_render_height = _SETTLEMENT_MAX_RENDER_HEIGHT if is_settlement_case else _MAX_RENDER_HEIGHT

        margin_x = box_w * margin_ratio
        margin_y = box_h * margin_ratio
        min_x -= margin_x
        min_y -= margin_y
        box_w += 2 * margin_x
        box_h += 2 * margin_y

        # "Contain" fit: the selection's own aspect ratio is preserved by
        # shaping the card to it (not forced into the whole-country
        # ratio), while still respecting a sane max height on very wide
        # multi-oblast selections and the available card width on very
        # tall/narrow ones.
        scale = min(available_width / box_w, max_render_height / box_h)

        self._origin_x = min_x
        self._origin_y = min_y
        self._scale = scale
        self._render_width = box_w * scale
        self._render_height = box_h * scale

    def _apply_layout(self) -> None:
        """Recompute layout and push the new size/shapes to the controls."""
        self._compute_layout()
        self._canvas.width = self._render_width
        self._canvas.height = self._render_height
        self._gesture_detector.width = self._render_width
        self._gesture_detector.height = self._render_height
        self._overlay_stack.width = self._render_width
        self._overlay_stack.height = self._render_height
        self._map_frame.width = max(
            _MIN_RENDER_WIDTH, self._page_width - (2 * PAGE_PADDING) - (2 * CARD_PADDING)
        )
        self._rebuild_overlay()
        if self.page is not None:
            self._map_frame.update()
            self._overlay_stack.update()
            self._empty_hint.update()

    def _to_pixels(self, point: tuple[float, float]) -> tuple[float, float]:
        """Project one viewBox-unit point to pixels under the current transform."""
        return (point[0] - self._origin_x) * self._scale, (point[1] - self._origin_y) * self._scale

    def _to_viewbox(self, pixel: tuple[float, float]) -> tuple[float, float]:
        """Inverse of :meth:`_to_pixels`, used for tap hit-testing."""
        return pixel[0] / self._scale + self._origin_x, pixel[1] / self._scale + self._origin_y

    # --- Drawing --------------------------------------------------------

    def _visible_regions(self) -> "list[Region]":
        """Regions actually drawn: the selection, or all 27 if none is set."""
        return list(self._selected_regions) if self._selected_regions else list(Region)

    def _build_map_and_arrow_shapes(self) -> list[canvas.Shape]:
        """Build the base map (selection-aware) plus any movement arrows."""
        shapes: list[canvas.Shape] = []
        is_focused = bool(self._selected_regions)

        for region in self._visible_regions():
            elements = _rings_to_path_elements(REGION_RINGS[region], self._origin_x, self._origin_y, self._scale)
            if not elements:
                continue
            shapes.append(
                canvas.Path(elements, paint=ft.Paint(color=_MAP_NEUTRAL_FILL, style=ft.PaintingStyle.FILL))
            )
            shapes.append(
                canvas.Path(
                    elements,
                    paint=ft.Paint(
                        color=theme.REGION_WATCHED_STROKE if is_focused else theme.BORDER,
                        style=ft.PaintingStyle.STROKE,
                        stroke_width=_MAP_STROKE_WIDTH * (1.6 if is_focused else 1.0),
                    ),
                )
            )

        for movement in self._movements:
            if not movement.has_direction:
                continue
            x1, y1 = self._to_pixels(movement.origin_point)  # type: ignore[arg-type]
            x2, y2 = self._to_pixels(movement.destination_point)  # type: ignore[arg-type]
            shapes.extend(_arrow_shapes(x1, y1, x2, y2, _ARROW_COLOR))

        # Special case (Kyiv Oblast / Cherkasy Oblast only, see
        # app/data/oblast_districts.py): draw each raion's outline inside
        # that oblast, stroke-only (no fill, so the oblast's own fill
        # still shows through), same purple used for the oblast's own
        # focused-mode border but thinner. Every other oblast is
        # unaffected -- only ever added for a region the user selected.
        for region in self._selected_regions:
            districts = DISTRICT_RINGS.get(region)
            if not districts:
                continue
            for rings in districts.values():
                elements = _rings_to_path_elements(rings, self._origin_x, self._origin_y, self._scale)
                if not elements:
                    continue
                shapes.append(
                    canvas.Path(
                        elements,
                        paint=ft.Paint(
                            color=_DISTRICT_STROKE_COLOR,
                            style=ft.PaintingStyle.STROKE,
                            stroke_width=_DISTRICT_STROKE_WIDTH,
                        ),
                    )
                )

        # Special case (Kyiv Oblast / Cherkasy Oblast only, see
        # app/data/oblast_settlements.py): plot every known settlement in
        # that oblast as a small dot + label, same canvas/visual style as
        # everything else on this map. Every other oblast is unaffected --
        # this only ever adds to a region the user actively selected.
        for region in self._selected_regions:
            settlements = _OBLAST_SETTLEMENTS.get(region)
            if not settlements:
                continue
            priority_names = _PRIORITY_SETTLEMENTS.get(region, frozenset())

            # Priority (raion-center) settlements are placed first so
            # they keep their label even when smaller neighbors have to
            # give theirs up -- see _PRIORITY_SETTLEMENTS above.
            ordered_names = sorted(
                settlements.keys(), key=lambda n: (n not in priority_names, n)
            )

            placed_label_points: list[tuple[float, float]] = []
            min_spacing_sq = _SETTLEMENT_LABEL_MIN_SPACING**2

            for name in ordered_names:
                lat, lon = settlements[name]
                x, y = self._to_pixels(project_lat_lon(lat, lon))
                shapes.append(
                    canvas.Circle(
                        x, y, _SETTLEMENT_DOT_RADIUS,
                        paint=ft.Paint(color=_SETTLEMENT_DOT_COLOR, style=ft.PaintingStyle.FILL),
                    )
                )

                too_close = any(
                    (x - px) ** 2 + (y - py) ** 2 < min_spacing_sq for px, py in placed_label_points
                )
                if too_close:
                    continue  # dot only -- every settlement stays visible, just not its label

                placed_label_points.append((x, y))
                shapes.append(
                    canvas.Text(
                        x + 4,
                        y - 5,
                        name.capitalize(),
                        style=ft.TextStyle(size=_SETTLEMENT_LABEL_SIZE, color=_SETTLEMENT_DOT_COLOR),
                    )
                )

        return shapes

    def _rebuild_overlay(self) -> None:
        """Rebuild the canvas shapes and icon/label markers for the current movements."""
        self._canvas.shapes = self._build_map_and_arrow_shapes()

        controls: list[ft.Control] = [self._gesture_detector]
        has_any_plotted = any(m.has_any_location for m in self._movements)

        for movement in self._movements:
            controls.extend(self._build_markers_for(movement))

        self._overlay_stack.controls = controls
        self._empty_hint.visible = not has_any_plotted

    def _build_markers_for(self, movement: ThreatMovement) -> list[ft.Control]:
        """Build the pin label(s) and threat icon for one movement, if it has a location."""
        markers: list[ft.Control] = []

        if movement.has_direction:
            ox, oy = self._to_pixels(movement.origin_point)  # type: ignore[arg-type]
            dx, dy = self._to_pixels(movement.destination_point)  # type: ignore[arg-type]
            if movement.origin_name:
                markers.append(self._pin_label(movement.origin_name, ox, oy))
            if movement.destination_name:
                markers.append(self._pin_label(movement.destination_name, dx, dy))
            mid_x, mid_y = (ox + dx) / 2, (oy + dy) / 2
            markers.append(self._threat_icon(movement, mid_x, mid_y))
        elif movement.has_any_location:
            point = movement.origin_point or movement.destination_point
            name = movement.origin_name or movement.destination_name
            px, py = self._to_pixels(point)  # type: ignore[arg-type]
            if name:
                markers.append(self._pin_label(name, px, py))
            markers.append(self._threat_icon(movement, px, py))

        return markers

    def _pin_label(self, name: str, x: float, y: float) -> ft.Control:
        """A small "📍 name" label anchored near a point."""
        return ft.Container(
            left=max(0.0, x - 40),
            top=max(0.0, y - 30),
            content=ft.Container(
                padding=ft.padding.symmetric(horizontal=6, vertical=2),
                border_radius=6,
                bgcolor=ft.Colors.with_opacity(0.85, theme.BACKGROUND),
                content=ft.Text(f"📍 {name}", size=10, color=theme.TEXT_PRIMARY),
            ),
        )

    def _threat_icon(self, movement: ThreatMovement, x: float, y: float) -> ft.Control:
        """The tappable emoji icon for one movement, centered at (x, y)."""
        icon_text = ft.Text(movement.threat_type.icon, size=24)
        return ft.Container(
            left=max(0.0, x - 16),
            top=max(0.0, y - 16),
            content=ft.GestureDetector(
                content=icon_text,
                on_tap_up=lambda e, m=movement: self._handle_tap(m),
            ),
        )

    def _handle_tap(self, movement: ThreatMovement) -> None:
        if self._on_movement_tap is not None:
            self._on_movement_tap(movement)

    def _handle_map_tap_up(self, e: ft.TapEvent) -> None:
        """Convert a tap's pixel position (in the current crop) into a Region."""
        if self._on_region_tap is None:
            return
        point = self._to_viewbox((e.local_x, e.local_y))
        region = region_at_point(point)
        if region is not None:
            self._on_region_tap(region)


def _rings_to_path_elements(
    rings: list[list[tuple[float, float]]], origin_x: float, origin_y: float, scale: float
) -> list[canvas.Path.PathElement]:
    """Convert one region's viewBox-unit point rings into scaled Path elements.

    ``origin_x``/``origin_y`` are subtracted first so this works for both
    the whole-country view (origin 0,0) and a cropped/zoomed selection
    (origin = the selection's own padded bounding-box corner).
    """
    elements: list[canvas.Path.PathElement] = []
    for ring in rings:
        if not ring:
            continue
        x0, y0 = ring[0]
        elements.append(canvas.Path.MoveTo((x0 - origin_x) * scale, (y0 - origin_y) * scale))
        for x, y in ring[1:]:
            elements.append(canvas.Path.LineTo((x - origin_x) * scale, (y - origin_y) * scale))
        elements.append(canvas.Path.Close())
    return elements


def _arrow_shapes(x1: float, y1: float, x2: float, y2: float, color: str) -> list[canvas.Shape]:
    """Build a dashed line + filled triangular arrowhead pointing at (x2, y2)."""
    angle = math.atan2(y2 - y1, x2 - x1)

    back1_x = x2 - _ARROWHEAD_LENGTH * math.cos(angle - _ARROWHEAD_ANGLE)
    back1_y = y2 - _ARROWHEAD_LENGTH * math.sin(angle - _ARROWHEAD_ANGLE)
    back2_x = x2 - _ARROWHEAD_LENGTH * math.cos(angle + _ARROWHEAD_ANGLE)
    back2_y = y2 - _ARROWHEAD_LENGTH * math.sin(angle + _ARROWHEAD_ANGLE)

    line = canvas.Line(
        x1, y1, x2, y2,
        paint=ft.Paint(
            color=color,
            stroke_width=_ARROW_STROKE_WIDTH,
            style=ft.PaintingStyle.STROKE,
            stroke_dash_pattern=[7, 5],
            stroke_cap=ft.StrokeCap.ROUND,
        ),
    )
    head = canvas.Path(
        [
            canvas.Path.MoveTo(x2, y2),
            canvas.Path.LineTo(back1_x, back1_y),
            canvas.Path.LineTo(back2_x, back2_y),
            canvas.Path.Close(),
        ],
        paint=ft.Paint(color=color, style=ft.PaintingStyle.FILL),
    )
    return [line, head]
