"""'Налаштування' (Settings) tab content.

Also owns the "Сповіщення за областями" (region-alert) checkbox picker --
edits the same AppSettings.watched_regions list as the "Рух загроз" tab's
oblast chips (see app/ui/views/movement_view.py); both write through the
same main.py persistence handler (_persist_selected_regions), so there is
exactly one place that saves this list no matter which screen the user
used to change it, and each screen's control reflects a change made on
the other via the existing set_selected_regions()/set_watched_regions()
push-back calls.

Note: this view no longer has a "Telegram" section (API ID/API HASH,
login/session controls) or embedded "Канали"/"Лог" panels. The mobile
app no longer connects to Telegram directly or shows server/debug logs
-- see app/services/server_client.py and the project's server
application for where that now lives.
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
OnConnectServer = Callable[[], None]
OnDisconnectServer = Callable[[], None]
#: Same shape as movement_view.py's OnSelectedRegionsChanged -- both
#: views edit the same underlying AppSettings.watched_regions list, and
#: both changes flow through the same main.py persistence handler (see
#: module docstring below).
OnWatchedRegionsChanged = Callable[[set[Region]], None]


class SettingsView(ft.Column):
    """Editable form for all persisted application settings."""

    def __init__(
        self,
        on_save: OnSave,
        on_reset: OnReset,
        on_watched_regions_changed: Optional[OnWatchedRegionsChanged] = None,
        on_connect_server: Optional[OnConnectServer] = None,
        on_disconnect_server: Optional[OnDisconnectServer] = None,
    ) -> None:
        """Build the form; call :meth:`set_settings` to populate its values."""
        self._on_save = on_save
        self._on_reset = on_reset
        self._on_watched_regions_changed = on_watched_regions_changed
        self._on_connect_server = on_connect_server
        self._on_disconnect_server = on_disconnect_server
        #: Tracks whether the server connection should be considered
        #: "enabled" for persistence purposes (i.e. auto-reconnect on next
        #: app start) -- set True on "Підключити", False on "Відключити".
        #: Initialized from whatever was last persisted in set_settings().
        self._server_enabled_hint: bool = False

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

        # --- Air Alert Analyzer SERVER (centralized backend). This
        # device only ever sends its own device_id/token here, and the
        # server never returns Telegram API id/hash/phone/session in
        # any response this app parses (see app/services/server_client.py).
        self._server_url_field = self._text_field(
            "Адреса сервера (ip:порт)", hint_text="напр. 192.168.1.10:8765"
        )
        self._server_device_id_field = self._text_field("Device ID")
        self._server_token_field = self._text_field(
            "Token", password=True, can_reveal_password=True
        )
        self._server_status_text = ft.Text(
            "Сервер: не підключено", size=12, color=theme.TEXT_SECONDARY
        )

        sections: list[ft.Control] = [
            self._section(
                "Сервер (централізований бекенд)",
                [
                    ft.Text(
                        "Підключення до вашого власного сервера Air Alert Analyzer. Дані "
                        "для входу (Device ID / Token) видає адміністратор сервера в його "
                        "розділі «Користувачі».",
                        size=11,
                        color=theme.TEXT_MUTED,
                    ),
                    self._server_url_field,
                    self._server_device_id_field,
                    self._server_token_field,
                    ft.Row(
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Container(content=self._server_status_text, expand=True),
                            ft.Row(
                                spacing=8,
                                controls=[
                                    ft.OutlinedButton(
                                        text="Відключити",
                                        icon=ft.Icons.LINK_OFF_ROUNDED,
                                        on_click=self._handle_disconnect_server_click,
                                    ),
                                    ft.ElevatedButton(
                                        text="Підключити",
                                        icon=ft.Icons.LINK_ROUNDED,
                                        on_click=self._handle_connect_server_click,
                                    ),
                                ],
                            ),
                        ],
                    ),
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
                        "(потрібен токен нижче) та з подій, отриманих від вашого сервера "
                        "Air Alert Analyzer вище.",
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
        self._server_url_field.value = settings.server_url
        self._server_device_id_field.value = settings.server_device_id
        self._server_token_field.value = settings.server_token
        self._server_enabled_hint = settings.server_enabled
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

    def set_server_status(self, connected: bool, message: Optional[str] = None) -> None:
        """Update the small server connection status line."""
        if message:
            self._server_status_text.value = f"Сервер: {message}"
        else:
            self._server_status_text.value = "Сервер: підключено" if connected else "Сервер: не підключено"
        self._server_status_text.color = "#22C55E" if connected else theme.TEXT_SECONDARY
        if self.page is not None:
            self._server_status_text.update()

    def _handle_connect_server_click(self, _: ft.ControlEvent) -> None:
        """Persist current server fields (via the normal save path) then connect."""
        self._server_enabled_hint = True
        self._on_save(self._build_settings_from_form())
        if self._on_connect_server is not None:
            self._on_connect_server()

    def _handle_disconnect_server_click(self, _: ft.ControlEvent) -> None:
        """Persist the disabled flag then disconnect."""
        self._server_enabled_hint = False
        self._on_save(self._build_settings_from_form())
        if self._on_disconnect_server is not None:
            self._on_disconnect_server()

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
            server_url=(self._server_url_field.value or "").strip(),
            server_device_id=(self._server_device_id_field.value or "").strip(),
            server_token=(self._server_token_field.value or "").strip(),
            server_enabled=self._server_enabled_hint,
        )

    def _handle_save(self, _: ft.ControlEvent) -> None:
        """Validate the form and forward a new :class:`AppSettings` to save."""
        self._on_save(self._build_settings_from_form())
