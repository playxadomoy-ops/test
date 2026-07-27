"""One row in the "Лог" (journal) list."""

from __future__ import annotations

import flet as ft

from app.models.log_models import LogEntry
from app.ui.theme import colors as theme


def build_log_row(entry: LogEntry) -> ft.Container:
    """Build a single, compact log row control for a :class:`LogEntry`."""
    level_color = theme.LOG_LEVEL_COLORS.get(entry.level.value, theme.TEXT_SECONDARY)
    return ft.Container(
        padding=ft.padding.symmetric(vertical=6, horizontal=10),
        border_radius=8,
        bgcolor=theme.SURFACE,
        margin=ft.margin.only(bottom=4),
        content=ft.Row(
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.START,
            controls=[
                ft.Text(
                    f"{entry.timestamp:%H:%M:%S}",
                    size=11,
                    color=theme.TEXT_MUTED,
                    width=70,
                ),
                ft.Container(
                    padding=ft.padding.symmetric(horizontal=6, vertical=2),
                    border_radius=6,
                    bgcolor=ft.Colors.with_opacity(0.15, level_color),
                    content=ft.Text(
                        entry.level.value, size=10, color=level_color, weight=ft.FontWeight.W_600
                    ),
                ),
                ft.Text(entry.message, size=12, color=theme.TEXT_PRIMARY, expand=True),
            ],
        ),
    )
