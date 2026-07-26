"""Compact statistics grid (active regions, messages, last update, etc.)."""

from __future__ import annotations

import flet as ft

from app.models.alert_models import ThreatSnapshot
from app.ui.theme import colors as theme


class StatsPanel(ft.Container):
    """Four small stat tiles arranged responsively with ``ResponsiveRow``."""

    def __init__(self) -> None:
        """Build the static shell of the stats grid."""
        self._active_value = ft.Text("0", size=20, weight=ft.FontWeight.BOLD, color=theme.TEXT_PRIMARY)
        self._messages_value = ft.Text("0", size=20, weight=ft.FontWeight.BOLD, color=theme.TEXT_PRIMARY)
        self._score_value = ft.Text("0%", size=20, weight=ft.FontWeight.BOLD, color=theme.TEXT_PRIMARY)
        self._updated_value = ft.Text("—", size=20, weight=ft.FontWeight.BOLD, color=theme.TEXT_PRIMARY)

        super().__init__(
            padding=16,
            border_radius=20,
            bgcolor=theme.SURFACE_ELEVATED,
            border=ft.border.all(1, theme.BORDER),
            content=ft.ResponsiveRow(
                spacing=10,
                run_spacing=10,
                controls=[
                    self._stat_tile("Активні області", self._active_value, col={"xs": 6, "md": 3}),
                    self._stat_tile("Повідомлень", self._messages_value, col={"xs": 6, "md": 3}),
                    self._stat_tile("Рівень ризику", self._score_value, col={"xs": 6, "md": 3}),
                    self._stat_tile("Оновлено", self._updated_value, col={"xs": 6, "md": 3}),
                ],
            ),
        )

    @staticmethod
    def _stat_tile(label: str, value_control: ft.Text, col: dict) -> ft.Container:
        """Build one labeled stat tile."""
        return ft.Container(
            col=col,
            padding=12,
            border_radius=14,
            bgcolor=theme.SURFACE,
            content=ft.Column(
                spacing=2,
                controls=[
                    ft.Text(label, size=11, color=theme.TEXT_SECONDARY),
                    value_control,
                ],
            ),
        )

    def update_snapshot(self, snapshot: ThreatSnapshot) -> None:
        """Refresh all stat values from a new threat snapshot."""
        self._active_value.value = str(snapshot.active_regions_count)
        self._messages_value.value = str(snapshot.total_messages_analyzed)
        self._score_value.value = f"{int(round(snapshot.risk_score))}%"
        self._updated_value.value = f"{snapshot.last_update:%H:%M:%S}"
        if self.page is not None:
            self.update()
