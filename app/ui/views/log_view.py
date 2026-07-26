"""'Лог' (Log journal) tab content."""

from __future__ import annotations

from typing import Callable

import flet as ft

from app.models.log_models import LogEntry
from app.ui.components.log_row import build_log_row
from app.ui.theme import colors as theme

_MAX_VISIBLE_ROWS = 300


class LogView(ft.Column):
    """Shows the full log journal and lets the user clear it."""

    def __init__(self, on_clear: Callable[[], None]) -> None:
        """Build the static shell; call :meth:`set_entries` to populate it."""
        self._on_clear = on_clear
        self._list = ft.ListView(spacing=0, expand=True, auto_scroll=True)

        super().__init__(
            spacing=12,
            expand=True,
            controls=[
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Text(
                            "Журнал подій",
                            size=14,
                            weight=ft.FontWeight.W_600,
                            color=theme.TEXT_SECONDARY,
                            expand=True,
                        ),
                        ft.TextButton(
                            text="Очистити",
                            icon=ft.Icons.CLEANING_SERVICES_ROUNDED,
                            on_click=lambda e: self._on_clear(),
                        ),
                    ],
                ),
                ft.Container(
                    expand=True,
                    padding=12,
                    border_radius=20,
                    bgcolor=theme.SURFACE_ELEVATED,
                    border=ft.border.all(1, theme.BORDER),
                    content=self._list,
                ),
            ],
        )

    def set_entries(self, entries: list[LogEntry]) -> None:
        """Rebuild the visible journal from the full entry list."""
        visible_entries = entries[-_MAX_VISIBLE_ROWS:]
        self._list.controls = [build_log_row(entry) for entry in visible_entries]
        if self.page is not None:
            self.update()

    def append_entry(self, entry: LogEntry) -> None:
        """Append a single new entry without rebuilding the whole list."""
        self._list.controls.append(build_log_row(entry))
        if len(self._list.controls) > _MAX_VISIBLE_ROWS:
            self._list.controls.pop(0)
        if self.page is not None:
            self._list.update()
