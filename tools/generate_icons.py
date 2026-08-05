"""
Generate this project's real icon assets:
  * assets/icons/{shahed_uav,cruise_missile,ballistic_missile,aircraft,
    explosion,nationwide_warning}.svg -- hand-authored vector silhouettes,
    one per threat/status type. SVG (not raster) so they stay perfectly
    sharp at any on-screen size on both Desktop and Android.
  * assets/icons/{siren_active,siren_clear}.png -- the air-raid status
    glyphs (not weapon silhouettes, so they stay as simple rasterized
    dot+arc icons; unaffected by the SVG conversion above).

Every SVG uses a tight 0 0 100 100 viewBox with the silhouette itself
filling ~88% of the canvas (minimal padding), nose/business-end pointing
along +x (angle 0) for consistency with the app's rotate-to-bearing
convention, and the same stroke width / stroke color treatment across
all six for one consistent visual style.

Usage:  python3 tools/generate_icons.py
"""

from __future__ import annotations

import math
import os
import sys

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "assets", "icons")

#: Shared stroke treatment for every SVG icon -- same width/color/join
#: style everywhere, so all six read as one consistent icon family
#: rather than six independently-styled images.
_SVG_STROKE_WIDTH = 2.4
_SVG_STROKE_LINEJOIN = "round"


def _darken(hex_color: str, factor: float) -> str:
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (0, 2, 4))
    return f"#{int(r * factor):02x}{int(g * factor):02x}{int(b * factor):02x}"


def _lighten(hex_color: str, factor: float) -> str:
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (0, 2, 4))
    r = int(r + (255 - r) * factor)
    g = int(g + (255 - g) * factor)
    b = int(b + (255 - b) * factor)
    return f"#{r:02x}{g:02x}{b:02x}"


def _ring_path(ring: list[tuple[float, float]]) -> str:
    head = ring[0]
    tail = " ".join(f"L{x:.2f},{y:.2f}" for x, y in ring[1:])
    return f"M{head[0]:.2f},{head[1]:.2f} {tail} Z"


def _write_svg_icon(
    name: str,
    rings: list[list[tuple[float, float]]],
    color: str,
    detail_lines: "list[tuple[tuple[float, float], tuple[float, float]]] | None" = None,
) -> None:
    """Write one SVG icon: filled/stroked silhouette rings, plus optional
    thin lighter detail lines (fuselage seams, panel lines) for a more
    "professional military" look than a flat silhouette alone.
    """
    outline = _darken(color, 0.5)
    highlight = _lighten(color, 0.4)

    body_paths = "".join(
        f'<path d="{_ring_path(ring)}" fill="{color}" stroke="{outline}" '
        f'stroke-width="{_SVG_STROKE_WIDTH}" stroke-linejoin="{_SVG_STROKE_LINEJOIN}"/>'
        for ring in rings
    )
    detail_paths = ""
    if detail_lines:
        for (x1, y1), (x2, y2) in detail_lines:
            detail_paths += (
                f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
                f'stroke="{highlight}" stroke-width="{_SVG_STROKE_WIDTH * 0.4:.2f}" '
                f'stroke-linecap="round" opacity="0.85"/>'
            )

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        f"{body_paths}{detail_paths}"
        "</svg>"
    )
    out_path = os.path.join(_OUTPUT_DIR, f"{name}.svg")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(svg)
    print(f"wrote {out_path}")


# --- Weapon-type silhouettes (nose points +x, viewBox 0 0 100 100) --------

#: Shahed/kamikaze-UAV flying wing -- bigger and more detailed than a
#: plain kite: a swept delta wing with a concave trailing edge (the
#: distinctive "M"-shaped tail typical of this airframe family), not
#: just a 4-point diamond.
_SHAHED_UAV_RING = [
    (95, 50),   # nose
    (55, 37),   # upper body shoulder
    (6, 12),    # upper wingtip (swept far back)
    (36, 50),   # tail notch (concave -- the characteristic "M" wing)
    (6, 88),    # lower wingtip
    (55, 63),   # lower body shoulder
]

#: Cruise missile -- slim cylindrical body, pointed nose, cross-tail fins.
_CRUISE_MISSILE_BODY = [
    (94, 50), (76, 43), (24, 43), (24, 57), (76, 57),
]
_CRUISE_MISSILE_FIN_UPPER = [(24, 43), (5, 24), (26, 41)]
_CRUISE_MISSILE_FIN_LOWER = [(24, 57), (5, 76), (26, 59)]

#: Ballistic missile -- noticeably THICKER body than the cruise missile,
#: blunter re-entry-vehicle nose, larger fins.
_BALLISTIC_MISSILE_BODY = [
    (92, 50), (66, 30), (26, 34), (26, 66), (66, 70),
]
_BALLISTIC_MISSILE_FIN_UPPER = [(26, 34), (3, 10), (29, 32)]
_BALLISTIC_MISSILE_FIN_LOWER = [(26, 66), (3, 90), (29, 68)]

#: Generic/carrier aircraft -- fuselage + large swept wings + small
#: tailplane, recognizable as a fixed-wing jet from directly above.
_AIRCRAFT_FUSELAGE = [
    (92, 50), (72, 46), (18, 47), (10, 50), (18, 53), (72, 54),
]
_AIRCRAFT_WING_UPPER = [(60, 49), (16, 8), (36, 47)]
_AIRCRAFT_WING_LOWER = [(60, 51), (16, 92), (36, 53)]
_AIRCRAFT_TAIL_UPPER = [(20, 49), (7, 34), (17, 48)]
_AIRCRAFT_TAIL_LOWER = [(20, 51), (7, 66), (17, 52)]


def _explosion_points(spikes: int = 10, outer: float = 46.0, inner: float = 17.0) -> list[tuple[float, float]]:
    """An irregular starburst (alternating spike length varied slightly
    per-point) -- reads as more "explosive"/dynamic than a perfectly
    regular star.
    """
    points: list[tuple[float, float]] = []
    for i in range(spikes * 2):
        base_radius = outer if i % 2 == 0 else inner
        # Small deterministic variation per spike for an irregular,
        # non-mechanical silhouette (not random -- same output every run).
        wobble = 1.0 + 0.12 * math.sin(i * 2.3)
        radius = base_radius * wobble
        angle = math.pi * i / spikes - math.pi / 2
        points.append((50 + radius * math.cos(angle), 50 + radius * math.sin(angle)))
    return points


def _nationwide_warning_svg(name: str, color: str) -> None:
    """Warning triangle + exclamation mark, filling most of the canvas."""
    outline = _darken(color, 0.5)
    mark_color = "#0A0E14"  # theme.BACKGROUND -- dark mark on the bright triangle
    triangle = "M50,6 L94,90 L6,90 Z"
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        f'<path d="{triangle}" fill="{color}" stroke="{outline}" '
        f'stroke-width="{_SVG_STROKE_WIDTH}" stroke-linejoin="round"/>'
        f'<rect x="45.5" y="38" width="9" height="28" rx="4.5" fill="{mark_color}"/>'
        f'<circle cx="50" cy="76" r="5.2" fill="{mark_color}"/>'
        "</svg>"
    )
    out_path = os.path.join(_OUTPUT_DIR, f"{name}.svg")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(svg)
    print(f"wrote {out_path}")


# --- Siren status icons (rasterized PNG -- unchanged, not weapon glyphs) --

_PNG_SUPERSAMPLE = 512
_PNG_OUTPUT_SIZE = 128
_PNG_ICON_SPAN = 34.0


def _render_siren_icon(name: str, color: str, active: bool) -> None:
    image = Image.new("RGBA", (_PNG_SUPERSAMPLE, _PNG_SUPERSAMPLE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    cx, cy = _PNG_SUPERSAMPLE / 2, _PNG_SUPERSAMPLE / 2
    core_r = _PNG_SUPERSAMPLE * 0.11
    draw.ellipse(
        [cx - core_r, cy - core_r, cx + core_r, cy + core_r],
        fill=color, outline=_darken(color, 0.55), width=max(2, int(_PNG_SUPERSAMPLE * 0.012)),
    )
    if active:
        arc_width = max(3, int(_PNG_SUPERSAMPLE * 0.018))
        for ring_r in (_PNG_SUPERSAMPLE * 0.20, _PNG_SUPERSAMPLE * 0.30):
            for start, end in ((205, 335), (25, 155)):
                draw.arc(
                    [cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r],
                    start=start, end=end, fill=color, width=arc_width,
                )
    final = image.resize((_PNG_OUTPUT_SIZE, _PNG_OUTPUT_SIZE), Image.LANCZOS)
    out_path = os.path.join(_OUTPUT_DIR, f"{name}.png")
    final.save(out_path, "PNG")
    print(f"wrote {out_path} ({final.mode}, {final.size})")


def main() -> None:
    os.makedirs(_OUTPUT_DIR, exist_ok=True)
    from app.ui.theme import colors as theme

    _write_svg_icon(
        "shahed_uav", [_SHAHED_UAV_RING], theme.THREAT_ICON_UAV,
        detail_lines=[((95, 50), (36, 50))],
    )
    _write_svg_icon(
        "cruise_missile",
        [_CRUISE_MISSILE_BODY, _CRUISE_MISSILE_FIN_UPPER, _CRUISE_MISSILE_FIN_LOWER],
        theme.THREAT_ICON_CRUISE,
        detail_lines=[((94, 50), (24, 50))],
    )
    _write_svg_icon(
        "ballistic_missile",
        [_BALLISTIC_MISSILE_BODY, _BALLISTIC_MISSILE_FIN_UPPER, _BALLISTIC_MISSILE_FIN_LOWER],
        theme.THREAT_ICON_BALLISTIC,
        detail_lines=[((92, 50), (26, 50)), ((66, 30), (66, 70))],
    )
    _write_svg_icon(
        "aircraft",
        [_AIRCRAFT_FUSELAGE, _AIRCRAFT_WING_UPPER, _AIRCRAFT_WING_LOWER, _AIRCRAFT_TAIL_UPPER, _AIRCRAFT_TAIL_LOWER],
        theme.THREAT_ICON_AIRCRAFT,
        detail_lines=[((92, 50), (10, 50))],
    )
    _write_svg_icon("explosion", [_explosion_points()], theme.REGION_ACTIVE_COLOR)
    _nationwide_warning_svg("nationwide_warning", theme.THREAT_ICON_UAV)

    _render_siren_icon("siren_active", theme.REGION_ACTIVE_COLOR, active=True)
    _render_siren_icon("siren_clear", theme.THREAT_STATUS_CLEAR_COLOR, active=False)


if __name__ == "__main__":
    main()
