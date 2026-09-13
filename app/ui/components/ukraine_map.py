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
from app.models.alert_models import Region, RegionState
from app.ui.components.label_layout import place_labels, scaled_settlement_sizes
from app.ui.theme import colors as theme
from app.ui.theme.colors import CARD_PADDING, PAGE_PADDING
from app.ukraine_geo import (
    REGION_RINGS,
    VIEWBOX_HEIGHT,
    VIEWBOX_WIDTH,
    district_at_point,
    region_at_point,
    regions_bounding_box,
)

OnRegionTapped = Callable[[Region], None]
#: Called with (owning oblast, district/raion name) when a district is
#: tapped while zoomed into a focusable oblast -- see _handle_tap_up.
OnDistrictTapped = Callable[[Region, str], None]

_MAP_ASPECT_RATIO = VIEWBOX_WIDTH / VIEWBOX_HEIGHT
_MIN_RENDER_WIDTH = 240.0
_STROKE_WIDTH = 1.1

# --- Focused-oblast zoom (district drill-down) -------------------------
# Tapping a focusable oblast on the main map (unlike every other oblast,
# which keeps its exact previous "show info dialog" tap behavior) crops/
# zooms to just that one oblast and layers on its internal raion
# ("district") borders -- see app.data.oblast_districts for the source/
# coverage of that geometry. Every oblast WITH district data is
# focusable, not just a hardcoded pair -- Kyiv City and Sevastopol have
# no raion data (see that module's docstring) and so aren't included,
# exactly mirroring which oblasts actually have something to drill into.
_FOCUSABLE_REGIONS: frozenset[Region] = frozenset(DISTRICT_RINGS.keys())
#: Tight margin + tall cap for the focused view, same reasoning as
#: movement_map.py's equivalents -- fills "almost the entire available
#: map area", per the Map Improvements spec.
_FOCUS_CROP_MARGIN_RATIO = 0.02
_FOCUS_MAX_RENDER_HEIGHT = 900.0
_DISTRICT_STROKE_WIDTH = 0.8


class UkraineMap(ft.Container):
    """The map card shown at the top of the main layout."""

    def __init__(
        self,
        on_region_tap: Optional[OnRegionTapped] = None,
        on_district_tap: Optional[OnDistrictTapped] = None,
    ) -> None:
        """Build the map card.

        ``on_region_tap`` is called with the tapped Region. ``on_district_tap``
        (new, optional -- every existing caller that doesn't pass it keeps
        working unchanged) is called with (oblast, district name) instead,
        when the tap lands inside a specific district while zoomed into a
        focusable oblast -- see ``_handle_tap_up``.
        """
        self._on_region_tap = on_region_tap
        self._on_district_tap = on_district_tap
        self._region_states: dict[Region, RegionState] = {}
        self._page_width: float = 340.0 + (2 * PAGE_PADDING) + (2 * CARD_PADDING)
        #: None = whole-country view (unchanged, existing behavior). Set
        #: to a focusable oblast to crop/zoom to just that oblast -- see
        #: _FOCUSABLE_REGIONS above.
        self._focused_region: Optional[Region] = None

        # Current viewBox-unit -> pixel transform: pixel = (point - origin) * scale.
        # Recomputed by _compute_layout() from page width + focus state.
        self._origin_x: float = 0.0
        self._origin_y: float = 0.0
        self._scale: float = 1.0
        self._render_width: float = 340.0
        self._render_height: float = 340.0 / _MAP_ASPECT_RATIO

        #: Recomputed alongside ``self._scale`` in ``_compute_layout()`` --
        #: see ``scaled_settlement_sizes()`` (label_layout.py); reused here
        #: for district name labels so they stay legible across phone and
        #: desktop render widths instead of a fixed pixel size.
        self._district_label_size: float = 9.0

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
        _, self._district_label_size = scaled_settlement_sizes(self._scale)

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

        return shapes

    def _build_district_shapes(self, region: Region) -> list[canvas.Shape]:
        """Internal raion/community borders + name labels inside a focused
        oblast (display-only).

        Labels use the same collision-avoided placement already used for
        settlement labels elsewhere in this project (``label_layout.place_labels``)
        so two adjacent small raions' names can't overlap each other --
        anchored at each raion's proper area-weighted centroid (see
        ``_polygon_centroid``), not a naive average of its vertices, which
        for a long/irregular raion shape can land noticeably off-center or
        even outside it.
        """
        shapes: list[canvas.Shape] = []
        districts = DISTRICT_RINGS.get(region)
        if not districts:
            return shapes

        anchors: list[tuple[str, float, float]] = []
        for name, rings in districts.items():
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
            # Largest-by-area ring is the raion's main body -- centroid of
            # a small detached exclave/island ring would be a misleading
            # label anchor.
            main_ring = max(rings, key=_ring_area)
            cx, cy = _polygon_centroid(main_ring)
            px, py = self._to_pixels((cx, cy))
            anchors.append((name, px, py))

        for placed in place_labels(anchors, self._district_label_size, frozenset()):
            shapes.append(
                canvas.Text(
                    placed.label_x,
                    placed.label_y,
                    placed.name,
                    style=ft.TextStyle(
                        size=self._district_label_size,
                        color=theme.TEXT_SECONDARY,
                        weight=ft.FontWeight.W_500,
                    ),
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
        """Convert a tap's pixel position into a Region (or, while zoomed
        into a focusable oblast, a specific district) via point-in-polygon.

        Checking the district first only happens while actually zoomed in
        (``self._focused_region``) -- the whole-country view's tap
        behavior for every oblast is completely unchanged. A tap that
        lands inside the focused oblast but outside every one of its
        district polygons (simplification can leave tiny gaps right at a
        shared border) falls back to the existing oblast-level dialog
        instead of silently doing nothing.
        """
        if not self._render_width or not self._render_height or not self._scale:
            return
        point = (e.local_x / self._scale + self._origin_x, e.local_y / self._scale + self._origin_y)

        if self._focused_region is not None:
            district_name = district_at_point(self._focused_region, point)
            if district_name is not None:
                if self._on_district_tap is not None:
                    self._on_district_tap(self._focused_region, district_name)
                return

        region = region_at_point(point)
        if region is not None:
            self._handle_tap(region)

    def _handle_tap(self, region: Region) -> None:
        """Forward a resolved tap, or enter/exit a focusable oblast's zoom.

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


def _ring_area(ring: list[tuple[float, float]]) -> float:
    """Shoelace-formula area of a closed point ring (always non-negative).

    Used to pick a multi-ring district's main body (largest area) as the
    label anchor, rather than a small detached exclave.
    """
    if len(ring) < 3:
        return 0.0
    total = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def _polygon_centroid(ring: list[tuple[float, float]]) -> tuple[float, float]:
    """Area-weighted centroid of a closed point ring (viewBox units).

    Standard polygon centroid formula -- meaningfully better than a
    naive average of vertices for a long/irregular raion shape, where a
    vertex average can land noticeably off-center or even outside the
    polygon. Falls back to a simple vertex average for a degenerate
    (near-zero-area) ring, which the formula below can't handle.
    """
    area_sum = 0.0
    cx = 0.0
    cy = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        cross = x1 * y2 - x2 * y1
        area_sum += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    area = area_sum / 2.0
    if abs(area) < 1e-9:
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        return sum(xs) / len(ring), sum(ys) / len(ring)
    return cx / (6.0 * area), cy / (6.0 * area)

