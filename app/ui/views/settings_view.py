"""'Налаштування' (Settings) tab content."""

from __future__ import annotations

from typing import Callable, Optional

import flet as ft

from app.config import DEFAULTS
from app.models.settings_models import AppSettings
from app.ui.theme import colors as theme

OnSave = Callable[[AppSettings], None]
OnReset = Callable[[], None]
OnLoginTelegram = Callable[[], None]
OnLogoutTelegram = Callable[[], None]


class SettingsView(ft.Column):
    """Editable form for all persisted application settings."""

    def __init__(
        self,
        on_save: OnSave,
        on_reset: OnReset,
        on_login_telegram: OnLoginTelegram,
        on_logout_telegram: Optional[OnLogoutTelegram] = None,
        channels_view: Optional[ft.Control] = None,
        log_view: Optional[ft.Control] = None,
    ) -> None:
        """Build the form; call :meth:`set_settings` to populate its values.

        ``channels_view``/``log_view`` are the existing "Канали"/"Лог"
        controls, embedded here (not rebuilt) now that this is the single
        ⚙ settings screen rather than a top-level tab bar -- per the
        interface change, everything configuration-related lives here.
        """
        self._on_save = on_save
        self._on_reset = on_reset
        self._on_login_telegram = on_login_telegram
        self._on_logout_telegram = on_logout_telegram

        self._api_id_field = self._text_field("API ID", keyboard_type=ft.KeyboardType.NUMBER)
        self._api_hash_field = self._text_field("API HASH", password=True, can_reveal_password=True)
        self._alerts_token_field = self._text_field(
            "Токен alerts.in.ua (необов'язково)", password=True, can_reveal_password=True
        )
        self._interval_field = self._text_field(
            "Інтервал оновлення (сек)", keyboard_type=ft.KeyboardType.NUMBER
        )
        self._auto_start_switch = ft.Switch(active_color=theme.ACCENT_BLUE)
        self._notifications_switch = ft.Switch(active_color=theme.ACCENT_BLUE)
        self._telegram_status_text = ft.Text(
            "Telegram: не підключено", size=12, color=theme.TEXT_SECONDARY
        )
        self._session_status_text = ft.Text(
            "Сесія: немає збереженої сесії", size=12, color=theme.TEXT_SECONDARY
        )
        # Oblast selection now lives entirely in MovementView (see the
        # "Threat Movement" screen's "Обрані області" chips) -- Settings
        # no longer edits it, but must still carry the current value
        # forward unchanged whenever this form's own Save button is used,
        # so saving e.g. a new API ID doesn't silently wipe it back to [].
        self._current_watched_regions: list[str] = []

        self._channels_section = (
            self._section(
                "Канали",
                [ft.Container(height=340, content=channels_view)],
            )
            if channels_view is not None
            else None
        )
        self._log_section = (
            self._section(
                "Журнал подій",
                [ft.Container(height=340, content=log_view)],
            )
            if log_view is not None
            else None
        )

        sections: list[ft.Control] = [
            self._section(
                "Telegram",
                [
                    self._api_id_field,
                    self._api_hash_field,
                    ft.Row(
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Container(content=self._telegram_status_text, expand=True),
                            ft.ElevatedButton(
                                text="Увійти в Telegram",
                                icon=ft.Icons.SEND_ROUNDED,
                                on_click=lambda e: self._on_login_telegram(),
                            ),
                        ],
                    ),
                    ft.Row(
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Container(content=self._session_status_text, expand=True),
                            ft.OutlinedButton(
                                text="Скинути сесію",
                                icon=ft.Icons.LOGOUT_ROUNDED,
                                on_click=self._handle_logout_click,
                            ),
                        ],
                    ),
                ],
            ),
        ]
        if self._channels_section is not None:
            sections.append(self._channels_section)
        sections.append(
            self._section(
                "Джерела даних",
                [
                    ft.Text(
                        "Статус тривоги по областях оновлюється з двох джерел: "
                        "(1) офіційний alerts.in.ua, якщо нижче вказано токен і запит вдалий -- "
                        "має пріоритет, і (2) безкоштовно, без жодного токена -- автоматичний "
                        "розбір повідомлень з ваших каналів (вкладка «Канали»), якщо канал прямо "
                        "називає область (наприклад «Дніпропетровська область: тривога» чи "
                        "«Харківщина: відбій»). Друге джерело вмикається автоматично, поки перше "
                        "не налаштоване або недоступне -- токен нижче необов'язковий.",
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
        if self._log_section is not None:
            sections.append(self._log_section)
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
            border_radius=20,
            bgcolor=theme.SURFACE_ELEVATED,
            border=ft.border.all(1, theme.BORDER),
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
        self._api_id_field.value = settings.api_id
        self._api_hash_field.value = settings.api_hash
        self._alerts_token_field.value = settings.alerts_api_token
        self._interval_field.value = str(settings.update_interval_seconds)
        self._auto_start_switch.value = settings.auto_start_monitoring
        self._notifications_switch.value = settings.notifications_enabled
        self._current_watched_regions = list(settings.watched_regions)
        if self.page is not None:
            self.update()

    def set_watched_regions(self, watched_regions: list[str]) -> None:
        """Record the current oblast selection (owned by MovementView now).

        Does not touch any other field -- only keeps :meth:`_handle_save`
        from overwriting a selection made on the Threat Movement screen
        with a stale value from the last full :meth:`set_settings` call.
        """
        self._current_watched_regions = list(watched_regions)

    def set_telegram_status(self, connected: bool) -> None:
        """Update the small Telegram connection status line."""
        self._telegram_status_text.value = (
            "Telegram: підключено" if connected else "Telegram: не підключено"
        )
        self._telegram_status_text.color = (
            "#22C55E" if connected else theme.TEXT_SECONDARY
        )
        if self.page is not None:
            self._telegram_status_text.update()

    def set_session_status(self, has_session: bool) -> None:
        """Update the small 'is a Telegram session saved locally' line."""
        self._session_status_text.value = (
            "Сесія: збережена локально" if has_session else "Сесія: немає збереженої сесії"
        )
        self._session_status_text.color = "#22C55E" if has_session else theme.TEXT_SECONDARY
        if self.page is not None:
            self._session_status_text.update()

    def _handle_logout_click(self, _: ft.ControlEvent) -> None:
        """Forward a request to disconnect Telegram and clear the saved session."""
        if self._on_logout_telegram is not None:
            self._on_logout_telegram()

    def _handle_save(self, _: ft.ControlEvent) -> None:
        """Validate the form and forward a new :class:`AppSettings` to save."""
        try:
            interval = int(self._interval_field.value or DEFAULTS.UPDATE_INTERVAL_SECONDS)
        except ValueError:
            interval = DEFAULTS.UPDATE_INTERVAL_SECONDS
        interval = max(
            DEFAULTS.MIN_UPDATE_INTERVAL_SECONDS,
            min(DEFAULTS.MAX_UPDATE_INTERVAL_SECONDS, interval),
        )

        settings = AppSettings(
            api_id=(self._api_id_field.value or "").strip(),
            api_hash=(self._api_hash_field.value or "").strip(),
            alerts_api_token=(self._alerts_token_field.value or "").strip(),
            update_interval_seconds=interval,
            auto_start_monitoring=bool(self._auto_start_switch.value),
            notifications_enabled=bool(self._notifications_switch.value),
            watched_regions=list(self._current_watched_regions),
        )
        self._on_save(settings)
