"""Main "тривога" status card at the top of the Overview tab.

Visually modeled on the project's reference design: a bordered clock-style
time readout on the left, and a glowing colored square with a short status
word (e.g. "Чисто") on the right. The clock ticks live every second
(:meth:`tick`, driven by main.py's background loop); the color, status
word, and meta line react to the current :class:`ThreatSnapshot`
(:meth:`update_snapshot`).
"""

from __future__ import annotations

from datetime import datetime

import flet as ft

from app.models.alert_models import ApiStatus, RiskLevel, ThreatSnapshot
from app.ui.theme import colors as theme


class ThreatCard(ft.Container):
    """A card whose clock ticks live and whose color/status track the snapshot."""

    def __init__(self) -> None:
        """Build the static shell; call :meth:`tick`/:meth:`update_snapshot` to refresh it."""
        self._time_text = ft.Text(
            f"{datetime.now():%H:%M}",
            size=32,
            weight=ft.FontWeight.BOLD,
            color=theme.TEXT_PRIMARY,
            style=ft.TextStyle(letter_spacing=1.5),
        )
        self._time_box = ft.Container(
            padding=ft.padding.symmetric(horizontal=18, vertical=14),
            border_radius=16,
            border=ft.border.all(1.5, theme.BORDER),
            alignment=ft.alignment.center,
            content=self._time_text,
        )

        # --- Indicator 1: "Статус загрози" -- plain yes/no, independent
        # of the calculated risk score (see ThreatSnapshot.has_active_threat).
        # Same square design/glow/animation as Indicator 2, just a binary
        # green/red palette instead of the graduated risk-level one.
        self._threat_square = ft.Container(
            width=72,
            height=72,
            border_radius=18,
            bgcolor=theme.THREAT_STATUS_CLEAR_COLOR,
            animate=ft.Animation(300, ft.AnimationCurve.EASE_OUT),
        )
        self._threat_status_text = ft.Text(
            "Завантаження...",
            size=15,
            weight=ft.FontWeight.W_700,
            color=theme.TEXT_SECONDARY,
        )
        self._threat_label = ft.Text(
            "статус загрози",
            size=10,
            weight=ft.FontWeight.W_600,
            color=theme.TEXT_MUTED,
        )

        # --- Indicator 2: "Рівень ризику" -- the existing graduated
        # risk-level indicator (unchanged colors/behavior), just labeled.
        self._square = ft.Container(
            width=72,
            height=72,
            border_radius=18,
            bgcolor=theme.RISK_COLORS[RiskLevel.NONE],
            animate=ft.Animation(300, ft.AnimationCurve.EASE_OUT),
        )
        self._status_text = ft.Text(
            "Завантаження...",
            size=15,
            weight=ft.FontWeight.W_700,
            color=theme.TEXT_SECONDARY,
        )
        self._risk_label = ft.Text(
            "рівень ризику",
            size=10,
            weight=ft.FontWeight.W_600,
            color=theme.TEXT_MUTED,
        )
        self._meta_text = ft.Text("", size=11, color=theme.TEXT_MUTED)
        self._warning_banner = ft.Container(
            visible=False,
            padding=ft.padding.symmetric(horizontal=10, vertical=6),
            border_radius=8,
            bgcolor=ft.Colors.with_opacity(0.15, theme.WARNING_COLOR),
            content=ft.Text(
                "",
                size=11,
                weight=ft.FontWeight.W_600,
                color=theme.WARNING_COLOR,
                max_lines=2,
            ),
        )

        super().__init__(
            padding=20,
            border_radius=20,
            bgcolor=theme.SURFACE_ELEVATED,
            border=ft.border.all(1, theme.BORDER),
            animate=ft.Animation(300, ft.AnimationCurve.EASE_OUT),
            content=ft.Column(
                spacing=14,
                controls=[
                    ft.Text(
                        "тривога",
                        size=13,
                        weight=ft.FontWeight.W_600,
                        color=theme.TEXT_SECONDARY,
                    ),
                    ft.Row(
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            self._time_box,
                            ft.Row(
                                spacing=10,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                controls=[
                                    ft.Column(
                                        spacing=4,
                                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                                        controls=[
                                            self._threat_square,
                                            self._threat_status_text,
                                            self._threat_label,
                                        ],
                                    ),
                                    ft.Column(
                                        spacing=4,
                                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                                        controls=[self._square, self._status_text, self._risk_label],
                                    ),
                                ],
                            ),
                        ],
                    ),
                    self._warning_banner,
                    self._meta_text,
                ],
            ),
        )

    def did_mount(self) -> None:
        """Show the correct current time the instant the card is mounted."""
        self.tick(datetime.now())

    def tick(self, now: datetime) -> None:
        """Advance the live clock. Called every second from the background loop."""
        new_value = f"{now:%H:%M}"
        if self._time_text.value == new_value:
            return
        self._time_text.value = new_value
        if self.page is not None:
            self._time_text.update()

    def update_snapshot(self, snapshot: ThreatSnapshot) -> None:
        """Refresh the card's color, status word, and meta line from a new snapshot.

        Critically: if the official alerts.in.ua source failed
        (``ApiStatus.ERROR``) and nothing else (Telegram analysis)
        indicates danger either, this must NOT show the green "Чисто"
        all-clear -- that would be a false negative. It shows an explicit
        "Дані недоступні" warning instead.
        """
        api_unavailable = snapshot.api_status == ApiStatus.ERROR
        looks_clean = snapshot.overall_risk == RiskLevel.NONE

        if api_unavailable and looks_clean:
            # We have no real signal at all right now -- neither confirmed
            # clean nor confirmed dangerous. Never claim "Чисто" here.
            color = theme.WARNING_COLOR
            self._status_text.value = "Дані недоступні"
        else:
            color = theme.risk_color(snapshot.overall_risk)
            self._status_text.value = snapshot.overall_risk.short_label_uk

        self._status_text.color = color

        if api_unavailable:
            reason = snapshot.api_error_message or "невідома причина"
            self._warning_banner.visible = True
            self._warning_banner.content.value = f"⚠ alerts.in.ua недоступне: {reason}"
        else:
            self._warning_banner.visible = False

        self._meta_text.value = (
            f"Активних областей: {snapshot.active_regions_count} · "
            f"Повідомлень: {snapshot.total_messages_analyzed} · "
            f"Рівень: {int(round(snapshot.risk_score))}% · "
            f"Дані: {snapshot.last_update:%H:%M:%S}"
        )

        self._square.bgcolor = color
        self._square.shadow = ft.BoxShadow(
            spread_radius=1,
            blur_radius=22,
            color=ft.Colors.with_opacity(0.55, color),
            offset=ft.Offset(0, 0),
        )
        self.border = ft.border.all(1, ft.Colors.with_opacity(0.4, color))

        # Indicator 1: plain "is there a threat right now" -- deliberately
        # NOT derived from `color`/risk level above (see ThreatSnapshot.
        # has_active_threat's docstring for why these can legitimately
        # disagree, e.g. a still-decaying risk score with no active event).
        threat_color = (
            theme.THREAT_STATUS_ACTIVE_COLOR
            if snapshot.has_active_threat
            else theme.THREAT_STATUS_CLEAR_COLOR
        )
        self._threat_status_text.value = "Активна загроза" if snapshot.has_active_threat else "Чисто"
        self._threat_status_text.color = threat_color
        self._threat_square.bgcolor = threat_color
        self._threat_square.shadow = ft.BoxShadow(
            spread_radius=1,
            blur_radius=22,
            color=ft.Colors.with_opacity(0.55, threat_color),
            offset=ft.Offset(0, 0),
        )

        if self.page is not None:
            self.update()
