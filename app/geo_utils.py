"""
Shared polygon-repair helpers for this project's hand-digitized border
data.

Both ``app.ukraine_geo`` (oblast-level ``REGION_RINGS``) and
``app.data.oblast_districts`` (raion-level ``DISTRICT_RINGS``) are built
from the same class of source data and can suffer the same class of
digitization defect: a short mis-ordered stretch of a ring that loops
back and crosses an earlier edge of the SAME ring ("bowtie"
self-intersection). A point-in-polygon test can register the small loop
this creates as "inside" independently of the polygon's real shape,
which is exactly the kind of small, unexpected extra clickable area
that has previously been reported against this project's maps (see the
Kyiv Oblast / Chernihiv / Cherkasy tap-bleed bug).

This module holds ONE implementation of the repair so both geometry
sources use it identically, instead of two copies that can quietly
drift apart.
"""

from __future__ import annotations

from typing import Optional

Point = tuple[float, float]
Ring = list[Point]


def segments_intersect(a1: Point, a2: Point, b1: Point, b2: Point) -> Optional[Point]:
    """Return the intersection point of two line SEGMENTS (not infinite
    lines), or ``None`` if they don't properly cross.
    """
    x1, y1 = a1
    x2, y2 = a2
    x3, y3 = b1
    x4, y4 = b2
    denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if denom == 0:
        return None  # parallel (or collinear) -- not a proper crossing
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / denom
    u = ((x1 - x3) * (y1 - y2) - (y1 - y3) * (x1 - x2)) / denom
    if not (0.0 < t < 1.0 and 0.0 < u < 1.0):
        return None
    return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))


def repair_self_intersecting_ring(ring: Ring) -> Ring:
    """Remove any self-crossing ("bowtie") loop from one polygon ring.

    Repair: when edges (i, i+1) and (j, j+1) cross, the vertices
    strictly between them (i+1 .. j) form the small looped-back loop --
    replace that whole stretch with the single computed crossing point,
    which keeps the ring a simple (non-self-intersecting) polygon using
    only this ring's own existing geometry plus one computed point, not
    any new/fabricated boundary shape.
    """
    ring = list(ring)
    # A bounded number of passes is enough for the kind of single short
    # mis-ordered stretch this data actually has; re-checks after each
    # repair in case removing one loop exposes another.
    for _ in range(8):
        n = len(ring)
        repaired = False
        for i in range(n):
            a1, a2 = ring[i], ring[(i + 1) % n]
            for j in range(i + 2, n):
                if i == 0 and j == n - 1:
                    continue  # adjacent through the wrap-around, not a crossing
                b1, b2 = ring[j], ring[(j + 1) % n]
                crossing = segments_intersect(a1, a2, b1, b2)
                if crossing is not None:
                    ring = ring[: i + 1] + [crossing] + ring[j + 1 :]
                    repaired = True
                    break
            if repaired:
                break
        if not repaired:
            break
    return ring


def ring_area(ring: Ring) -> float:
    """Shoelace-formula unsigned area of a closed ring, in viewBox units^2."""
    if len(ring) < 3:
        return 0.0
    total = 0.0
    x1, y1 = ring[-1]
    for x2, y2 in ring:
        total += x1 * y2 - x2 * y1
        x1, y1 = x2, y2
    return abs(total) / 2.0


def is_degenerate_ring(ring: Ring) -> bool:
    """A ring that cannot enclose any area at all (fewer than 3 distinct
    points, e.g. a stray back-and-forth digitization artifact) -- not a
    real polygon, just noise left over from a previous data pass.
    """
    return len(set(ring)) < 3
