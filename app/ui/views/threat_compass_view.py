"""'Компас загроз' (Threat Compass) tab content.

Per the current project requirement, this page is UI structure only: a
category summary row, the radar itself, a legend, an update-status
panel, a primary-direction panel, and bottom summary stat cards -- all
driven by :class:`~app.models.compass_models.CompassSnapshot`, which
starts in its explicit empty state (see ``CompassSnapshot.empty()``).
No threat calculation happens in this module; :meth:`set_snapshot` is
the single hook a later real analyzer will call to populate it.
"""

from __future__ import annotations

import math
from typing import Callable, Optional

import flet as ft

from app.models.compass_models import CompassSnapshot
from app.models.movement_models import ThreatType
from app.ui.components.compass_radar import CompassRadar
from app.ui.icon_assets import ICON_AIRCRAFT, ICON_BALLISTIC_MISSILE, ICON_CRUISE_MISSILE, ICON_SHAHED_UAV
from app.ui.theme import colors as theme

_CATEGORY_LABELS: dict[ThreatType, str] = {
    ThreatType.SHAHED: "Shahed",
    ThreatType.MISSILE: "Ракети",
    ThreatType.AIRCRAFT: "Авіація",
}

_CATEGORY_COLORS: dict[ThreatType, str] = {
    ThreatType.SHAHED: theme.THREAT_ICON_UAV,
    ThreatType.MISSILE: theme.THREAT_ICON_BALLISTIC,
    ThreatType.AIRCRAFT: theme.THREAT_ICON_AIRCRAFT,
}

_LEGEND_ENTRIES: tuple[tuple[str, str], ...] = (
    ("Shahed / БПЛА", ICON_SHAHED_UAV),
    ("Крилата ракета", ICON_CRUISE_MISSILE),
    ("Балістична ракета", ICON_BALLISTIC_MISSILE),
    ("Авіація", ICON_AIRCRAFT),
)

_CATEGORY_ICON: dict[ThreatType, str] = {
    ThreatType.SHAHED: ICON_SHAHED_UAV,
    ThreatType.MISSILE: ICON_CRUISE_MISSILE,
    ThreatType.AIRCRAFT: ICON_AIRCRAFT,
}


class ThreatCompassView(ft.Column):
    """The 'Компас загроз' tab: header, cards, radar, legend, stats."""

    def __init__(self, on_open_settings: Optional[Callable[[], None]] = None) -> None:
        self.radar = CompassRadar()

        self._category_cards: dict[ThreatType, dict[str, ft.Text]] = {}
        category_row = ft.ResponsiveRow(
            spacing=8,
            run_spacing=8,
            controls=[self._build_category_card(t) for t in _CATEGORY_LABELS],
        )

        self._updated_value = ft.Text("—", size=13, weight=ft.FontWeight.W_600, color=theme.TEXT_PRIMARY)
        self._online_dot = ft.Container(width=8, height=8, border_radius=4, bgcolor=theme.TEXT_MUTED)
        self._online_label = ft.Text("Офлайн", size=11, color=theme.TEXT_MUTED)

        self._direction_value = ft.Text("—", size=18, weight=ft.FontWeight.BOLD, color=theme.TEXT_PRIMARY)
        self._direction_share = ft.Text("", size=12, color=theme.TEXT_SECONDARY)
        self._direction_activity = ft.Text("Немає даних", size=11, color=theme.TEXT_MUTED)

        self._stat_total = ft.Text("0", size=18, weight=ft.FontWeight.BOLD, color=theme.TEXT_PRIMARY)
        self._stat_nearest = ft.Text("—", size=18, weight=ft.FontWeight.BOLD, color=theme.TEXT_PRIMARY)
        self._stat_nearest_sub = ft.Text("", size=10, color=theme.TEXT_MUTED)
        self._stat_farthest = ft.Text("—", size=18, weight=ft.FontWeight.BOLD, color=theme.TEXT_PRIMARY)
        self._stat_farthest_sub = ft.Text("", size=10, color=theme.TEXT_MUTED)
        self._stat_average = ft.Text("—", size=18, weight=ft.FontWeight.BOLD, color=theme.TEXT_PRIMARY)
        self._stat_level = ft.Text("—", size=18, weight=ft.FontWeight.BOLD, color=theme.TEXT_PRIMARY)

        header = ft.Container(
            padding=ft.padding.symmetric(horizontal=4),
            content=ft.Row(
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Text("Компас загроз", size=16, weight=ft.FontWeight.W_600, color=theme.TEXT_PRIMARY),
                    ft.IconButton(
                        icon=ft.Icons.SETTINGS_ROUNDED,
                        icon_color=theme.TEXT_SECONDARY,
                        tooltip="Налаштування",
                        on_click=lambda e: on_open_settings() if on_open_settings else None,
                    ),
                ],
            ),
        )

        radar_card = ft.Container(
            padding=theme.CARD_PADDING,
            border_radius=theme.RADIUS_LG,
            bgcolor=theme.SURFACE_ELEVATED,
            border=ft.border.all(1, theme.BORDER),
            shadow=theme.elevation_shadow(),
            content=self.radar,
        )

        legend_card = self._build_legend_card()
        status_row = ft.ResponsiveRow(
            spacing=8,
            run_spacing=8,
            controls=[
                self._build_update_status_card(),
                self._build_primary_direction_card(),
            ],
        )
        stats_card = self._build_stats_card()

        super().__init__(
            spacing=12,
            expand=True,
            scroll=ft.ScrollMode.ADAPTIVE,
            controls=[
                header,
                category_row,
                radar_card,
                legend_card,
                status_row,
                stats_card,
            ],
        )

    # --- Public API ------------------------------------------------------

    def resize(self, page_width: float) -> None:
        """Forward a page resize to the radar, same pattern as the other maps."""
        self.radar.resize(page_width)

    def set_snapshot(self, snapshot: CompassSnapshot) -> None:
        """Refresh every value on the page from a new, real snapshot.

        The single hook a later real threat-position analyzer will call.
        Never invoked with fabricated data anywhere in this project --
        callers either pass a real, computed ``CompassSnapshot`` or leave
        the page in its ``CompassSnapshot.empty()`` starting state.
        """
        by_type = {c.threat_type: c for c in snapshot.categories}
        for threat_type, texts in self._category_cards.items():
            summary = by_type.get(threat_type)
            texts["count"].value = f"{summary.target_count if summary else 0} цілей"
            texts["range"].value = summary.range_label if summary else "—"

        if snapshot.farthest_km is not None:
            self.radar.set_max_range(max(200.0, math.ceil(snapshot.farthest_km / 50.0) * 50.0))
        self.radar.set_targets(list(snapshot.targets))

        if snapshot.updated_at is not None:
            self._updated_value.value = f"{snapshot.updated_at:%H:%M:%S}"
        else:
            self._updated_value.value = "—"
        self._online_dot.bgcolor = theme.THREAT_STATUS_CLEAR_COLOR if snapshot.is_online else theme.TEXT_MUTED
        self._online_label.value = "Онлайн" if snapshot.is_online else "Офлайн"
        self._online_label.color = theme.THREAT_STATUS_CLEAR_COLOR if snapshot.is_online else theme.TEXT_MUTED

        direction = snapshot.primary_direction
        self._direction_value.value = direction.direction_label or "—"
        self._direction_share.value = f"{direction.share_percent:.0f}%" if direction.share_percent is not None else ""
        self._direction_activity.value = direction.activity_label or "Немає даних"

        self._stat_total.value = str(snapshot.total_targets)
        self._stat_nearest.value = f"{snapshot.nearest_km:.0f} км" if snapshot.nearest_km is not None else "—"
        self._stat_nearest_sub.value = snapshot.nearest_label or ""
        self._stat_farthest.value = f"{snapshot.farthest_km:.0f} км" if snapshot.farthest_km is not None else "—"
        self._stat_farthest_sub.value = snapshot.farthest_label or ""
        self._stat_average.value = f"{snapshot.average_km:.0f} км" if snapshot.average_km is not None else "—"
        self._stat_level.value = snapshot.threat_level_label or "—"

        if self.page is not None:
            self.update()

    # --- Internal builders -------------------------------------------------

    def _build_category_card(self, threat_type: ThreatType) -> ft.Control:
        count_text = ft.Text("0 цілей", size=13, weight=ft.FontWeight.W_600, color=theme.TEXT_PRIMARY)
        range_text = ft.Text("—", size=11, color=theme.TEXT_MUTED)
        self._category_cards[threat_type] = {"count": count_text, "range": range_text}
        color = _CATEGORY_COLORS[threat_type]
        return ft.Container(
            col={"xs": 6, "md": 3},
            padding=12,
            border_radius=theme.RADIUS_MD,
            bgcolor=theme.SURFACE_ELEVATED,
            border=ft.border.all(1, theme.BORDER),
            content=ft.Column(
                spacing=2,
                controls=[
                    ft.Row(
                        spacing=6,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Image(src=_CATEGORY_ICON[threat_type], width=16, height=16, fit=ft.ImageFit.CONTAIN),
                            ft.Text(_CATEGORY_LABELS[threat_type], size=12, weight=ft.FontWeight.W_600, color=color),
                        ],
                    ),
                    count_text,
                    range_text,
                ],
            ),
        )

    def _build_legend_card(self) -> ft.Control:
        return ft.Container(
            padding=12,
            border_radius=theme.RADIUS_MD,
            bgcolor=theme.SURFACE_ELEVATED,
            border=ft.border.all(1, theme.BORDER),
            content=ft.Column(
                spacing=6,
                controls=[
                    ft.Text("Умовні позначення", size=12, weight=ft.FontWeight.W_600, color=theme.TEXT_SECONDARY),
                    ft.Row(
                        wrap=True,
                        spacing=14,
                        run_spacing=8,
                        controls=[
                            ft.Row(
                                spacing=6,
                                controls=[
                                    ft.Image(src=icon_path, width=16, height=16, fit=ft.ImageFit.CONTAIN),
                                    ft.Text(label, size=11, color=theme.TEXT_SECONDARY),
                                ],
                            )
                            for label, icon_path in _LEGEND_ENTRIES
                        ],
                    ),
                ],
            ),
        )

    def _build_update_status_card(self) -> ft.Control:
        return ft.Container(
            col={"xs": 12, "md": 6},
            padding=12,
            border_radius=theme.RADIUS_MD,
            bgcolor=theme.SURFACE_ELEVATED,
            border=ft.border.all(1, theme.BORDER),
            content=ft.Column(
                spacing=4,
                controls=[
                    ft.Text("Оновлення", size=11, color=theme.TEXT_SECONDARY),
                    self._updated_value,
                    ft.Row(spacing=6, controls=[self._online_dot, self._online_label]),
                ],
            ),
        )

    def _build_primary_direction_card(self) -> ft.Control:
        return ft.Container(
            col={"xs": 12, "md": 6},
            padding=12,
            border_radius=theme.RADIUS_MD,
            bgcolor=theme.SURFACE_ELEVATED,
            border=ft.border.all(1, theme.BORDER),
            content=ft.Column(
                spacing=4,
                controls=[
                    ft.Text("Основний напрямок", size=11, color=theme.TEXT_SECONDARY),
                    ft.Row(
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.END,
                        controls=[self._direction_value, self._direction_share],
                    ),
                    self._direction_activity,
                ],
            ),
        )

    def _build_stats_card(self) -> ft.Control:
        return ft.Container(
            padding=16,
            border_radius=theme.RADIUS_LG,
            bgcolor=theme.SURFACE_ELEVATED,
            border=ft.border.all(1, theme.BORDER),
            shadow=theme.elevation_shadow(),
            content=ft.Column(
                spacing=10,
                controls=[
                    ft.Text("Зведена статистика", size=14, weight=ft.FontWeight.W_600, color=theme.TEXT_SECONDARY),
                    ft.ResponsiveRow(
                        spacing=10,
                        run_spacing=10,
                        controls=[
                            self._stat_tile("Всього цілей", self._stat_total, col={"xs": 6, "md": 2}),
                            self._stat_tile(
                                "Найближча ціль", self._stat_nearest, col={"xs": 6, "md": 2}, sub=self._stat_nearest_sub
                            ),
                            self._stat_tile(
                                "Найдальша ціль", self._stat_farthest, col={"xs": 6, "md": 2}, sub=self._stat_farthest_sub
                            ),
                            self._stat_tile("Середня відстань", self._stat_average, col={"xs": 6, "md": 2}),
                            self._stat_tile("Рівень загрози", self._stat_level, col={"xs": 12, "md": 4}),
                        ],
                    ),
                ],
            ),
        )

    @staticmethod
    def _stat_tile(label: str, value_control: ft.Text, col: dict, sub: Optional[ft.Text] = None) -> ft.Container:
        column_controls: list[ft.Control] = [
            ft.Text(label, size=11, color=theme.TEXT_SECONDARY),
            value_control,
        ]
        if sub is not None:
            column_controls.append(sub)
        return ft.Container(
            col=col,
            padding=12,
            border_radius=theme.RADIUS_MD,
            bgcolor=theme.SURFACE,
            content=ft.Column(spacing=2, controls=column_controls),
        )
