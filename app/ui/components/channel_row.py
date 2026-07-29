"""One row in the "Канали" (Channels) list."""

from __future__ import annotations

from typing import Callable

import flet as ft

from app.models.channel_models import TelegramChannel
from app.ui.theme import colors as theme

_CONNECTED_COLOR = "#22C55E"


class ChannelRow(ft.Container):
    """Shows a channel's status and exposes enable/disable/remove actions."""

    def __init__(
        self,
        channel: TelegramChannel,
        on_toggle: Callable[[str, bool], None],
        on_remove: Callable[[str], None],
    ) -> None:
        """Build the row for a given channel with the supplied callbacks."""
        self.channel = channel

        status_color = _CONNECTED_COLOR if channel.connected else theme.TEXT_MUTED

        self._status_dot = ft.Container(
            width=8, height=8, border_radius=4, bgcolor=status_color
        )
        self._title_text = ft.Text(
            channel.display_name or channel.username, size=14, color=theme.TEXT_PRIMARY
        )
        self._meta_text = ft.Text(
            self._build_meta_text(channel), size=11, color=theme.TEXT_SECONDARY
        )
        self._switch = ft.Switch(
            value=channel.enabled,
            active_color=theme.ACCENT_BLUE,
            on_change=lambda e: on_toggle(channel.username, e.control.value),
        )

        super().__init__(
            padding=12,
            border_radius=14,
            bgcolor=theme.SURFACE,
            margin=ft.margin.only(bottom=8),
            content=ft.Row(
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    self._status_dot,
                    ft.Column(
                        spacing=2,
                        expand=True,
                        controls=[self._title_text, self._meta_text],
                    ),
                    self._switch,
                    ft.IconButton(
                        icon=ft.Icons.DELETE_OUTLINE_ROUNDED,
                        icon_color=theme.TEXT_SECONDARY,
                        tooltip="Видалити канал",
                        on_click=lambda e: on_remove(channel.username),
                    ),
                ],
            ),
        )

    @staticmethod
    def _build_meta_text(channel: TelegramChannel) -> str:
        """Build the small status/metadata line under the channel name."""
        status = "підключено" if channel.connected else "не підключено"
        last_update = f"{channel.last_update:%H:%M:%S}" if channel.last_update else "—"
        return f"{status} · повідомлень: {channel.messages_count} · останнє: {last_update}"

    def refresh(self, channel: TelegramChannel) -> None:
        """Update the row's visuals from a fresh channel model instance."""
        self.channel = channel
        self._title_text.value = channel.display_name or channel.username
        self._meta_text.value = self._build_meta_text(channel)
        self._status_dot.bgcolor = _CONNECTED_COLOR if channel.connected else theme.TEXT_MUTED
        self._switch.value = channel.enabled
        if self.page is not None:
            self.update()
