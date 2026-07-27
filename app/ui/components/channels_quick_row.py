"""Small tappable "тг канали" quick-access row for the Overview tab."""

from __future__ import annotations

from typing import Callable, Optional

import flet as ft

from app.ui.theme import colors as theme


class ChannelsQuickRow(ft.Container):
    """A compact card summarizing channel count; tap navigates to "Канали"."""

    def __init__(self, on_tap: Optional[Callable[[], None]] = None) -> None:
        """Build the row; call :meth:`set_summary` to update its counts."""
        self._on_tap = on_tap

        self._icon = ft.Icon(name=ft.Icons.SEND_ROUNDED, size=20, color=theme.ACCENT_BLUE)
        self._summary_text = ft.Text(
            "Немає підключених каналів",
            size=12,
            color=theme.TEXT_SECONDARY,
        )

        super().__init__(
            padding=ft.padding.symmetric(horizontal=16, vertical=14),
            border_radius=20,
            bgcolor=theme.SURFACE_ELEVATED,
            border=ft.border.all(1, theme.BORDER),
            ink=True,
            on_click=self._handle_tap,
            content=ft.Row(
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Row(
                        spacing=10,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            self._icon,
                            ft.Column(
                                spacing=2,
                                controls=[
                                    ft.Text(
                                        "тг канали",
                                        size=15,
                                        weight=ft.FontWeight.W_600,
                                        color=theme.TEXT_PRIMARY,
                                    ),
                                    self._summary_text,
                                ],
                            ),
                        ],
                    ),
                    ft.Icon(
                        name=ft.Icons.CHEVRON_RIGHT_ROUNDED,
                        size=22,
                        color=theme.TEXT_MUTED,
                    ),
                ],
            ),
        )

    def _handle_tap(self, _: ft.ControlEvent) -> None:
        """Forward the tap to the parent-supplied navigation callback."""
        if self._on_tap is not None:
            self._on_tap()

    def set_summary(self, total: int, connected: int) -> None:
        """Update the small summary line under the "тг канали" title."""
        if total == 0:
            self._summary_text.value = "Немає підключених каналів"
        else:
            self._summary_text.value = f"Підключено {connected} з {total}"
        if self.page is not None:
            self._summary_text.update()
