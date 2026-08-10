"""'Налаштування' (Settings) tab content.

Also owns the "Сповіщення за областями" (region-alert) checkbox picker --
edits the same AppSettings.watched_regions list as the "Рух загроз" tab's
oblast chips (see app/ui/views/movement_view.py); both write through the
same main.py persistence handler (_persist_selected_regions), so there is
exactly one place that saves this list no matter which screen the user
used to change it, and each screen's control reflects a change made on
the other via the existing set_selected_regions()/set_watched_regions()
push-back calls.
"""

from __future__ import annotations

import asyncio
from typing import Callable, Optional

import flet as ft

from app.config import DEFAULTS
from app.models.alert_models import ApiStatus, Region
from app.models.settings_models import AppSettings
from app.ui.theme import colors as theme

OnSave = Callable[[AppSettings], None]
OnReset = Callable[[], None]
OnLoginTelegram = Callable[[], None]
OnLogoutTelegram = Callable[[], None]
#: Fired (debounced) after an edit to a field that auto-saves without
#: requiring the "Зберегти" button -- currently API ID/API Hash only,
#: per the requirement that Telegram credentials persist immediately.
OnAutoSave = Callable[[AppSettings], None]
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
        on_login_telegram: OnLoginTelegram,
        on_logout_telegram: Optional[OnLogoutTelegram] = None,
        on_watched_regions_changed: Optional[OnWatchedRegionsChanged] = None,
        on_auto_save: Optional[OnAutoSave] = None,
        channels_view: Optional[ft.Control] = None,
        log_view: Optional[ft.Control] = None,
    ) -> None:
        """Build the form; call :meth:`set_settings` to populate its values.

        ``channels_view``/``log_view`` are the existing "Канали"/"Лог"
        controls, embedded here (not rebuilt) now that this is the single
        ⚙ settings screen rather than a top-level tab bar -- per the
        interface change, everything configuration-related lives here.

        ``on_auto_save`` (optional, backward compatible -- every existing
        caller that doesn't pass it keeps the old "Save button only"
        behavior for every field) is called, debounced, after an edit to
        API ID or API Hash specifically -- see ``_schedule_auto_save``.
        """
        self._on_save = on_save
        self._on_reset = on_reset
        self._on_login_telegram = on_login_telegram
        self._on_logout_telegram = on_logout_telegram
        self._on_watched_regions_changed = on_watched_regions_changed
        self._on_auto_save = on_auto_save
        #: In-flight debounce task for the credentials auto-save, so a
        #: fast run of keystrokes only ever results in ONE actual save
        #: (after typing settles), not one disk write per character --
        #: same debounce pattern already used for page-resize handling in
        #: main.py.
        self._auto_save_task: Optional["asyncio.Task"] = None

        self._api_id_field = self._text_field(
            "API ID", keyboard_type=ft.KeyboardType.NUMBER, on_change=self._handle_credential_changed
        )
        self._api_hash_field = self._text_field(
            "API HASH", password=True, can_reveal_password=True, on_change=self._handle_credential_changed
        )
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
                    self._source_status_text,
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
        self._api_id_field.value = settings.api_id
        self._api_hash_field.value = settings.api_hash
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
        """Reflect which data source is actually currently in effect.

        Mirrors the exact same precedence already described in this
        section's explanatory text (and already implemented in
        ``AlertService``): the official alerts.in.ua API when its last
        request succeeded, falling back to Telegram-channel parsing
        otherwise (no token configured, or the last request failed) --
        this never introduces a second, separate notion of "current
        source", it just surfaces the one ``AlertService`` already
        tracks as ``ThreatSnapshot.api_status``.
        """
        if api_status == ApiStatus.OK:
            self._source_status_text.value = "Поточне джерело тривог: alerts.in.ua API"
            self._source_status_text.color = "#22C55E"
        else:
            self._source_status_text.value = "Поточне джерело тривог: Telegram-канали"
            self._source_status_text.color = theme.ACCENT_BLUE
        if self.page is not None:
            self._source_status_text.update()

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
        """Read every field's current value into a fresh :class:`AppSettings`.

        Shared by the explicit "Зберегти" button and the debounced
        credentials auto-save, so both ways of saving build the exact
        same object from the exact same fields -- no separate, possibly
        drifting copy of this logic.
        """
        try:
            interval = int(self._interval_field.value or DEFAULTS.UPDATE_INTERVAL_SECONDS)
        except ValueError:
            interval = DEFAULTS.UPDATE_INTERVAL_SECONDS
        interval = max(
            DEFAULTS.MIN_UPDATE_INTERVAL_SECONDS,
            min(DEFAULTS.MAX_UPDATE_INTERVAL_SECONDS, interval),
        )
        return AppSettings(
            api_id=(self._api_id_field.value or "").strip(),
            api_hash=(self._api_hash_field.value or "").strip(),
            alerts_api_token=(self._alerts_token_field.value or "").strip(),
            update_interval_seconds=interval,
            auto_start_monitoring=bool(self._auto_start_switch.value),
            notifications_enabled=bool(self._notifications_switch.value),
            watched_regions=list(self._current_watched_regions),
        )

    def _handle_save(self, _: ft.ControlEvent) -> None:
        """Validate the form and forward a new :class:`AppSettings` to save."""
        self._on_save(self._build_settings_from_form())

    def _handle_credential_changed(self, _: ft.ControlEvent) -> None:
        """API ID/API Hash changed -- (re)schedule the debounced auto-save.

        Cancels any still-pending auto-save from a previous keystroke
        first, so a fast run of edits results in exactly one actual save
        (after typing settles), not one disk write per character.
        """
        if self._on_auto_save is None or self.page is None:
            return
        if self._auto_save_task is not None and not self._auto_save_task.done():
            self._auto_save_task.cancel()
        self._auto_save_task = self.page.run_task(self._debounced_auto_save)

    async def _debounced_auto_save(self) -> None:
        """Wait for typing to settle, then fire the auto-save callback.

        0.7s comfortably covers the gap between individual keystrokes
        (so it never fires mid-word) while still feeling immediate once
        the person actually pauses/moves on -- no "press Save" step
        required, per the requirement.
        """
        await asyncio.sleep(0.7)
        if self._on_auto_save is not None:
            self._on_auto_save(self._build_settings_from_form())
