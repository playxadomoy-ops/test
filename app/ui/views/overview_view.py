"""'Огляд' (Overview) tab content -- the main dashboard screen.

Layout mirrors the project's reference design from top to bottom:
  1. the "тривога" status card (time + colored square + status word),
  2. a tappable "тг канали" quick-access row,
  3. the real geographic Ukraine map,
  4. (below the fold, scrollable) the stats grid and a live feed of the
     most recently analyzed Telegram messages -- kept for functional
     depth rather than dropped, per the project's "no simplification"
     requirement.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

import flet as ft

from app.models.alert_models import Region, RegionState, ThreatSnapshot
from app.models.channel_models import ChannelMessage
from app.ui.components.channels_quick_row import ChannelsQuickRow
from app.ui.components.stats_panel import StatsPanel
from app.ui.components.threat_card import ThreatCard
from app.ui.components.ukraine_map import OnRegionTapped, UkraineMap
from app.ui.theme import colors as theme

_MAX_VISIBLE_MESSAGES = 30


class OverviewView(ft.Column):
    """The main dashboard: alert status, channels, map, stats, and feed."""

    def __init__(
        self,
        on_region_tap: Optional[OnRegionTapped] = None,
        on_open_channels: Optional[Callable[[], None]] = None,
    ) -> None:
        """Build the full dashboard shell."""
        self.threat_card = ThreatCard()
        self.channels_quick_row = ChannelsQuickRow(on_tap=on_open_channels)
        self.ukraine_map = UkraineMap(on_region_tap=on_region_tap)
        self.stats_panel = StatsPanel()
        self._messages_list = ft.ListView(spacing=6, height=260, auto_scroll=True)
        self._messages_empty = ft.Row(
            spacing=8,
            controls=[
                ft.Icon(ft.Icons.MARK_CHAT_UNREAD_OUTLINED, size=16, color=theme.TEXT_MUTED),
                ft.Text(
                    "Повідомлень ще немає. Вони з'являться тут, щойно підключені "
                    "канали почнуть надходити.",
                    size=12,
                    color=theme.TEXT_MUTED,
                ),
            ],
        )

        super().__init__(
            spacing=12,
            expand=True,
            scroll=ft.ScrollMode.ADAPTIVE,
            controls=[
                self.threat_card,
                self.channels_quick_row,
                self.ukraine_map,
                self.stats_panel,
                ft.Container(
                    padding=16,
                    border_radius=theme.RADIUS_LG,
                    bgcolor=theme.SURFACE_ELEVATED,
                    border=ft.border.all(1, theme.BORDER),
                    shadow=theme.elevation_shadow(),
                    content=ft.Column(
                        spacing=8,
                        controls=[
                            ft.Text(
                                "Останні повідомлення",
                                size=14,
                                weight=ft.FontWeight.W_600,
                                color=theme.TEXT_SECONDARY,
                            ),
                            self._messages_empty,
                            self._messages_list,
                        ],
                    ),
                ),
            ],
        )

    # --- Forwarded updates ---------------------------------------------

    def update_snapshot(self, snapshot: ThreatSnapshot) -> None:
        """Refresh the alert card and the stats grid from a new snapshot."""
        self.threat_card.update_snapshot(snapshot)
        self.stats_panel.update_snapshot(snapshot)

    def tick(self, now: datetime) -> None:
        """Advance the live clock on the alert card. Called every second."""
        self.threat_card.tick(now)

    def update_region_states(self, region_states: dict[Region, RegionState]) -> None:
        """Refresh the map's per-region colors."""
        self.ukraine_map.update_region_states(region_states)

    def set_channels_summary(self, total: int, connected: int) -> None:
        """Refresh the "тг канали" quick row's summary line."""
        self.channels_quick_row.set_summary(total, connected)

    def add_message(self, message: ChannelMessage) -> None:
        """Append a newly analyzed message to the live feed."""
        self._messages_empty.visible = False
        risk_note = (
            f"+{message.risk_contribution:.0f}" if message.risk_contribution > 0 else "0"
        )
        row = ft.Container(
            padding=10,
            border_radius=10,
            bgcolor=theme.SURFACE,
            content=ft.Column(
                spacing=2,
                controls=[
                    ft.Row(
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        controls=[
                            ft.Text(
                                message.channel_username,
                                size=11,
                                color=theme.ACCENT_BLUE,
                                weight=ft.FontWeight.W_600,
                            ),
                            ft.Text(
                                f"{message.received_at:%H:%M:%S} · ризик {risk_note}",
                                size=10,
                                color=theme.TEXT_MUTED,
                            ),
                        ],
                    ),
                    ft.Text(message.text, size=12, color=theme.TEXT_PRIMARY, max_lines=3),
                ],
            ),
        )
        self._messages_list.controls.append(row)
        if len(self._messages_list.controls) > _MAX_VISIBLE_MESSAGES:
            self._messages_list.controls.pop(0)
        if self.page is not None:
            self._messages_empty.update()
            self._messages_list.update()
