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
_COLOR_WAITING = "#F59E0B"
_COLOR_SEARCHING = theme.TEXT_SECONDARY
_COLOR_ERROR = "#EF4444"

#: status_code (from ServerClient's status listener) -> (emoji, color).
_STATUS_STYLE: dict[str, tuple[str, str]] = {
    "discovering": ("\U0001F7E1", _COLOR_SEARCHING),
    "waiting_approval": ("\U0001F7E0", _COLOR_WAITING),
    "connected": ("\U0001F7E2", _COLOR_CONNECTED),
    "disconnected": ("\U0001F534", _COLOR_ERROR),
    "rejected": ("\U0001F534", _COLOR_ERROR),
}


class ServerStatusRow(ft.Container):
    """A compact card summarizing server connection + live stats; tap navigates to Налаштування.

    Fully automatic, per the project's zero-configuration requirement:
    this row only ever displays a status the app arrived at on its
    own (searching / waiting for approval / connected / unavailable)
    -- there's nothing here for the user to configure or a "Connect"
    button to press.
    """

    def __init__(self, on_tap: Optional[Callable[[], None]] = None) -> None:
        """Build the row; call :meth:`set_registration_status`/:meth:`set_stats` to update it."""
        self._on_tap = on_tap

        self._icon_text = ft.Text("\U0001F7E1", size=14)
        self._status_text = ft.Text(
            "Пошук сервера...",
            size=12,
            color=_COLOR_SEARCHING,
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
                            self._icon_text,
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

    def set_registration_status(self, status_code: str, message: str) -> None:
        """Reflect the current connection state (see ServerClient's OnStatusChanged).

        ``status_code`` is one of "discovering", "waiting_approval",
        "connected", "disconnected", "rejected" -- an unrecognized code
        falls back to the "disconnected" look rather than raising, so a
        future new status from ServerClient degrades gracefully instead
        of crashing the Dashboard.
        """
        emoji, color = _STATUS_STYLE.get(status_code, _STATUS_STYLE["disconnected"])
        self._icon_text.value = emoji
        self._status_text.value = message
        self._status_text.color = color
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
