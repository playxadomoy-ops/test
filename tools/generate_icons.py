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


def main() -> None:
    os.makedirs(_OUTPUT_DIR, exist_ok=True)
    from app.ui.theme import colors as theme

    _render_icon("shahed_uav", [_ICON_RING_UAV], theme.THREAT_ICON_UAV)
    _render_icon("cruise_missile", [_ICON_RING_CRUISE], theme.THREAT_ICON_CRUISE)
    _render_icon("ballistic_missile", [_ICON_RING_BALLISTIC], theme.THREAT_ICON_BALLISTIC)
    _render_icon("aircraft", _ICON_RINGS_AIRCRAFT, theme.THREAT_ICON_AIRCRAFT)


if __name__ == "__main__":
    main()
