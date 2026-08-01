"""'Канали' (Channels) tab content."""

from __future__ import annotations

from typing import Callable

import flet as ft

from app.models.channel_models import TelegramChannel
from app.ui.components.channel_row import ChannelRow
from app.ui.theme import colors as theme

OnAddChannel = Callable[[str], None]
OnToggleChannel = Callable[[str, bool], None]
OnRemoveChannel = Callable[[str], None]


class ChannelsView(ft.Column):
    """Lets the user manage the list of monitored Telegram channels."""

    def __init__(
        self,
        on_add: OnAddChannel,
        on_toggle: OnToggleChannel,
        on_remove: OnRemoveChannel,
    ) -> None:
        """Build the static shell; call :meth:`set_channels` to populate it."""
        self._on_add = on_add
        self._on_toggle = on_toggle
        self._on_remove = on_remove
        self._rows: dict[str, ChannelRow] = {}

        self._input = ft.TextField(
            hint_text="@назва_каналу",
            expand=True,
            border_color=theme.BORDER,
            bgcolor=theme.SURFACE,
            color=theme.TEXT_PRIMARY,
            border_radius=12,
            content_padding=ft.padding.symmetric(horizontal=14, vertical=10),
            on_submit=self._handle_add_click,
        )
        self._list = ft.ListView(spacing=0, expand=True)
        self._empty_state = ft.Row(
            spacing=8,
            controls=[
                ft.Icon(ft.Icons.INBOX_OUTLINED, size=16, color=theme.TEXT_MUTED),
                ft.Text(
                    "Ще немає доданих каналів. Додайте перший вище.",
                    size=12,
                    color=theme.TEXT_MUTED,
                ),
            ],
        )

        super().__init__(
            spacing=12,
            expand=True,
            controls=[
                ft.Container(
                    padding=16,
                    border_radius=theme.RADIUS_LG,
                    bgcolor=theme.SURFACE_ELEVATED,
                    border=ft.border.all(1, theme.BORDER),
                    shadow=theme.elevation_shadow(),
                    content=ft.Row(
                        controls=[
                            self._input,
                            ft.IconButton(
                                icon=ft.Icons.ADD_ROUNDED,
                                icon_color=theme.ACCENT_BLUE,
                                tooltip="Додати канал",
                                on_click=self._handle_add_click,
                            ),
                        ],
                    ),
                ),
                ft.Container(
                    expand=True,
                    padding=16,
                    border_radius=theme.RADIUS_LG,
                    bgcolor=theme.SURFACE_ELEVATED,
                    border=ft.border.all(1, theme.BORDER),
                    shadow=theme.elevation_shadow(),
                    content=ft.Column(
                        expand=True,
                        controls=[self._empty_state, self._list],
                    ),
                ),
            ],
        )

    def _handle_add_click(self, _: ft.ControlEvent) -> None:
        """Validate and forward the entered channel username."""
        value = (self._input.value or "").strip()
        if not value:
            return
        self._on_add(value)
        self._input.value = ""
        if self.page is not None:
            self._input.update()

    def set_channels(self, channels: list[TelegramChannel]) -> None:
        """Rebuild the visible list from the full current channel list."""
        self._rows.clear()
        self._list.controls.clear()
        for channel in channels:
            row = ChannelRow(channel, self._on_toggle, self._on_remove)
            self._rows[channel.username] = row
            self._list.controls.append(row)

        self._empty_state.visible = len(channels) == 0
        self._list.visible = len(channels) > 0
        if self.page is not None:
            self.update()

    def refresh_channel(self, channel: TelegramChannel) -> None:
        """Update a single channel row in place (avoids a full rebuild)."""
        row = self._rows.get(channel.username)
        if row is not None:
            row.refresh(channel)
