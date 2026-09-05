"""Small tappable "сервер" quick-status row for the Overview tab.

Mirrors ChannelsQuickRow's exact structure/styling -- same card, same
tap-to-navigate-to-settings pattern -- just for the centralized server
connection instead of the Telegram channel list.
"""

from __future__ import annotations

from typing import Callable, Optional

import flet as ft

from app.ui.theme import colors as theme

_COLOR_CONNECTED = "#22C55E"
_COLOR_DISCONNECTED = theme.TEXT_SECONDARY


class ServerStatusRow(ft.Container):
    """A compact card summarizing server connection + live stats; tap navigates to Налаштування."""

    def __init__(self, on_tap: Optional[Callable[[], None]] = None) -> None:
        """Build the row; call :meth:`set_status`/:meth:`set_stats` to update it."""
        self._on_tap = on_tap

        self._icon = ft.Icon(name=ft.Icons.DNS_ROUNDED, size=20, color=theme.TEXT_MUTED)
        self._status_text = ft.Text(
            "Сервер не налаштовано",
            size=12,
            color=theme.TEXT_SECONDARY,
        )
        self._stats_text = ft.Text(
            "",
            size=11,
            color=theme.TEXT_MUTED,
        )

        super().__init__(
            padding=ft.padding.symmetric(horizontal=16, vertical=14),
            border_radius=theme.RADIUS_LG,
            bgcolor=theme.SURFACE_ELEVATED,
            border=ft.border.all(1, theme.BORDER),
            shadow=theme.elevation_shadow(),
            ink=True,
            animate=theme.ANIM_FAST,
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
                                        "сервер",
                                        size=15,
                                        weight=ft.FontWeight.W_600,
                                        color=theme.TEXT_PRIMARY,
                                    ),
                                    self._status_text,
                                    self._stats_text,
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

    def set_status(self, connected: bool, configured: bool) -> None:
        """Reflect the current connection state.

        ``configured`` distinguishes "never set up" from "set up but
        currently offline" -- both show as disconnected, but with a
        different message, so the person isn't left wondering whether
        they forgot to enter server details or the server is just down.
        """
        if not configured:
            self._status_text.value = "Сервер не налаштовано"
            self._icon.color = theme.TEXT_MUTED
        elif connected:
            self._status_text.value = "Підключено"
            self._icon.color = _COLOR_CONNECTED
        else:
            self._status_text.value = "Немає з'єднання"
            self._icon.color = _COLOR_DISCONNECTED
        self._status_text.color = _COLOR_CONNECTED if connected else theme.TEXT_SECONDARY
        if self.page is not None:
            self.update()

    def set_stats(self, stats: dict) -> None:
        """Update the compact stats line from a server "stats_update" payload."""
        users_online = stats.get("users_online")
        active_threats = stats.get("active_threats")
        messages_processed = stats.get("messages_processed")
        if users_online is None and active_threats is None:
            self._stats_text.value = ""
        else:
            self._stats_text.value = (
                f"Онлайн: {users_online or 0} · Цілі: {active_threats or 0} · "
                f"Повідомлень: {messages_processed or 0}"
            )
        if self.page is not None:
            self._stats_text.update()
