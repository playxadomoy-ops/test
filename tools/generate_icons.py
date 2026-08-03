"""
Generate assets/icons/*.png -- the four threat-type icon assets used by
the Рух загроз map and the Threat Compass legend.

Rasterizes THIS PROJECT'S OWN existing vector icon geometry (the same
``_ICON_RING_*`` point sets already defined and used in
``app.ui.components.movement_map`` for on-canvas drawing) at a large
supersampled size and downsamples with high-quality resampling for
crisp, anti-aliased edges at small on-screen sizes -- not a new/separate
icon design, just the same shapes as real transparent PNG assets.

Usage:  python3 tools/generate_icons.py
"""

from __future__ import annotations

import math
import os
import sys

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.ui.components.movement_map import (  # noqa: E402
    _ICON_RING_BALLISTIC,
    _ICON_RING_CRUISE,
    _ICON_RING_UAV,
    _ICON_RINGS_AIRCRAFT,
)

#: Supersample canvas size (rendered this large, then downsampled) -- gives
#: clean anti-aliased edges even at the small on-screen sizes (~20-28px)
#: these icons are actually displayed at.
_SUPERSAMPLE = 512
_OUTPUT_SIZE = 128
_ICON_SPAN = 34.0  # local coordinate half-extent to fit within the canvas, with margin

_OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "assets", "icons")


def _darken(hex_color: str, factor: float) -> str:
    """Return ``hex_color`` scaled toward black by ``factor`` (0..1)."""
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (0, 2, 4))
    return f"#{int(r * factor):02x}{int(g * factor):02x}{int(b * factor):02x}"


def _lighten(hex_color: str, factor: float) -> str:
    """Return ``hex_color`` blended toward white by ``factor`` (0..1)."""
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (0, 2, 4))
    r = int(r + (255 - r) * factor)
    g = int(g + (255 - g) * factor)
    b = int(b + (255 - b) * factor)
    return f"#{r:02x}{g:02x}{b:02x}"


def _to_canvas(point: tuple[float, float]) -> tuple[float, float]:
    """Local icon-space (x right, y up, origin center) -> image pixel space."""
    scale = _SUPERSAMPLE / (2 * _ICON_SPAN)
    x, y = point
    return (_SUPERSAMPLE / 2 + x * scale, _SUPERSAMPLE / 2 - y * scale)


def _render_icon(name: str, rings: list[list[tuple[float, float]]], color: str) -> None:
    image = Image.new("RGBA", (_SUPERSAMPLE, _SUPERSAMPLE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    outline = _darken(color, 0.55)
    highlight = _lighten(color, 0.35)
    stroke_width = max(2, int(_SUPERSAMPLE * 0.012))

    for ring in rings:
        polygon = [_to_canvas(p) for p in ring]
        draw.polygon(polygon, fill=color, outline=outline, width=stroke_width)

    # A thin lighter centerline along the long (nose-to-tail) axis of the
    # largest ring only, for a bit of visual depth -- purely cosmetic,
    # doesn't change the silhouette used for hit-testing/rotation.
    largest_ring = max(rings, key=len)
    nose = max(largest_ring, key=lambda p: p[0])
    tail = min(largest_ring, key=lambda p: p[0])
    draw.line(
        [_to_canvas(nose), _to_canvas(tail)],
        fill=highlight,
        width=max(1, stroke_width // 3),
    )

    final = image.resize((_OUTPUT_SIZE, _OUTPUT_SIZE), Image.LANCZOS)
    out_path = os.path.join(_OUTPUT_DIR, f"{name}.png")
    final.save(out_path, "PNG")
    print(f"wrote {out_path} ({final.mode}, {final.size})")


def _render_polygon_icon(name: str, points: list[tuple[float, float]], color: str, outline_factor: float = 0.55) -> None:
    """Render one arbitrary polygon (not from the weapon-type ring tables
    above) the same way -- same supersample/downsample/outline treatment,
    for the status icons (explosion, siren, nationwide warning) that
    aren't a weapon silhouette.
    """
    image = Image.new("RGBA", (_SUPERSAMPLE, _SUPERSAMPLE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    outline = _darken(color, outline_factor)
    stroke_width = max(2, int(_SUPERSAMPLE * 0.012))
    draw.polygon([_to_canvas(p) for p in points], fill=color, outline=outline, width=stroke_width)
    final = image.resize((_OUTPUT_SIZE, _OUTPUT_SIZE), Image.LANCZOS)
    out_path = os.path.join(_OUTPUT_DIR, f"{name}.png")
    final.save(out_path, "PNG")
    print(f"wrote {out_path} ({final.mode}, {final.size})")


def _explosion_points(spikes: int = 8, outer: float = 30.0, inner: float = 13.0) -> list[tuple[float, float]]:
    """An 8-point starburst -- "target destroyed" glyph."""
    points: list[tuple[float, float]] = []
    for i in range(spikes * 2):
        radius = outer if i % 2 == 0 else inner
        angle = math.pi * i / spikes
        points.append((radius * math.cos(angle), radius * math.sin(angle)))
    return points


def _render_siren_icon(name: str, color: str, active: bool) -> None:
    """Air-raid status glyph: a filled dot, with radiating arcs only when
    active (an at-rest circle alone reads as "calm"/all-clear).
    """
    image = Image.new("RGBA", (_SUPERSAMPLE, _SUPERSAMPLE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    cx, cy = _SUPERSAMPLE / 2, _SUPERSAMPLE / 2
    core_r = _SUPERSAMPLE * 0.11
    draw.ellipse([cx - core_r, cy - core_r, cx + core_r, cy + core_r], fill=color, outline=_darken(color, 0.55), width=max(2, int(_SUPERSAMPLE * 0.012)))

    if active:
        arc_width = max(3, int(_SUPERSAMPLE * 0.018))
        for ring_r in (_SUPERSAMPLE * 0.20, _SUPERSAMPLE * 0.30):
            for start, end in ((205, 335), (25, 155)):
                draw.arc(
                    [cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r],
                    start=start, end=end, fill=color, width=arc_width,
                )

    final = image.resize((_OUTPUT_SIZE, _OUTPUT_SIZE), Image.LANCZOS)
    out_path = os.path.join(_OUTPUT_DIR, f"{name}.png")
    final.save(out_path, "PNG")
    print(f"wrote {out_path} ({final.mode}, {final.size})")


def _render_nationwide_warning_icon(name: str, color: str) -> None:
    """A warning triangle with an exclamation mark -- nationwide alert glyph."""
    image = Image.new("RGBA", (_SUPERSAMPLE, _SUPERSAMPLE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    triangle = [_to_canvas(p) for p in ((0, 30), (-28, -22), (28, -22))]
    draw.polygon(triangle, fill=color, outline=_darken(color, 0.55), width=max(2, int(_SUPERSAMPLE * 0.012)))

    cx = _SUPERSAMPLE / 2
    mark_color = "#0A0E14"  # theme.BACKGROUND -- dark mark on the bright triangle
    bar_top_y, bar_bottom_y = _to_canvas((0, 12))[1], _to_canvas((0, -6))[1]
    bar_w = _SUPERSAMPLE * 0.028
    draw.rounded_rectangle([cx - bar_w, bar_top_y, cx + bar_w, bar_bottom_y], radius=bar_w, fill=mark_color)
    dot_r = _SUPERSAMPLE * 0.032
    dot_y = _to_canvas((0, -14))[1]
    draw.ellipse([cx - dot_r, dot_y - dot_r, cx + dot_r, dot_y + dot_r], fill=mark_color)

    final = image.resize((_OUTPUT_SIZE, _OUTPUT_SIZE), Image.LANCZOS)
    out_path = os.path.join(_OUTPUT_DIR, f"{name}.png")
    final.save(out_path, "PNG")
    print(f"wrote {out_path} ({final.mode}, {final.size})")


def main() -> None:
    os.makedirs(_OUTPUT_DIR, exist_ok=True)
    from app.ui.theme import colors as theme

    _render_icon("shahed_uav", [_ICON_RING_UAV], theme.THREAT_ICON_UAV)
    _render_icon("cruise_missile", [_ICON_RING_CRUISE], theme.THREAT_ICON_CRUISE)
    _render_icon("ballistic_missile", [_ICON_RING_BALLISTIC], theme.THREAT_ICON_BALLISTIC)
    _render_icon("aircraft", _ICON_RINGS_AIRCRAFT, theme.THREAT_ICON_AIRCRAFT)

    _render_polygon_icon("explosion", _explosion_points(), theme.REGION_ACTIVE_COLOR)
    _render_siren_icon("siren_active", theme.REGION_ACTIVE_COLOR, active=True)
    _render_siren_icon("siren_clear", theme.THREAT_STATUS_CLEAR_COLOR, active=False)
    _render_nationwide_warning_icon("nationwide_warning", theme.THREAT_ICON_UAV)


if __name__ == "__main__":
    main()
