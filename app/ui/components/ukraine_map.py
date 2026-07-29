"""
Real geographic map of Ukraine's 27 regions.

Unlike a grid of labeled buttons, this draws the actual oblast borders
(``app.ukraine_geo``) as native vector shapes on a ``flet.canvas.Canvas``,
colored per-region from live alert state, and resolves taps to a region
with point-in-polygon testing against that same border data -- so the
tap target always matches what is drawn, at any screen size.

Rendering strategy (why it's built this way, for future maintainers):
  * Each region is drawn as a ``flet.canvas.Path`` shape built directly
    from its polygon points (one ``MoveTo``/``LineTo``/``Close`` per
    ring), rather than as an SVG image. This avoids depending on the
    Flutter SVG renderer being able to auto-detect/parse a large inline
    base64 image -- Canvas/``CustomPainter`` is a core, first-class Flet
    rendering path with no format-sniffing involved.
  * A bare Canvas alone cannot tell us *which* region was tapped, since
    its shapes don't receive individual tap events. Instead, a
    ``GestureDetector`` sits on top and reports the tap's pixel
    position; that position is converted into this module's own
    coordinate space and matched against ``REGION_RINGS`` using the
    ray-casting algorithm (unchanged from the previous SVG-based
    version -- only the drawing technique changed, not the geometry or
    the hit-testing math).
  * That conversion needs to know the map's *actual* rendered pixel
    size. Flet has no per-control resize callback, so this control
    fixes its own width/height explicitly (kept in sync from
    ``main.py`` via ``page.on_resized``) rather than guessing -- this
    is what keeps the hit-testing exact on any phone size/rotation
    without any hardcoded screen coordinates.
"""

from __future__ import annotations

from typing import Callable, Optional

import flet as ft
import flet.canvas as canvas

from app.data.oblast_districts import DISTRICT_RINGS
from app.data.oblast_settlements import CHERKASY_OBLAST_SETTLEMENTS, KYIV_OBLAST_SETTLEMENTS
from app.models.alert_models import Region, RegionState
from app.ui.components.label_layout import place_labels, scaled_settlement_sizes
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

OnRegionTapped = Callable[[Region], None]

_MAP_ASPECT_RATIO = VIEWBOX_WIDTH / VIEWBOX_HEIGHT
_MIN_RENDER_WIDTH = 240.0
_STROKE_WIDTH = 1.1

# --- Kyiv Oblast / Cherkasy Oblast focused-zoom special case -----------
# Tapping either of these two oblasts on the main map (unlike every other
# oblast, which keeps its exact previous "show info dialog" tap
# behavior) crops/zooms the same real geometry already used for this
# purpose on the "Рух загроз" map (see app/ui/components/movement_map.py)
# to just that one oblast, and layers on its settlement dots/labels and
# internal raion borders. Reusing that same data/architecture, not a
# second copy of it, per the project's "reuse existing classes" rule.
_FOCUSABLE_REGIONS: frozenset[Region] = frozenset({Region.KYIV_OBLAST, Region.CHERKASY})
_OBLAST_SETTLEMENTS: dict[Region, dict[str, tuple[float, float]]] = {
    Region.KYIV_OBLAST: KYIV_OBLAST_SETTLEMENTS,
    Region.CHERKASY: CHERKASY_OBLAST_SETTLEMENTS,
}
_PRIORITY_SETTLEMENTS: dict[Region, frozenset[str]] = {
    Region.KYIV_OBLAST: frozenset(
        {"бориспіль", "біла церква", "вишгород", "обухів", "бровари", "буча", "фастів"}
    ),
    Region.CHERKASY: frozenset({"звенигородка", "умань", "золотоноша", "черкаси"}),
}
#: Tight margin + tall cap for the focused view, same reasoning as
#: movement_map.py's equivalents -- fills "almost the entire available
#: map area", per the Map Improvements spec.
_FOCUS_CROP_MARGIN_RATIO = 0.02
_FOCUS_MAX_RENDER_HEIGHT = 900.0
_LEADER_LINE_COLOR = theme.TEXT_MUTED
_DISTRICT_STROKE_WIDTH = 0.8


class UkraineMap(ft.Container):
    """The map card shown at the top of the main layout."""

    def __init__(self, on_region_tap: Optional[OnRegionTapped] = None) -> None:
        """Build the map card; ``on_region_tap`` is called with the tapped Region."""
        self._on_region_tap = on_region_tap
        self._region_states: dict[Region, RegionState] = {}
        self._page_width: float = 340.0 + (2 * PAGE_PADDING) + (2 * CARD_PADDING)
        #: None = whole-country view (unchanged, existing behavior). Set
        #: to Kyiv Oblast / Cherkasy Oblast to crop/zoom to just that
        #: oblast -- see _FOCUSABLE_REGIONS above.
        self._focused_region: Optional[Region] = None

        # Current viewBox-unit -> pixel transform: pixel = (point - origin) * scale.
        # Recomputed by _compute_layout() from page width + focus state.
        self._origin_x: float = 0.0
        self._origin_y: float = 0.0
        self._scale: float = 1.0
        self._render_width: float = 340.0
        self._render_height: float = 340.0 / _MAP_ASPECT_RATIO

        #: Recomputed alongside ``self._scale`` in ``_compute_layout()`` --
        #: see ``scaled_settlement_sizes()`` (label_layout.py) for why these
        #: aren't fixed constants.
        self._settlement_dot_radius: float = 2.2
        self._settlement_label_size: float = 9.0

        #: Per-region transformed border geometry, cached against the
        #: transform it was computed for (``_elements_cache_key``).
        #: ``update_region_states()`` -- by far the most frequent redraw,
        #: firing on every Telegram message / API refresh -- only ever
        #: changes fill *colors*, never the geometry itself. Without this
        #: cache, every one of those redraws re-ran the full point-by-
        #: point (x - origin) * scale transform for all 27 oblasts' borders
        #: (thousands of coordinate pairs) purely to reproduce the exact
        #: same numbers as last time. The cache is invalidated (cleared)
        #: automatically whenever the transform actually changes -- see
        #: ``_build_shapes()`` -- so resize/focus changes still redraw
        #: correctly; only the truly-redundant recomputation is skipped.
        self._region_elements_cache: dict[Region, list[canvas.Path.PathElement]] = {}
        self._elements_cache_key: Optional[tuple[float, float, float]] = None

        #: Fingerprint of the ``is_active`` flags last actually painted --
        #: the only part of ``RegionState`` that affects this map's colors
        #: (see ``_build_shapes``). ``update_region_states()`` is called on
        #: every snapshot change, which (via the once-a-second decay tick)
        #: can fire far more often than any region's real state changes.
        #: Comparing against this fingerprint lets a no-op update skip
        #: rebuilding all 27 region shapes and, more importantly, skip the
        #: canvas repaint -- with no change to what gets drawn whenever
        #: something *did* actually change.
        self._painted_active_fingerprint: Optional[tuple[bool, ...]] = None

        self._compute_layout()

        self._canvas = canvas.Canvas(
            shapes=self._build_shapes(),
            width=self._render_width,
            height=self._render_height,
        )

        self._gesture_detector = ft.GestureDetector(
            content=self._canvas,
            width=self._render_width,
            height=self._render_height,
            on_tap_up=self._handle_tap_up,
        )

        self._map_frame = ft.Container(
            content=self._gesture_detector,
            alignment=ft.alignment.center,
            animate_size=theme.ANIM_SLOW,
        )

        self._back_button = ft.TextButton(
            text="← Вся карта України",
            icon=ft.Icons.ARROW_BACK_ROUNDED,
            visible=False,
            on_click=lambda e: self._exit_focus(),
        )
        self._map_title = ft.Text(
            "Карта тривог",
            size=14,
            weight=ft.FontWeight.W_600,
            color=theme.TEXT_SECONDARY,
        )

        super().__init__(
            padding=CARD_PADDING,
            border_radius=theme.RADIUS_LG,
            bgcolor=theme.SURFACE_ELEVATED,
            border=ft.border.all(1, theme.BORDER),
            shadow=theme.elevation_shadow(),
            content=ft.Column(
                spacing=10,
                controls=[
                    ft.Row(
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        controls=[self._map_title, self._back_button],
                    ),
                    self._map_frame,
                    self._build_legend(),
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
        """Recompute the map's pixel size from the current page width.

        Called once on mount and again from ``main.py``'s
        ``page.on_resized`` handler, so the map (and therefore the tap
        hit-testing) always matches the actual rendered size, on any
        Android phone and after rotation.
        """
        self._page_width = page_width
        self._apply_layout()

    def update_region_states(self, region_states: dict[Region, RegionState]) -> None:
        """Redraw the map with fresh per-region colors.

        Skips the rebuild + canvas repaint entirely when none of the
        regions' ``is_active`` flags actually changed since the last
        call -- see ``_painted_active_fingerprint``'s docstring. The
        stored region_states dict is still always kept up to date so a
        subsequent focus/resize redraw (which calls ``_build_shapes``
        directly) sees the latest data.
        """
        self._region_states = region_states
        fingerprint = tuple(
            bool(region_states.get(region) and region_states[region].is_active) for region in Region
        )
        if fingerprint == self._painted_active_fingerprint:
            return
        self._painted_active_fingerprint = fingerprint
        self._canvas.shapes = self._build_shapes()
        if self.page is not None:
            self._canvas.update()

    # --- Focus (Kyiv Oblast / Cherkasy Oblast zoom) ------------------------

    def _enter_focus(self, region: Region) -> None:
        """Crop/zoom to just ``region`` -- only ever called for a focusable oblast."""
        if self._focused_region == region:
            return
        self._focused_region = region
        self._back_button.visible = True
        self._map_title.value = f"Карта тривог -- {region.value}"
        self._apply_layout()
        if self.page is not None:
            self._back_button.update()
            self._map_title.update()

    def _exit_focus(self) -> None:
        """Return to the whole-country view."""
        if self._focused_region is None:
            return
        self._focused_region = None
        self._back_button.visible = False
        self._map_title.value = "Карта тривог"
        self._apply_layout()
        if self.page is not None:
            self._back_button.update()
            self._map_title.update()

    # --- Layout / transform ------------------------------------------------

    def _compute_layout(self) -> None:
        """Recompute origin/scale/render size for the current page width + focus."""
        available_width = max(
            _MIN_RENDER_WIDTH, self._page_width - (2 * PAGE_PADDING) - (2 * CARD_PADDING)
        )

        if self._focused_region is None:
            self._origin_x = 0.0
            self._origin_y = 0.0
            self._scale = available_width / VIEWBOX_WIDTH
            self._render_width = available_width
            self._render_height = available_width / _MAP_ASPECT_RATIO
            self._settlement_dot_radius, self._settlement_label_size = scaled_settlement_sizes(self._scale)
            return

        min_x, min_y, max_x, max_y = regions_bounding_box({self._focused_region})
        box_w = max(1.0, max_x - min_x)
        box_h = max(1.0, max_y - min_y)

        margin_x = box_w * _FOCUS_CROP_MARGIN_RATIO
        margin_y = box_h * _FOCUS_CROP_MARGIN_RATIO
        min_x -= margin_x
        min_y -= margin_y
        box_w += 2 * margin_x
        box_h += 2 * margin_y

        # "Contain" fit, same as the movement map's crop: the oblast's own
        # aspect ratio is preserved and it fills almost the entire
        # available card area, per the Map Improvements spec.
        scale = min(available_width / box_w, _FOCUS_MAX_RENDER_HEIGHT / box_h)

        self._origin_x = min_x
        self._origin_y = min_y
        self._scale = scale
        self._render_width = box_w * scale
        self._render_height = box_h * scale
        self._settlement_dot_radius, self._settlement_label_size = scaled_settlement_sizes(self._scale)

    def _apply_layout(self) -> None:
        """Recompute layout and push the new size/shapes to the controls."""
        self._compute_layout()
        self._canvas.width = self._render_width
        self._canvas.height = self._render_height
        self._canvas.shapes = self._build_shapes()
        self._gesture_detector.width = self._render_width
        self._gesture_detector.height = self._render_height
        if self.page is not None:
            self._map_frame.update()
            self._canvas.update()
            self._gesture_detector.update()

    def _to_pixels(self, point: tuple[float, float]) -> tuple[float, float]:
        """Project one viewBox-unit point to pixels under the current transform."""
        return (point[0] - self._origin_x) * self._scale, (point[1] - self._origin_y) * self._scale

    # --- Drawing --------------------------------------------------------

    def _visible_regions(self) -> "list[Region]":
        """Regions actually drawn: just the focused oblast, or all 27."""
        return [self._focused_region] if self._focused_region is not None else list(Region)

    def _build_shapes(self) -> list[canvas.Shape]:
        """Build one fill + one stroke Path shape per visible region, in pixel space.

        When focused on Kyiv/Cherkasy Oblast, this still colors that
        single oblast by its real alert state (unlike the static-colored
        "Рух загроз" map) -- only the crop/zoom and the settlement/
        district overlay are the special case here, per "preserve the
        current dark theme ... preserve interaction".
        """
        shapes: list[canvas.Shape] = []
        is_focused = self._focused_region is not None

        # Invalidate the cached per-region geometry only when the actual
        # viewBox->pixel transform changed (resize/rotation/focus) -- see
        # the cache's declaration in __init__ for why this matters.
        cache_key = (self._origin_x, self._origin_y, self._scale)
        if cache_key != self._elements_cache_key:
            self._region_elements_cache = {}
            self._elements_cache_key = cache_key

        for region in self._visible_regions():
            state = self._region_states.get(region)
            is_active = bool(state and state.is_active)
            fill_color = theme.REGION_ACTIVE_COLOR if is_active else theme.REGION_INACTIVE_COLOR

            elements = self._region_elements_cache.get(region)
            if elements is None:
                elements = _rings_to_path_elements(
                    REGION_RINGS[region], self._origin_x, self._origin_y, self._scale
                )
                self._region_elements_cache[region] = elements
            if not elements:
                continue

            shapes.append(
                canvas.Path(
                    elements,
                    paint=ft.Paint(color=fill_color, style=ft.PaintingStyle.FILL),
                )
            )
            shapes.append(
                canvas.Path(
                    elements,
                    paint=ft.Paint(
                        # Purple border while focused (matches the same
                        # special-case treatment already used on the
                        # movement map for a user-selected oblast),
                        # otherwise the normal thin region stroke.
                        color=theme.REGION_WATCHED_STROKE if is_focused else theme.REGION_STROKE,
                        style=ft.PaintingStyle.STROKE,
                        stroke_width=_STROKE_WIDTH * (1.8 if is_focused else 1.0),
                    ),
                )
            )

        if self._focused_region is not None:
            shapes.extend(self._build_district_shapes(self._focused_region))
            shapes.extend(self._build_settlement_shapes(self._focused_region))

        return shapes

    def _build_district_shapes(self, region: Region) -> list[canvas.Shape]:
        """Internal raion/community borders inside a focused oblast (display-only)."""
        shapes: list[canvas.Shape] = []
        districts = DISTRICT_RINGS.get(region)
        if not districts:
            return shapes
        for rings in districts.values():
            elements = _rings_to_path_elements(rings, self._origin_x, self._origin_y, self._scale)
            if not elements:
                continue
            shapes.append(
                canvas.Path(
                    elements,
                    paint=ft.Paint(
                        color=theme.REGION_WATCHED_STROKE,
                        style=ft.PaintingStyle.STROKE,
                        stroke_width=_DISTRICT_STROKE_WIDTH,
                    ),
                )
            )
        return shapes

    def _build_settlement_shapes(self, region: Region) -> list[canvas.Shape]:
        """Cities/towns/villages inside a focused oblast, with collision-avoided labels.

        See app/ui/components/label_layout.py's module docstring for why
        this replaced a fixed anchor-to-anchor pixel distance (that
        algorithm didn't account for actual label text width or for
        narrower viewports genuinely packing dots closer together, and it
        hid conflicting labels instead of repositioning them).
        """
        shapes: list[canvas.Shape] = []
        settlements = _OBLAST_SETTLEMENTS.get(region)
        if not settlements:
            return shapes
        priority_names = _PRIORITY_SETTLEMENTS.get(region, frozenset())
        dot_color = theme.TEXT_MUTED

        anchors = [
            (name, *self._to_pixels(project_lat_lon(lat, lon))) for name, (lat, lon) in settlements.items()
        ]
        for name, x, y in anchors:
            shapes.append(
                canvas.Circle(
                    x, y, self._settlement_dot_radius,
                    paint=ft.Paint(color=dot_color, style=ft.PaintingStyle.FILL),
                )
            )

        for placed in place_labels(anchors, self._settlement_label_size, priority_names):
            if placed.offset:
                # Repositioned away from its dot to avoid a collision --
                # a short leader line keeps the association readable.
                shapes.append(
                    canvas.Line(
                        placed.anchor_x, placed.anchor_y,
                        placed.label_x, placed.label_y + placed.height / 2,
                        paint=ft.Paint(color=_LEADER_LINE_COLOR, stroke_width=0.6, style=ft.PaintingStyle.STROKE),
                    )
                )
            shapes.append(
                canvas.Text(
                    placed.label_x,
                    placed.label_y,
                    placed.name.capitalize(),
                    style=ft.TextStyle(size=self._settlement_label_size, color=dot_color),
                )
            )
        return shapes

    def _build_legend(self) -> ft.Row:
        """Small white/red legend explaining the two map colors."""

        def swatch(color: str, label: str) -> ft.Row:
            return ft.Row(
                spacing=6,
                controls=[
                    ft.Container(
                        width=12,
                        height=12,
                        border_radius=3,
                        bgcolor=color,
                        border=ft.border.all(1, theme.BORDER),
                    ),
                    ft.Text(label, size=11, color=theme.TEXT_SECONDARY),
                ],
            )

        return ft.Row(
            spacing=16,
            alignment=ft.MainAxisAlignment.CENTER,
            controls=[
                swatch(theme.REGION_INACTIVE_COLOR, "Тривоги немає"),
                swatch(theme.REGION_ACTIVE_COLOR, "Активна тривога"),
            ],
        )

    # --- Hit-testing ------------------------------------------------------

    def _handle_tap_up(self, e: ft.TapEvent) -> None:
        """Convert a tap's pixel position into a Region via point-in-polygon."""
        if not self._render_width or not self._render_height or not self._scale:
            return
        point = (e.local_x / self._scale + self._origin_x, e.local_y / self._scale + self._origin_y)

        region = region_at_point(point)
        if region is not None:
            self._handle_tap(region)

    def _handle_tap(self, region: Region) -> None:
        """Forward a resolved tap, or enter/exit the Kyiv/Cherkasy zoom.

        First tap on a focusable oblast (from the whole-country view)
        zooms in instead of opening the info dialog -- every other
        oblast's tap behavior is completely unchanged. Once already
        zoomed in, a tap resolves and opens the info dialog as usual
        (the back button, not the map itself, is how focus is exited).
        """
        if self._focused_region is None and region in _FOCUSABLE_REGIONS:
            self._enter_focus(region)
            return
        if self._on_region_tap is not None:
            self._on_region_tap(region)


def _rings_to_path_elements(
    rings: list[list[tuple[float, float]]], origin_x: float, origin_y: float, scale: float
) -> list[canvas.Path.PathElement]:
    """Convert one region's viewBox-unit point rings into scaled Path elements.

    ``origin_x``/``origin_y`` are subtracted first so this works for both
    the whole-country view (origin 0,0) and a focused/zoomed oblast
    (origin = that oblast's own padded bounding-box corner).
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

