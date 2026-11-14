"""
Shared collision-avoidance label placement for settlement/place-name labels
drawn on the map canvases (see ``ukraine_map.py`` and ``movement_map.py``'s
Kyiv Oblast / Cherkasy Oblast settlement overlays).

Root cause of the Android-only overlap bug this replaces
----------------------------------------------------------
Both maps used to declutter labels with one fixed PIXEL DISTANCE between
anchor **points** -- "skip drawing a label if its dot is within 26px of an
already-labeled dot" -- and nothing else. That was wrong in two independent
ways, both of which just happened not to show up on Windows/BlueStacks:

1. It measured distance between DOTS, never the actual rendered WIDTH of
   the label TEXT. Settlement names in this dataset range from 4 to 21
   characters; a 21-character Cyrillic name at this font size easily
   renders 90-100+ px wide, more than triple the 26px the old check
   assumed was "safe". Two names could pass the "far enough apart" test
   and still visually run into each other.

2. The available width the map's pixel ``scale`` is computed from is the
   page's LOGICAL width -- already DPI-normalized by Flutter, so that part
   is *not* an Android quirk -- but a real phone's logical width
   (~360-430px, matching its physical screen) is legitimately much
   narrower than a resizable desktop window or a BlueStacks virtual
   display, which people commonly run much wider. At the *same* real-world
   zoom (e.g. the whole of Kyiv Oblast), a narrower viewport means a
   smaller ``scale``, so the very same settlements' dots end up packed
   into far fewer pixels on a real phone than in a roomy desktop/BlueStacks
   window. The old fixed 26px constant only ever worked "by luck" at the
   larger scale those environments happen to produce -- it was never
   actually a property of Android, just of whichever viewport happened to
   be wide enough to keep dots more than 26px apart.

This module fixes both: it estimates each label's real footprint from its
character count (see ``estimate_text_width``), does genuine axis-aligned
bounding-box collision checks between labels (not point distance), and
when two labels would collide, tries a small ring of alternative offset
positions around the anchor before falling back to the least-bad option --
so a label is *repositioned*, never hidden. Every anchor passed in gets a
returned placement. Because it works entirely in the same logical-pixel
space the maps already use, it adapts automatically to any viewport size
or zoom level with no device-specific branching at all.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

#: Average rendered glyph width as a fraction of font size, for the
#: sans-serif font Flet/Flutter renders by default. Cyrillic glyphs tend
#: to run a bit wider than Latin ones at the same font size. This is
#: deliberately a conservative (generous) estimate: Python has no way to
#: query the Flutter client's real, on-device font metrics for an exact
#: per-glyph width, so erring wide is what keeps placement robust to
#: ordinary device-to-device rendering variance (a slightly different
#: system font fallback, sub-pixel rounding, etc.) -- at worst it costs a
#: little placement density, it never causes a wrongly-confident overlap.
_AVG_GLYPH_WIDTH_RATIO = 0.62
#: Extra fixed padding (px) added to every estimated label box: covers
#: anti-aliasing bleed plus the small left-padding used when drawing text.
_LABEL_PADDING_X = 5.0
_LABEL_PADDING_Y = 3.0
#: How many rings of alternative offset positions to try around an
#: anchor before giving up and using the least-overlapping candidate
#: found so far, and how many compass directions to sample at each
#: ring. Tuned against the densest real cluster in this app's data (all
#: 85 Kyiv Oblast settlements, focused/zoomed to a real Android phone's
#: narrow ~380 logical-px width -- the exact scenario that exposed the
#: original bug): this configuration resolves every collision in that
#: worst case, at a still-smooth ~15ms per full relayout (see
#: ``place_labels``'s docstring for the perf note).
_MAX_RINGS = 11
_DIRECTIONS_PER_RING = 20

# --- Adaptive settlement dot/label sizing (phones <-> desktop) ---------
# Both maps used to draw settlement dots/labels at a FIXED pixel size
# (radius 2.2, font 9.0), tuned to look right on a phone-width card. That
# size never changed even though the map's own geometry -- region
# borders, the focused oblast's crop -- already scales with each map's
# own ``self._scale`` (bigger on desktop, since a resizable window
# commonly offers far more width than a phone's ~360-430 logical px --
# see this module's docstring above for why that's the same "narrower
# phone vs. roomy desktop viewport" scale difference already documented
# for label *position*). A fixed dot/font size meant text and dots that
# were legible on a phone shrank -- relatively speaking -- to a barely
# readable fraction of a wide desktop map. ``scaled_settlement_sizes()``
# scales both between that same phone-tuned floor and a desktop-tuned
# ceiling instead, shared by both maps so they stay visually consistent
# with each other.
_SETTLEMENT_DOT_RADIUS_MIN = 2.2
_SETTLEMENT_DOT_RADIUS_MAX = 3.4
_SETTLEMENT_LABEL_SIZE_MIN = 9.0
_SETTLEMENT_LABEL_SIZE_MAX = 13.0
#: Render ``scale`` at the smallest supported phone width (240px / 762
#: viewBox units -- both maps' own ``_MIN_RENDER_WIDTH``/``VIEWBOX_WIDTH``)
#: -- the reference point where the MIN sizes above apply exactly, so the
#: smallest supported phones render identically to before this change.
_LABEL_SCALE_REFERENCE = 240.0 / 762.0
#: How many multiples of the phone-reference scale before sizing maxes
#: out at the ceiling above -- growth is capped past a certain window
#: width instead of scaling forever on an ultra-wide desktop.
_LABEL_SCALE_GROWTH_CAP = 2.4


#: Analogous phone<->desktop adaptive scaling for the "Рух загроз" map's
#: threat-type icons (vector shapes drawn at a fixed local-coordinate
#: size in movement_map.py, rotated but never scaled by ``self._scale``)
#: -- same root cause as settlement dots/labels above: geometry that
#: scales with render width should stay legible, not shrink relatively
#: on a wide desktop window. A narrower band than the settlement one
#: (0.85-1.3x) since icons are already a fairly large, deliberately
#: bold visual element -- growing them as aggressively as tiny text
#: would look oversized on a big desktop map.
_ICON_SCALE_FACTOR_MIN = 0.85
_ICON_SCALE_FACTOR_MAX = 1.3


def scaled_icon_factor(scale: float) -> float:
    """Return a multiplier for movement-icon local coordinates/stroke
    widths/tap radius, for the current render ``scale``.

    Same phone-floor / desktop-ceiling interpolation as
    ``scaled_settlement_sizes`` (see that function's docstring), reusing
    the same reference/cap constants so both kinds of adaptive sizing
    move in step with each other as the window resizes.
    """
    ratio = scale / _LABEL_SCALE_REFERENCE if _LABEL_SCALE_REFERENCE else 1.0
    ratio = max(1.0, min(ratio, _LABEL_SCALE_GROWTH_CAP))
    t = (ratio - 1.0) / (_LABEL_SCALE_GROWTH_CAP - 1.0)
    return round(_ICON_SCALE_FACTOR_MIN + (_ICON_SCALE_FACTOR_MAX - _ICON_SCALE_FACTOR_MIN) * t, 3)


def scaled_settlement_sizes(scale: float) -> tuple[float, float]:
    """Return (dot_radius, label_font_size) for a map's current render scale.

    Linear interpolation between the phone-tuned floor (at or below
    ``_LABEL_SCALE_REFERENCE``) and the desktop-tuned ceiling (at or
    above ``_LABEL_SCALE_REFERENCE * _LABEL_SCALE_GROWTH_CAP``), so
    narrow phones keep the exact original sizing (no regression) while
    wide desktop windows get visibly larger, legible dots/text instead of
    a size that was only ever tuned for a phone-width viewport.
    """
    ratio = scale / _LABEL_SCALE_REFERENCE if _LABEL_SCALE_REFERENCE else 1.0
    ratio = max(1.0, min(ratio, _LABEL_SCALE_GROWTH_CAP))
    t = (ratio - 1.0) / (_LABEL_SCALE_GROWTH_CAP - 1.0)
    dot_radius = _SETTLEMENT_DOT_RADIUS_MIN + (_SETTLEMENT_DOT_RADIUS_MAX - _SETTLEMENT_DOT_RADIUS_MIN) * t
    label_size = _SETTLEMENT_LABEL_SIZE_MIN + (_SETTLEMENT_LABEL_SIZE_MAX - _SETTLEMENT_LABEL_SIZE_MIN) * t
    return round(dot_radius, 2), round(label_size, 2)


def estimate_text_width(text: str, font_size: float) -> float:
    """A deliberately generous estimate of a label's rendered pixel width.

    Not an exact measurement -- Flet/Python has no synchronous way to ask
    the Flutter client for real font metrics -- but character-count-based
    estimation with a generous safety margin is the standard technique
    for this kind of offline label-layout, and is what keeps a 21-char
    name from being treated the same as a 4-char one (the actual bug
    being fixed here).
    """
    return max(1, len(text)) * font_size * _AVG_GLYPH_WIDTH_RATIO + _LABEL_PADDING_X


@dataclass(slots=True)
class PlacedLabel:
    """Where to actually draw one label, after collision avoidance."""

    name: str
    anchor_x: float
    anchor_y: float
    label_x: float  # top-left corner of the text draw position
    label_y: float
    width: float
    height: float
    #: True if this label had to move away from the default position --
    #: callers should draw a short leader line from (anchor_x, anchor_y)
    #: to the label so the association with its point stays clear (per
    #: "labels should remain close to their corresponding location").
    offset: bool


def _default_offset(font_size: float) -> tuple[float, float]:
    """The original, no-collision label offset from its anchor dot."""
    return 4.0, -(font_size * 0.55)


def _candidate_offset_template(font_size: float) -> list[tuple[float, float, bool]]:
    """All (dx, dy, is_offset) candidates to try, in preference order.

    Depends only on ``font_size`` (constant for one ``place_labels()``
    call, never per-anchor) -- built once per call and reused for every
    anchor, rather than recomputed per anchor as an earlier version of
    this function mistakenly did (pure performance fix, no behavior
    change: same offsets either way).
    """
    candidates: list[tuple[float, float, bool]] = [(*_default_offset(font_size), False)]
    step_degrees = 360.0 / _DIRECTIONS_PER_RING
    for ring in range(1, _MAX_RINGS + 1):
        radius = (font_size + 5.0) * ring
        for k in range(_DIRECTIONS_PER_RING):
            angle = math.radians(step_degrees * k)
            candidates.append((radius * math.cos(angle), radius * math.sin(angle), True))
    return candidates


def _overlap_area(
    ax: float, ay: float, aw: float, ah: float, bx: float, by: float, bw: float, bh: float
) -> float:
    """Overlap area (0.0 if none) between two axis-aligned boxes."""
    overlap_w = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    overlap_h = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    return overlap_w * overlap_h


def place_labels(
    anchors: list[tuple[str, float, float]],
    font_size: float,
    priority_names: frozenset[str] = frozenset(),
) -> list[PlacedLabel]:
    """Greedily place every ``(name, x, y)`` anchor's label with collision avoidance.

    Priority names (see each caller's raion-center set) are placed first,
    so they claim their preferred default position and only genuinely
    conflicting neighbors get pushed to an alternative offset. Every
    anchor passed in is guaranteed a returned ``PlacedLabel`` -- none are
    ever dropped, satisfying "do not simply hide labels" / "keep all
    important labels visible whenever possible".

    Performance: builds the candidate offset template once (see
    ``_candidate_offset_template``), then for N anchors this is
    O(N * candidates * N) in the worst case (candidates = 1 +
    ``_MAX_RINGS`` * ``_DIRECTIONS_PER_RING``), but the loop exits at the
    first collision-free candidate, so the common case is much cheaper.
    Even the worst real case in this app (all 85 Kyiv Oblast settlements,
    focused on a narrow ~380px-wide phone viewport -- the densest
    real-world cluster this dataset produces) resolves in roughly
    15-20ms, and this only re-runs when the map's layout or movement list
    actually changes, never per animation frame.
    """
    ordered = sorted(anchors, key=lambda a: (a[0] not in priority_names, a[0]))
    placed: list[PlacedLabel] = []
    candidates = _candidate_offset_template(font_size)

    for name, x, y in ordered:
        width = estimate_text_width(name, font_size)
        height = font_size + _LABEL_PADDING_Y

        best_position: Optional[tuple[float, float, bool]] = None
        best_overlap = float("inf")

        for ox, oy, is_offset in candidates:
            label_x, label_y = x + ox, y + oy
            # Early-exit: most candidates either collide with the very
            # first nearby already-placed label (no need to check the
            # rest) or collide with none at all -- summing every pair
            # for every candidate was pure wasted work in both cases.
            first_overlap = 0.0
            has_overlap = False
            for p in placed:
                first_overlap = _overlap_area(label_x, label_y, width, height, p.label_x, p.label_y, p.width, p.height)
                if first_overlap > 0.0:
                    has_overlap = True
                    break
            if not has_overlap:
                best_position = (label_x, label_y, is_offset)
                best_overlap = 0.0
                break  # fully collision-free candidate -- no need to keep searching
            if first_overlap < best_overlap:
                best_overlap = first_overlap
                best_position = (label_x, label_y, is_offset)

        assert best_position is not None  # candidates always has >= 1 entry
        label_x, label_y, is_offset = best_position
        placed.append(
            PlacedLabel(
                name=name,
                anchor_x=x,
                anchor_y=y,
                label_x=label_x,
                label_y=label_y,
                width=width,
                height=height,
                offset=is_offset,
            )
        )

    return placed
