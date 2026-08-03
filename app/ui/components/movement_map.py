"""
Map for the "Рух загроз" (threat movement) tab.

Draws the same real Ukraine geometry as the main map (``app.ukraine_geo``),
but statically colored (this map does not show alert status) with an
overlay of the currently active :class:`ThreatMovement` entries:

  * If a movement has BOTH an origin and a destination explicitly stated
    in its source message, a dashed line + arrowhead is drawn between
    them and a threat-type icon sits at the midpoint, rotated to point
    along the direction of travel.
  * If only one place is known, the icon sits at that single point with
    no arrow/rotation (per spec: never draw an arrow, and never invent a
    direction, unless the direction is explicit) -- it's drawn pointing
    north (up) as a neutral default orientation.
  * If no place at all could be resolved from the message, nothing is
    drawn on the map for it (it still appears in the side list).

Rendering mirrors ``UkraineMap``'s approach: the base map, arrows, AND
(unlike the previous emoji-based version) the threat icons themselves
are all native ``flet.canvas`` shapes (``Path``/``Line``/``Circle``) --
small hand-drawn vector glyphs per threat category (UAV/Shahed, cruise
missile, ballistic missile, aircraft), not emoji text and not raster/SVG
image assets. This keeps the same "no Flutter SVG/image-decoding
dependency, no asset files" property ``UkraineMap`` already relies on
for Android reliability, while additionally allowing each icon to be
precisely rotated to its movement's bearing (computing a rotated polygon
in Python is trivial; rotating an emoji glyph or a static image is not,
in Flet 0.28.3). Since icons no longer are individual tappable widgets,
tapping one is resolved the same way region taps already are: hit-test
math against the tap's pixel position, tried against tracked icon
positions before falling back to region hit-testing (see
``_handle_map_tap_up``). Place-name pin labels stay real ``ft.Text``
controls layered in a ``ft.Stack`` on top, unchanged.
"""

from __future__ import annotations

import math
from typing import Callable, Optional

import flet as ft
import flet.canvas as canvas

from app.data.oblast_districts import DISTRICT_RINGS
from app.models.alert_models import Region
from app.models.movement_models import ThreatMovement, ThreatType
from app.ui.components.label_layout import scaled_icon_factor
from app.ui.components.ukraine_map import OnDistrictTapped, OnRegionTapped
from app.ui.icon_assets import icon_for_movement, missile_subtype
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

OnMovementTapped = Callable[[ThreatMovement], None]

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
_ARROW_COLOR = "#38BDF8"
_MAP_NEUTRAL_FILL = "#1C2333"
_MAP_STROKE_WIDTH = 1.0
_ARROW_STROKE_WIDTH = 2.5
_ARROWHEAD_LENGTH = 9.0
_ARROWHEAD_ANGLE = math.radians(24)  # slightly sharper/sleeker than a generic triangle

#: "Possible further direction" line beyond a movement's last confirmed
#: point -- a geometric extrapolation of the reported origin->destination
#: bearing, NOT a forecast/prediction of where a real target will
#: actually go. Rendered clearly differently from a confirmed movement
#: (faint, thin, tightly dashed, no solid arrowhead/icon at its tip) so
#: it can never be mistaken for reported data. Only ever drawn for a
#: movement that already has an explicit origin AND destination (see
#: ``_prediction_shapes``) -- never fabricated from a single point.
_PREDICTION_OPACITY = 0.32
_PREDICTION_STROKE_WIDTH = 1.4
_PREDICTION_DASH_PATTERN = [2, 5]
_PREDICTION_LENGTH_RATIO = 0.35  # extrapolate this fraction of the confirmed segment's own length

#: Faded trail connecting a continuation-matched movement's prior
#: reported points (see ThreatMovement.position_history) to its current
#: one -- progressively more transparent for older points, so the most
#: recent prior position reads as "just before now" and earlier ones
#: fade into the background instead of all looking equally significant.
_TRAIL_STROKE_WIDTH = 1.6
_TRAIL_MIN_OPACITY = 0.12
_TRAIL_MAX_OPACITY = 0.4

#: Kyiv Oblast / Cherkasy Oblast get a tighter crop margin + taller max
#: render height when selected (see is_settlement_case below) than other
#: oblasts -- kept from the original settlement-focused redesign even
#: though permanent settlement dots/labels themselves were removed (see
#: this module's changelog note near _build_static_shapes): settlement
#: names now only ever appear as a movement's own pin label (_pin_label),
#: tied to an actual active threat, never as permanent map decoration.
_TIGHT_CROP_REGIONS: frozenset[Region] = frozenset({Region.KYIV_OBLAST, Region.CHERKASY})
#: Internal raion ("district") boundary lines, drawn only inside a
#: settlement-special-case oblast -- same purple as that oblast's own
#: focused-mode outline, but thinner/unfilled so it reads as an interior
#: subdivision, not a second oblast border.
_DISTRICT_STROKE_COLOR = theme.REGION_WATCHED_STROKE
_DISTRICT_STROKE_WIDTH = 0.8

# --- Threat-type vector icons -------------------------------------------
# Small hand-drawn glyphs, one or more closed polygon "rings" per icon, in
# local unrotated coordinates centered at (0, 0) with the nose/front
# pointing along +x (angle 0). Rotating by a movement's bearing angle
# (same atan2(dy, dx) convention already used for the arrow in
# _arrow_shapes) aligns the nose with the direction of travel directly,
# no extra offset needed. Replaces the flat ThreatType.icon emoji
# entirely on this map -- see module docstring for why vector shapes
# were chosen over SVG/PNG image assets.

#: UAV / Shahed -- a delta/kite "flying wing" silhouette (Shahed-136 is
#: itself a delta-wing airframe), one ring.
_ICON_RING_UAV: list[tuple[float, float]] = [
    (13.0, 0.0), (-3.0, 8.5), (-8.0, 0.0), (-3.0, -8.5),
]

#: Cruise missile -- slim fuselage + nose cone + small tail fins, one ring.
_ICON_RING_CRUISE: list[tuple[float, float]] = [
    (13.0, 0.0), (5.0, 3.0), (-9.0, 3.0), (-9.0, 6.5),
    (-13.0, 0.0), (-9.0, -6.5), (-9.0, -3.0), (5.0, -3.0),
]

#: Ballistic missile -- same family as cruise but larger, blunter body and
#: bigger fins, so it reads as visibly more severe at a glance.
_ICON_RING_BALLISTIC: list[tuple[float, float]] = [
    (15.0, 0.0), (6.0, 4.5), (-9.0, 4.5), (-9.0, 10.0),
    (-15.0, 0.0), (-9.0, -10.0), (-9.0, -4.5), (6.0, -4.5),
]

#: Aircraft (carrier/bomber) -- three separate rings: fuselage, main
#: swept wings, small tailplane -- read together as a top-down plane
#: silhouette.
_ICON_RINGS_AIRCRAFT: list[list[tuple[float, float]]] = [
    [(14.0, 0.0), (2.0, 2.5), (-12.0, 1.5), (-12.0, -1.5), (2.0, -2.5)],  # fuselage
    [(3.0, 1.5), (0.0, 11.0), (-4.0, 1.5), (-4.0, -1.5), (0.0, -11.0), (3.0, -1.5)],  # wings
    [(-9.0, 0.8), (-12.0, 5.0), (-14.0, 0.8), (-14.0, -0.8), (-12.0, -5.0), (-9.0, -0.8)],  # tailplane
]

#: Keyword stems (already present verbatim in ThreatMovement.matched_keywords,
#: Local alias -- app.ui.icon_assets.missile_subtype is now the single
#: source of truth for this classification (shared by every threat-icon
#: call site, not just the map), imported above.
_missile_subtype = missile_subtype


def _icon_geometry(movement: ThreatMovement) -> tuple[str, list[list[tuple[float, float]]]]:
    """Return (fill_color, rings) for a movement's threat-type icon.

    ``rings`` is now used only for the (rare) fallback described in
    ``app.ui.icon_assets.icon_for_movement`` and for sizing math -- the
    actual on-screen glyph is the real PNG asset from ``assets/icons/``
    (see ``_build_icon_image_controls``), not this vector path.
    """
    if movement.threat_type in (ThreatType.SHAHED, ThreatType.UAV):
        return theme.THREAT_ICON_UAV, [_ICON_RING_UAV]
    if movement.threat_type is ThreatType.AIRCRAFT:
        return theme.THREAT_ICON_AIRCRAFT, _ICON_RINGS_AIRCRAFT
    if movement.threat_type is ThreatType.MISSILE:
        if _missile_subtype(movement) == "ballistic":
            return theme.THREAT_ICON_BALLISTIC, [_ICON_RING_BALLISTIC]
        return theme.THREAT_ICON_CRUISE, [_ICON_RING_CRUISE]
    # ThreatType.UNKNOWN -- threat-related but no specific weapon named.
    return theme.TEXT_MUTED, [_ICON_RING_UAV]


_ICON_TRACK_RING_RADIUS = 17.0
#: Font size for the "×N" grouped-threat badge, before adaptive scaling.
_GROUP_BADGE_FONT_SIZE = 10.0
#: Default heading (pointing straight up/north) for a movement with only
#: one known point -- there is no real direction to rotate to, so this is
#: a neutral, non-invented orientation, never a guessed bearing.
_ICON_DEFAULT_ANGLE = -math.pi / 2
#: Tap-hit radius around an icon's center, in pixels -- generous enough to
#: be an easy touch target without overlapping a typical neighboring icon.
_ICON_TAP_RADIUS = 20.0


def _resolve_icon_overlaps(
    anchors: list[tuple[float, float]], min_separation: float, iterations: int = 3,
) -> list[tuple[float, float]]:
    """Nudge icon centers apart when two distinct movements' icons would
    otherwise overlap, via a few passes of simple pairwise separation.

    Purely a cosmetic rendering adjustment -- never changes any stored
    movement data, only where its icon is drawn. Bounded to a few
    relaxation passes over a small list (the number of simultaneously
    tracked movements is always small, capped by the existing time-based
    prune), so this stays cheap even though it's O(n^2) per pass.
    """
    positions = list(anchors)
    count = len(positions)
    for _ in range(iterations):
        moved = False
        for i in range(count):
            for j in range(i + 1, count):
                x1, y1 = positions[i]
                x2, y2 = positions[j]
                dx, dy = x2 - x1, y2 - y1
                dist = math.hypot(dx, dy)
                if dist < min_separation:
                    if dist < 1e-6:
                        dx, dy, dist = 1.0, 0.0, 1.0
                    push = (min_separation - dist) / 2
                    ux, uy = dx / dist, dy / dist
                    positions[i] = (x1 - ux * push, y1 - uy * push)
                    positions[j] = (x2 + ux * push, y2 + uy * push)
                    moved = True
        if not moved:
            break
    return positions


def _icon_shapes(
    movement: ThreatMovement, x: float, y: float, angle: float, scale_factor: float = 1.0,
) -> list[canvas.Shape]:
    """Build the track ring + group badge for one movement.

    The threat-type glyph itself is a real PNG image control (see
    ``_build_icon_image_controls``), not drawn here -- this only builds
    the surrounding canvas decoration (radar-style track ring, "×N"
    group badge) that sits in the same spot.

    ``angle`` is accepted for a consistent call signature with the
    image-control builder (same anchor/angle entries feed both) but
    isn't used for anything drawn here -- the ring and badge are
    circular/text elements with no orientation of their own.
    ``scale_factor`` (see ``label_layout.scaled_icon_factor``) keeps
    everything a consistent, legible size across phone and desktop
    render widths instead of a fixed pixel size that looked right only
    on the viewport it was originally tuned against.

    A ``group_count`` > 1 (e.g. "2 шахеди" in one message) draws ONE
    icon with a small "×N" badge rather than N overlapping icons --
    overlapping identical icons at the same reported point would just
    obscure each other and add nothing readable.
    """
    del angle  # see docstring -- kept only for a uniform call signature
    color, _rings = _icon_geometry(movement)
    track_radius = _ICON_TRACK_RING_RADIUS * scale_factor
    shapes: list[canvas.Shape] = [
        canvas.Circle(
            x, y, track_radius,
            paint=ft.Paint(color=ft.Colors.with_opacity(0.45, color), style=ft.PaintingStyle.STROKE, stroke_width=1.0),
        )
    ]

    if movement.group_count > 1:
        badge_font_size = _GROUP_BADGE_FONT_SIZE * scale_factor
        badge_x = x + track_radius * 0.55
        badge_y = y - track_radius * 0.95
        shapes.append(
            canvas.Circle(
                badge_x, badge_y, badge_font_size * 0.85,
                paint=ft.Paint(color=theme.BACKGROUND, style=ft.PaintingStyle.FILL),
            )
        )
        shapes.append(
            canvas.Circle(
                badge_x, badge_y, badge_font_size * 0.85,
                paint=ft.Paint(color=color, style=ft.PaintingStyle.STROKE, stroke_width=1.0),
            )
        )
        shapes.append(
            canvas.Text(
                badge_x, badge_y,
                f"×{movement.group_count}",
                style=ft.TextStyle(size=badge_font_size, color=color, weight=ft.FontWeight.BOLD),
                text_align=ft.TextAlign.CENTER,
            )
        )
    return shapes


def _icon_image_control(
    movement: ThreatMovement, x: float, y: float, angle: float, scale_factor: float = 1.0,
) -> ft.Control:
    """Build the real PNG threat-type icon as a positioned, rotated ``ft.Image``.

    ``angle`` is the same atan2(dy, dx) convention as the arrow bearing
    in ``_arrow_shapes`` (0 = pointing along +x/screen-right); the
    source PNGs are drawn nose-right at angle 0 (see
    ``tools/generate_icons.py``, which rasterizes this same convention
    from ``_ICON_RING_*``), so ``ft.Rotate(angle)`` lines the glyph up
    with the movement's actual reported direction directly, with no
    extra offset.
    """
    size = 2 * _ICON_TRACK_RING_RADIUS * scale_factor * 0.82
    return ft.Container(
        left=x - size / 2,
        top=y - size / 2,
        width=size,
        height=size,
        rotate=ft.Rotate(angle),
        content=ft.Image(
            src=icon_for_movement(movement),
            width=size,
            height=size,
            fit=ft.ImageFit.CONTAIN,
            filter_quality=ft.FilterQuality.HIGH,
        ),
    )


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
        on_district_tap: Optional[OnDistrictTapped] = None,
    ) -> None:
        """Build the map card; callbacks fire for a tapped movement icon / oblast / district."""
        self._on_movement_tap = on_movement_tap
        self._on_region_tap = on_region_tap
        self._on_district_tap = on_district_tap
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
        #: See ``label_layout.scaled_icon_factor`` -- keeps movement
        #: icons/arrows a consistent, legible size across phone and
        #: desktop render widths.
        self._icon_scale_factor: float = 1.0
        #: (x, y, movement) for every currently-drawn icon, in pixel
        #: space -- rebuilt alongside the canvas shapes every time, used
        #: by _handle_map_tap_up to resolve a tap to a movement (icons
        #: are canvas-drawn, not individually tappable widgets anymore).
        self._movement_hit_points: list[tuple[float, float, ThreatMovement]] = []
        self._icon_render_entries: list[tuple[ThreatMovement, float, float, float]] = []

        #: Cache of the layout/selection-only shapes (region borders,
        #: district outlines, settlement dots/labels) -- see
        #: ``_build_static_shapes()``'s docstring. Keyed on the transform +
        #: selection tuple that produced it; invalidated automatically
        #: whenever that key changes.
        self._static_shapes_cache: list[canvas.Shape] = []
        self._static_shapes_cache_key: Optional[tuple] = None

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
            animate_size=theme.ANIM_SLOW,
        )
        self._map_frame = ft.Container(
            content=self._overlay_stack,
            alignment=ft.alignment.center,
            width=self._page_width - (2 * PAGE_PADDING) - (2 * CARD_PADDING),
            animate_size=theme.ANIM_SLOW,
        )
        self._empty_hint = ft.Text(
            "Активних повідомлень з відомим напрямком немає.",
            size=12,
            color=theme.TEXT_MUTED,
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
            self._icon_scale_factor = scaled_icon_factor(self._scale)
            return

        min_x, min_y, max_x, max_y = regions_bounding_box(self._selected_regions)
        box_w = max(1.0, max_x - min_x)
        box_h = max(1.0, max_y - min_y)

        is_settlement_case = bool(self._selected_regions & _TIGHT_CROP_REGIONS)
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
        self._icon_scale_factor = scaled_icon_factor(self._scale)

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
        """Build the base map (selection-aware) plus any movement arrows.

        The region borders / district outlines / settlement dots below
        depend only on the current layout transform and oblast selection
        -- never on the movement list -- but this whole method re-runs on
        every new movement (``update_movements()``/``_rebuild_overlay()``),
        which during a rapid multi-message barrage can be several times a
        second. Recomputing ~27 oblasts' full point-by-point transform
        that many times a second for pixel-identical output is pure
        waste, so that part is cached and only rebuilt when the transform
        or selection actually changes (see ``_static_shapes_cache``).

        Icon anchor points are resolved in a first pass (see
        ``_resolve_icon_overlaps``) BEFORE anything is drawn, so two
        distinct nearby targets' icons get nudged apart -- purely a
        cosmetic rendering adjustment, never a change to the underlying
        movement's actual reported position.
        """
        cache_key = (self._origin_x, self._origin_y, self._scale, frozenset(self._selected_regions))
        if cache_key != self._static_shapes_cache_key:
            self._static_shapes_cache = self._build_static_shapes()
            self._static_shapes_cache_key = cache_key

        shapes: list[canvas.Shape] = list(self._static_shapes_cache)

        entries: list[dict] = []
        for movement in self._movements:
            if movement.has_direction:
                x1, y1 = self._to_pixels(movement.origin_point)  # type: ignore[arg-type]
                x2, y2 = self._to_pixels(movement.destination_point)  # type: ignore[arg-type]
                mid_x, mid_y = (x1 + x2) / 2, (y1 + y2) / 2
                entries.append({
                    "movement": movement,
                    "has_direction": True,
                    "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                    "anchor": (mid_x, mid_y),
                    "angle": math.atan2(y2 - y1, x2 - x1),
                })
            elif movement.has_any_location:
                point = movement.origin_point or movement.destination_point
                px, py = self._to_pixels(point)  # type: ignore[arg-type]
                entries.append({
                    "movement": movement,
                    "has_direction": False,
                    "anchor": (px, py),
                    "angle": _ICON_DEFAULT_ANGLE,
                })

        anchors = [entry["anchor"] for entry in entries]
        if len(anchors) > 1:
            min_separation = 2 * _ICON_TRACK_RING_RADIUS * self._icon_scale_factor * 0.9
            anchors = _resolve_icon_overlaps(anchors, min_separation)

        self._movement_hit_points = []
        self._icon_render_entries = []
        for entry, (ax, ay) in zip(entries, anchors):
            movement = entry["movement"]

            if movement.position_history:
                trail_points = [self._to_pixels(point) for point in movement.position_history]
                trail_points.append((ax, ay))
                shapes.extend(_trail_shapes(trail_points, _ARROW_COLOR, self._icon_scale_factor))

            if entry["has_direction"]:
                shapes.extend(
                    _arrow_shapes(entry["x1"], entry["y1"], entry["x2"], entry["y2"], _ARROW_COLOR, self._icon_scale_factor)
                )
                shapes.extend(
                    _prediction_shapes(
                        entry["x1"], entry["y1"], entry["x2"], entry["y2"], _ARROW_COLOR, self._icon_scale_factor
                    )
                )

            shapes.extend(_icon_shapes(movement, ax, ay, entry["angle"], self._icon_scale_factor))
            self._icon_render_entries.append((movement, ax, ay, entry["angle"]))
            self._movement_hit_points.append((ax, ay, movement))

        return shapes

    def _build_icon_image_controls(self) -> list[ft.Control]:
        """Build the real PNG threat-icon image, positioned+rotated, for
        every currently-plotted movement -- from the exact same
        overlap-resolved anchors ``_build_map_and_arrow_shapes`` just
        computed (see ``_icon_render_entries``), so the image lines up
        pixel-for-pixel with its track ring/badge on the canvas below it.
        """
        return [
            _icon_image_control(movement, ax, ay, angle, self._icon_scale_factor)
            for movement, ax, ay, angle in self._icon_render_entries
        ]

    def _build_static_shapes(self) -> list[canvas.Shape]:
        """Build everything that depends only on layout + selection, never on movements.

        Cached by ``_build_map_and_arrow_shapes`` -- see that method's
        docstring. Order matches the original single-pass build exactly
        (region borders, then district outlines, then settlement dots/
        labels) so cropping this out changes performance only, not the
        visual stacking order.
        """
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

        # Reuses the exact same DISTRICT_RINGS data (app/data/oblast_districts.py)
        # UkraineMap draws for the Alert Map's district drill-down -- draw
        # each raion's outline inside a region the person has selected,
        # stroke-only (no fill, so the region's own fill still shows
        # through), same purple used for the region's own focused-mode
        # border but thinner. A region not in ``_selected_regions`` is
        # completely unaffected.
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

        return shapes

    def _rebuild_overlay(self) -> None:
        """Rebuild the canvas shapes and icon/label markers for the current movements."""
        self._canvas.shapes = self._build_map_and_arrow_shapes()

        controls: list[ft.Control] = [self._gesture_detector]
        has_any_plotted = any(m.has_any_location for m in self._movements)

        controls.extend(self._build_icon_image_controls())
        for movement in self._movements:
            controls.extend(self._build_markers_for(movement))

        self._overlay_stack.controls = controls
        self._empty_hint.visible = not has_any_plotted

    def _build_markers_for(self, movement: ThreatMovement) -> list[ft.Control]:
        """Build the pin label(s) for one movement, if it has a location.

        The threat-type icon itself is no longer a widget here -- it's
        drawn directly on the canvas (see ``_build_map_and_arrow_shapes``/
        ``_icon_shapes``) so it can be rotated to the movement's bearing.
        """
        markers: list[ft.Control] = []

        if movement.has_direction:
            ox, oy = self._to_pixels(movement.origin_point)  # type: ignore[arg-type]
            dx, dy = self._to_pixels(movement.destination_point)  # type: ignore[arg-type]
            if movement.display_origin_name:
                markers.append(self._pin_label(movement.display_origin_name, ox, oy))
            if movement.display_destination_name:
                markers.append(self._pin_label(movement.display_destination_name, dx, dy))
        elif movement.has_any_location:
            point = movement.origin_point or movement.destination_point
            px, py = self._to_pixels(point)  # type: ignore[arg-type]
            destination_only_name = (
                movement.display_destination_name if movement.origin_point is None else None
            )
            if destination_only_name:
                # Destination-only report: say plainly that the origin
                # wasn't stated, instead of just showing the place name on
                # its own (which would read the same as a fully-known point).
                markers.append(self._pin_label(f"Unknown origin, moving towards {destination_only_name}", px, py))
            else:
                name = movement.display_origin_name or movement.display_destination_name
                if name:
                    markers.append(self._pin_label(name, px, py))

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

    def _handle_tap(self, movement: ThreatMovement) -> None:
        if self._on_movement_tap is not None:
            self._on_movement_tap(movement)

    def _handle_map_tap_up(self, e: ft.TapEvent) -> None:
        """Resolve a tap to a movement icon first, else a district/Region.

        Icons are canvas-drawn (not individual tappable widgets), so a
        tap is matched against each currently-drawn icon's pixel center
        within ``_ICON_TAP_RADIUS`` first -- closest one wins. If no
        icon is close enough, the tap is resolved against a place:
        district first (reusing the exact same ``district_at_point``
        point-in-polygon check and ``DISTRICT_RINGS`` geometry
        ``UkraineMap`` uses for the Alert Map's district drill-down --
        not a second, separately-maintained implementation), but only
        for a region the person has actually selected/cropped to (its
        district outlines are the only ones drawn here at all, per
        ``_build_static_shapes``); otherwise the tap resolves to a
        plain oblast tap exactly as before.
        """
        radius_sq = (_ICON_TAP_RADIUS * self._icon_scale_factor) ** 2
        best_movement: Optional[ThreatMovement] = None
        best_dist_sq = radius_sq
        for hx, hy, movement in self._movement_hit_points:
            dist_sq = (e.local_x - hx) ** 2 + (e.local_y - hy) ** 2
            if dist_sq <= best_dist_sq:
                best_dist_sq = dist_sq
                best_movement = movement

        if best_movement is not None:
            self._handle_tap(best_movement)
            return

        point = self._to_viewbox((e.local_x, e.local_y))
        region = region_at_point(point)
        if region is None:
            return

        if region in self._selected_regions and self._on_district_tap is not None:
            district_name = district_at_point(region, point)
            if district_name is not None:
                self._on_district_tap(region, district_name)
                return

        if self._on_region_tap is not None:
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


def _arrow_shapes(
    x1: float, y1: float, x2: float, y2: float, color: str, scale_factor: float = 1.0,
) -> list[canvas.Shape]:
    """Build a dashed line + filled triangular arrowhead pointing at (x2, y2).

    ``scale_factor`` (see ``label_layout.scaled_icon_factor``) keeps the
    stroke/arrowhead a consistent, legible size across phone and desktop
    render widths, matching the icons they lead into.
    """
    angle = math.atan2(y2 - y1, x2 - x1)
    head_length = _ARROWHEAD_LENGTH * scale_factor

    back1_x = x2 - head_length * math.cos(angle - _ARROWHEAD_ANGLE)
    back1_y = y2 - head_length * math.sin(angle - _ARROWHEAD_ANGLE)
    back2_x = x2 - head_length * math.cos(angle + _ARROWHEAD_ANGLE)
    back2_y = y2 - head_length * math.sin(angle + _ARROWHEAD_ANGLE)

    line = canvas.Line(
        x1, y1, x2, y2,
        paint=ft.Paint(
            color=color,
            stroke_width=_ARROW_STROKE_WIDTH * scale_factor,
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


def _prediction_shapes(x1: float, y1: float, x2: float, y2: float, color: str, scale_factor: float = 1.0) -> list[canvas.Shape]:
    """Build a faint, tightly-dashed "possible further direction" line
    beyond a movement's confirmed destination (x2, y2), extrapolated from
    the confirmed origin (x1, y1) -> destination bearing.

    This is a geometric extrapolation of an explicitly reported bearing,
    never a forecast of an actual future position -- see this constant
    group's docstring above _PREDICTION_OPACITY. Deliberately has no
    arrowhead and no icon at its tip, so it reads unambiguously as
    "beyond what was actually reported", never confusable with the solid
    confirmed arrow drawn by ``_arrow_shapes``.
    """
    dx, dy = x2 - x1, y2 - y1
    extra_x = x2 + dx * _PREDICTION_LENGTH_RATIO
    extra_y = y2 + dy * _PREDICTION_LENGTH_RATIO
    return [
        canvas.Line(
            x2, y2, extra_x, extra_y,
            paint=ft.Paint(
                color=ft.Colors.with_opacity(_PREDICTION_OPACITY, color),
                stroke_width=_PREDICTION_STROKE_WIDTH * scale_factor,
                style=ft.PaintingStyle.STROKE,
                stroke_dash_pattern=_PREDICTION_DASH_PATTERN,
                stroke_cap=ft.StrokeCap.ROUND,
            ),
        )
    ]


def _trail_shapes(points_px: list[tuple[float, float]], color: str, scale_factor: float = 1.0) -> list[canvas.Shape]:
    """Build a faded trail line through a movement's ``position_history``
    (already projected to pixels) leading into its current position.

    Older segments are more transparent (see _TRAIL_MIN/MAX_OPACITY),
    so the trail reads as "recent path", not as N equally-weighted
    points. Expects >= 2 points; the caller only builds this list when
    history is non-empty and appends the current position to it.
    """
    shapes: list[canvas.Shape] = []
    segment_count = len(points_px) - 1
    if segment_count < 1:
        return shapes
    for i in range(segment_count):
        x1, y1 = points_px[i]
        x2, y2 = points_px[i + 1]
        # Later segments (closer to "now") are more opaque.
        t = (i + 1) / segment_count
        opacity = _TRAIL_MIN_OPACITY + (_TRAIL_MAX_OPACITY - _TRAIL_MIN_OPACITY) * t
        shapes.append(
            canvas.Line(
                x1, y1, x2, y2,
                paint=ft.Paint(
                    color=ft.Colors.with_opacity(opacity, color),
                    stroke_width=_TRAIL_STROKE_WIDTH * scale_factor,
                    style=ft.PaintingStyle.STROKE,
                    stroke_cap=ft.StrokeCap.ROUND,
                ),
            )
        )
    return shapes
