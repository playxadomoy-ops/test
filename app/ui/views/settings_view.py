"""'Налаштування' (Settings) tab content.

Also owns the "Сповіщення за областями" (region-alert) checkbox picker --
edits the same AppSettings.watched_regions list as the "Рух загроз" tab's
oblast chips (see app/ui/views/movement_view.py); both write through the
same main.py persistence handler (_persist_selected_regions), so there is
exactly one place that saves this list no matter which screen the user
used to change it, and each screen's control reflects a change made on
the other via the existing set_selected_regions()/set_watched_regions()
push-back calls.

Note on the "Сервер" section: there is no address/Device ID/Token input
and no "Підключити" button here anymore. The server connection is fully
automatic (LAN discovery + self-registration + admin approval, see
app/services/server_client.py) -- this section only ever *displays* the
status the app arrived at on its own. An optional, collapsed
"Додатково" panel exposes read-only technical details (discovered
server address, this device's ID) for anyone curious or for support
purposes, without cluttering the normal view.
"""

from __future__ import annotations

from typing import Callable, Optional

import flet as ft

from app.config import DEFAULTS
from app.models.alert_models import ApiStatus, Region
from app.models.settings_models import AppSettings
from app.ui.theme import colors as theme

OnSave = Callable[[AppSettings], None]
OnReset = Callable[[], None]
#: Same shape as movement_view.py's OnSelectedRegionsChanged -- both
#: views edit the same underlying AppSettings.watched_regions list, and
#: both changes flow through the same main.py persistence handler (see
#: module docstring below).
OnWatchedRegionsChanged = Callable[[set[Region]], None]

_SERVER_STATUS_STYLE: dict[str, tuple[str, str]] = {
    "discovering": ("\U0001F7E1", theme.TEXT_SECONDARY),
    "waiting_approval": ("\U0001F7E0", "#F59E0B"),
    "connected": ("\U0001F7E2", "#22C55E"),
    "disconnected": ("\U0001F534", "#EF4444"),
    "rejected": ("\U0001F534", "#EF4444"),
}


class SettingsView(ft.Column):
    """Editable form for all persisted application settings."""

    def __init__(
        self,
        on_save: OnSave,
        on_reset: OnReset,
        on_watched_regions_changed: Optional[OnWatchedRegionsChanged] = None,
    ) -> None:
        """Build the form; call :meth:`set_settings` to populate its values."""
        self._on_save = on_save
        self._on_reset = on_reset
        self._on_watched_regions_changed = on_watched_regions_changed

        self._alerts_token_field = self._text_field(
            "Токен alerts.in.ua (необов'язково)", password=True, can_reveal_password=True
        )
        self._interval_field = self._text_field(
            "Інтервал оновлення (сек)", keyboard_type=ft.KeyboardType.NUMBER
        )
        self._auto_start_switch = ft.Switch(active_color=theme.ACCENT_BLUE)
        self._notifications_switch = ft.Switch(active_color=theme.ACCENT_BLUE)

        # Oblast selection is edited here AND on the "Рух загроз" chips
        # (see module docstring) -- both write through the same
        # AppSettings.watched_regions list via the same main.py handler.
        self._current_watched_regions: list[str] = []
        self._region_checkboxes: dict[Region, ft.Checkbox] = {
            region: ft.Checkbox(
                label=region.value,
                label_style=ft.TextStyle(size=12, color=theme.TEXT_PRIMARY),
                active_color=theme.ACCENT_PURPLE,
                on_change=self._make_checkbox_toggle_handler(region),
            )
            for region in Region
        }

        self._source_status_text = ft.Text(
            "Поточне джерело тривог: визначається...",
            size=12,
            weight=ft.FontWeight.W_600,
            color=theme.TEXT_SECONDARY,
        )

        # --- Air Alert Analyzer SERVER (centralized backend) -- fully
        # automatic: this card is read-only, reflecting whatever state
        # ServerClient arrived at on its own (see its module docstring).
        self._server_icon_text = ft.Text("\U0001F7E1", size=16)
        self._server_status_text = ft.Text(
            "Пошук сервера...", size=13, weight=ft.FontWeight.W_600, color=theme.TEXT_SECONDARY
        )
        self._server_details_ip_text = ft.Text("—", size=11, color=theme.TEXT_MUTED, selectable=True)
        self._server_details_device_text = ft.Text("—", size=11, color=theme.TEXT_MUTED, selectable=True)
        advanced_panel = ft.ExpansionTile(
            title=ft.Text("Додатково", size=12, color=theme.TEXT_MUTED),
            initially_expanded=False,
            controls_padding=ft.padding.only(left=4, right=4, bottom=8),
            controls=[
                ft.Row([ft.Text("Сервер:", size=11, color=theme.TEXT_MUTED, width=90),
                         self._server_details_ip_text]),
                ft.Row([ft.Text("Пристрій:", size=11, color=theme.TEXT_MUTED, width=90),
                         self._server_details_device_text]),
            ],
        )

        sections: list[ft.Control] = [
            self._section(
                "Сервер",
                [
                    ft.Row(
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[self._server_icon_text, self._server_status_text],
                    ),
                    ft.Text(
                        "Підключення до сервера Air Alert Analyzer відбувається автоматично -- "
                        "нічого налаштовувати не потрібно.",
                        size=11,
                        color=theme.TEXT_MUTED,
                    ),
                    advanced_panel,
                ],
            ),
        ]
        sections.append(
            self._section(
                "Джерела даних",
                [
                    self._source_status_text,
                    ft.Text(
                        "Статус тривоги по областях оновлюється з офіційного alerts.in.ua "
                        "(потрібен токен нижче) та з подій, отриманих від сервера Air Alert "
                        "Analyzer вище.",
                        size=11,
                        color=theme.TEXT_MUTED,
                    ),
                    self._alerts_token_field,
                    self._interval_field,
                ],
            )
        )
        sections.append(
            self._section(
                "Поведінка",
                [
                    self._switch_row("Автозапуск моніторингу", self._auto_start_switch),
                    self._switch_row("Сповіщення", self._notifications_switch),
                ],
            )
        )
        sections.append(
            self._section(
                "Сповіщення за областями",
                [
                    ft.Text(
                        "Оберіть області: коли будь-яка з них переходить із стану "
                        "«Немає тривоги» в «Повітряна тривога» за офіційними даними "
                        "alerts.in.ua, застосунок один раз відтворить звук (якщо "
                        "«Сповіщення» вище увімкнено). Той самий вибір також звужує "
                        "карту на вкладці «Рух загроз».",
                        size=11,
                        color=theme.TEXT_MUTED,
                    ),
                    ft.Row(
                        spacing=8,
                        controls=[
                            ft.OutlinedButton(text="Обрати всі", on_click=self._handle_select_all),
                            ft.OutlinedButton(text="Зняти всі", on_click=self._handle_clear_all),
                        ],
                    ),
                    ft.Container(
                        height=260,
                        border_radius=12,
                        bgcolor=theme.SURFACE,
                        padding=8,
                        content=ft.Column(
                            scroll=ft.ScrollMode.AUTO,
                            spacing=2,
                            controls=list(self._region_checkboxes.values()),
                        ),
                    ),
                ],
            )
        )
        sections.append(
            ft.Row(
                wrap=True,
                spacing=8,
                run_spacing=8,
                controls=[
                    ft.ElevatedButton(
                        text="Зберегти",
                        icon=ft.Icons.SAVE_ROUNDED,
                        bgcolor=theme.ACCENT_BLUE,
                        color=ft.Colors.WHITE,
                        on_click=self._handle_save,
                    ),
                    ft.OutlinedButton(
                        text="Скинути",
                        icon=ft.Icons.RESTORE_ROUNDED,
                        on_click=lambda e: self._on_reset(),
                    ),
                ]
            )
        )

        super().__init__(
            spacing=16,
            expand=True,
            scroll=ft.ScrollMode.AUTO,
            controls=sections,
        )

    def _section(self, title: str, controls: list[ft.Control]) -> ft.Container:
        """Build one labeled settings section card."""
        return ft.Container(
            padding=16,
            border_radius=theme.RADIUS_LG,
            bgcolor=theme.SURFACE_ELEVATED,
            border=ft.border.all(1, theme.BORDER),
            shadow=theme.elevation_shadow(),
            content=ft.Column(
                spacing=12,
                controls=[
                    ft.Text(title, size=14, weight=ft.FontWeight.W_600, color=theme.TEXT_SECONDARY),
                    *controls,
                ],
            ),
        )

    @staticmethod
    def _text_field(label: str, **kwargs) -> ft.TextField:
        """Build a themed text field for the form."""
        return ft.TextField(
            label=label,
            border_color=theme.BORDER,
            bgcolor=theme.SURFACE,
            color=theme.TEXT_PRIMARY,
            label_style=ft.TextStyle(color=theme.TEXT_SECONDARY),
            border_radius=12,
            **kwargs,
        )

    @staticmethod
    def _switch_row(label: str, switch: ft.Switch) -> ft.Row:
        """Build a label + switch row that stays responsive on narrow screens."""
        return ft.Row(
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Text(label, size=13, color=theme.TEXT_PRIMARY, expand=True),
                switch,
            ],
        )

    def set_settings(self, settings: AppSettings) -> None:
        """Populate the form fields from a loaded :class:`AppSettings`."""
        self._alerts_token_field.value = settings.alerts_api_token
        self._interval_field.value = str(settings.update_interval_seconds)
        self._auto_start_switch.value = settings.auto_start_monitoring
        self._notifications_switch.value = settings.notifications_enabled
        self._apply_watched_regions(list(settings.watched_regions))
        if self.page is not None:
            self.update()

    def set_watched_regions(self, watched_regions: list[str]) -> None:
        """Record an oblast selection made elsewhere (the Рух загроз chips).

        Keeps this form's own Save button, and the region checkboxes
        here, from showing/persisting a stale value from the last full
        :meth:`set_settings` call after the other screen changes it.
        """
        self._apply_watched_regions(list(watched_regions))

    def _apply_watched_regions(self, watched_regions: list[str]) -> None:
        """Shared by set_settings/set_watched_regions: store + reflect in checkboxes."""
        self._current_watched_regions = list(watched_regions)
        selected = set(watched_regions)
        for region, checkbox in self._region_checkboxes.items():
            checkbox.value = region.value in selected
            if self.page is not None:
                checkbox.update()

    def set_source_status(self, api_status: ApiStatus) -> None:
        """Reflect whether the official alerts.in.ua API is currently in effect."""
        if api_status == ApiStatus.OK:
            self._source_status_text.value = "Поточне джерело тривог: alerts.in.ua API"
            self._source_status_text.color = "#22C55E"
        else:
            self._source_status_text.value = "Поточне джерело тривог: недоступне"
            self._source_status_text.color = theme.TEXT_MUTED
        if self.page is not None:
            self._source_status_text.update()

    def set_server_status(self, status_code: str, message: str) -> None:
        """Reflect the current automatic server-connection state.

        ``status_code`` is one of "discovering", "waiting_approval",
        "connected", "disconnected", "rejected" -- see
        app/services/server_client.py's OnStatusChanged. Purely
        read-only display; there is nothing here for the user to act on.
        """
        emoji, color = _SERVER_STATUS_STYLE.get(status_code, _SERVER_STATUS_STYLE["disconnected"])
        self._server_icon_text.value = emoji
        self._server_status_text.value = message
        self._server_status_text.color = color
        if self.page is not None:
            self._server_icon_text.update()
            self._server_status_text.update()

    def set_server_details(self, server_address: Optional[str], device_id: Optional[str]) -> None:
        """Populate the collapsed "Додатково" panel -- optional technical info only."""
        self._server_details_ip_text.value = server_address or "—"
        self._server_details_device_text.value = device_id or "—"
        if self.page is not None:
            self._server_details_ip_text.update()
            self._server_details_device_text.update()

    def _make_checkbox_toggle_handler(self, region: Region) -> Callable[[ft.ControlEvent], None]:
        """Build a change handler that flips one region's checkbox and re-applies the selection."""

        def handler(_: ft.ControlEvent) -> None:
            checkbox = self._region_checkboxes[region]
            current = set(self._current_watched_regions)
            if checkbox.value:
                current.add(region.value)
            else:
                current.discard(region.value)
            self._current_watched_regions = sorted(current)
            self._notify_watched_regions_changed()

        return handler

    def _handle_select_all(self, _: ft.ControlEvent) -> None:
        """Check every region -- 'Обрати всі'."""
        self._apply_watched_regions([region.value for region in Region])
        self._notify_watched_regions_changed()

    def _handle_clear_all(self, _: ft.ControlEvent) -> None:
        """Uncheck every region -- 'Зняти всі'."""
        self._apply_watched_regions([])
        self._notify_watched_regions_changed()

    def _notify_watched_regions_changed(self) -> None:
        """Forward the current selection to main.py's shared persistence handler."""
        if self._on_watched_regions_changed is not None:
            self._on_watched_regions_changed(
                {Region(value) for value in self._current_watched_regions}
            )

    def _build_settings_from_form(self) -> AppSettings:
        """Read every field's current value into a fresh :class:`AppSettings`."""
        try:
            interval = int(self._interval_field.value or DEFAULTS.UPDATE_INTERVAL_SECONDS)
        except ValueError:
            interval = DEFAULTS.UPDATE_INTERVAL_SECONDS
        interval = max(
            DEFAULTS.MIN_UPDATE_INTERVAL_SECONDS,
            min(DEFAULTS.MAX_UPDATE_INTERVAL_SECONDS, interval),
        )
        return AppSettings(
            alerts_api_token=(self._alerts_token_field.value or "").strip(),
            update_interval_seconds=interval,
            auto_start_monitoring=bool(self._auto_start_switch.value),
            notifications_enabled=bool(self._notifications_switch.value),
            watched_regions=list(self._current_watched_regions),
        )

    def _handle_save(self, _: ft.ControlEvent) -> None:
        """Validate the form and forward a new :class:`AppSettings` to save."""
        self._on_save(self._build_settings_from_form())
