"""
One small, reusable builder for "a real icon asset with a subtle glow" --
used everywhere this app shows a threat/status glyph outside the map
itself (the movement list, dialogs, status panels), so they all get the
exact same treatment instead of each call site building its own
Image+BoxShadow combination slightly differently.
"""

from __future__ import annotations

import flet as ft


def icon_glyph(asset_path: str, color: str, size: int = 24) -> ft.Control:
    """A real PNG icon (see ``app.ui.icon_assets``) at list/dialog size
    (20-28px), with the slight glow this project's dark theme calls for.

    ``color`` is the icon's own accent color (e.g.
    ``theme.THREAT_ICON_UAV``) -- used only for the glow tint, so the
    glow always matches the icon it surrounds rather than a fixed,
    generic shadow color.
    """
    return ft.Container(
        width=size,
        height=size,
        shadow=ft.BoxShadow(
            spread_radius=0,
            blur_radius=size * 0.5,
            color=ft.Colors.with_opacity(0.45, color),
        ),
        content=ft.Image(src=asset_path, width=size, height=size, fit=ft.ImageFit.CONTAIN),
    )
