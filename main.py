"""
Application entry point.

Startup contract (see project requirements about avoiding a white screen):
  1. ``main()`` builds and adds the *entire* UI to the page synchronously,
     using safe default/empty values everywhere.
  2. Only afterwards does it kick off an async initialization task
     (``page.run_task``) that loads persisted data and starts background
     services. Any failure there is caught, logged, and surfaced as a
     banner — it never leaves the user looking at a blank page.

Layout contract: this is an Android-first, single-column app (no desktop
side-by-side split). A small header (сповіщення toggle + ⚙ settings
shortcut) sits above the main content area, which is two swipeable
screens -- Огляд (threat status + alert map + recent messages) and Рух
загроз (threat movement map) -- switched by a left/right drag or the dot
indicator, no top tab bar. Tapping ⚙ replaces that area with a single
full-screen Налаштування view (Telegram credentials/session, alert
source token, update interval, behavior switches, watched regions, and
the Канали/Лог sections, all in one place) until tapped again.
"""

from __future__ import annotations

print("main.py: module import STARTED (first line executed).")

import asyncio
import traceback
from datetime import datetime, timedelta
from typing import Optional

import flet as ft

print("main.py: flet imported OK.")

from app.models.alert_models import ApiStatus, Region, RegionState, ThreatSnapshot
from app.models.channel_models import ChannelMessage, TelegramChannel
from app.models.log_models import LogEntry
from app.models.movement_models import ThreatMovement
from app.models.settings_models import AppSettings
from app.models.vocabulary_models import VocabularyCache
from app.services.alert_service import AlertService
from app.services.logger_service import LoggerService
from app.services.movement_parser import parse_message
from app.services.region_alert_parser import parse_region_alerts
from app.services.risk_analyzer import RiskAnalyzer
from app.services.vocabulary_builder import VocabularyBuilder
from app.storage.local_storage import LocalStorage

print("main.py: app.models / app.services / app.storage imported OK.")

from app.telegram.telegram_service import TelegramAuthCallbacks, TelegramService

print("main.py: app.telegram.telegram_service (Telethon) imported OK.")

from app.ui.theme import colors as theme
from app.ui.theme.colors import build_page_theme
from app.ui.views.channels_view import ChannelsView
from app.ui.views.log_view import LogView
from app.ui.views.movement_view import MovementView
from app.ui.views.overview_view import OverviewView
from app.ui.views.settings_view import SettingsView

print("main.py: app.ui.* imported OK. module import COMPLETE.")


class _MutableInterval:
    """Tiny box so the background tick loop can see interval updates live."""

    seconds: int = 30


#: How long a "Рух загроз" entry stays on the map/list after its message
#: arrived. These are transient, time-sensitive alerts (a drone/missile in
#: flight), not a permanent log -- 2 hours comfortably covers the typical
#: flight time of a Shahed-type drone across Ukraine while still clearing
#: out stale entries automatically.
_MOVEMENT_TTL_SECONDS = 2 * 60 * 60

#: Vocabulary Builder timing (see app/services/vocabulary_builder.py).
#: Waits this long after startup before its first run (never competes
#: with initial connection/UI setup), then repeats on this interval --
#: each run is incremental, so a 6-hour cadence is cheap regardless of
#: total history size.
_VOCABULARY_INITIAL_DELAY_SECONDS = 3 * 60
_VOCABULARY_UPDATE_INTERVAL_SECONDS = 6 * 60 * 60


async def main(page: ft.Page) -> None:
    """Flet entry point: build the UI, then asynchronously bring it to life."""
    print("main.py: main() ENTERED -- building the UI synchronously now.")
    try:
        _configure_page(page)
        print("main.py: _configure_page() OK.")

        # --- Dependency graph (constructor injection, no globals) ----------
        storage = LocalStorage(page)
        logger = LoggerService(storage)
        risk_analyzer = RiskAnalyzer()
        alert_service = AlertService(logger, risk_analyzer)
        telegram_service = TelegramService(logger)
        vocabulary_builder = VocabularyBuilder(telegram_service, logger)
        print("main.py: service layer constructed OK (storage/logger/risk_analyzer/alert_service/telegram_service/vocabulary_builder).")

        interval_box = _MutableInterval()
        channels: list[TelegramChannel] = []
        movements: list[ThreatMovement] = []
        settings_holder: list[AppSettings] = [AppSettings.default()]

        # --- UI shell, built and shown immediately --------------------------

        def handle_region_tap(region: Region) -> None:
            _show_region_info_dialog(page, region, alert_service.region_states.get(region))

        def handle_open_channels() -> None:
            _show_settings()

        overview_view = OverviewView(
            on_region_tap=handle_region_tap,
            on_open_channels=handle_open_channels,
        )
        print("main.py: OverviewView() constructed OK (ThreatCard/ChannelsQuickRow/UkraineMap/StatsPanel).")

        def handle_movement_tap(movement: ThreatMovement) -> None:
            _show_movement_dialog(page, movement)

        def handle_selected_regions_changed(regions: set[Region]) -> None:
            page.run_task(_persist_selected_regions, regions)

        movement_view = MovementView(
            on_movement_tap=handle_movement_tap,
            on_region_tap=handle_region_tap,  # same info dialog as the Огляд map, per spec
            on_selected_regions_changed=handle_selected_regions_changed,
        )
        print("main.py: MovementView() constructed OK.")

        def handle_add_channel(username: str) -> None:
            page.run_task(_add_channel, username)

        def handle_toggle_channel(username: str, enabled: bool) -> None:
            page.run_task(_toggle_channel, username, enabled)

        def handle_remove_channel(username: str) -> None:
            page.run_task(_remove_channel, username)

        channels_view = ChannelsView(
            on_add=handle_add_channel,
            on_toggle=handle_toggle_channel,
            on_remove=handle_remove_channel,
        )
        print("main.py: ChannelsView() constructed OK.")

        def handle_clear_log() -> None:
            page.run_task(_clear_log)

        log_view = LogView(on_clear=handle_clear_log)
        print("main.py: LogView() constructed OK.")

        def handle_save_settings(settings: AppSettings) -> None:
            page.run_task(_save_settings, settings)

        def handle_reset_settings() -> None:
            page.run_task(_reset_settings)

        def handle_login_telegram() -> None:
            page.run_task(_login_telegram)

        def handle_logout_telegram() -> None:
            page.run_task(_logout_telegram)

        # NOTE on the interface change: per the project's requirement to
        # remove the top tab bar, "Канали" and "Лог" no longer get their
        # own top-level screens -- they are embedded as sections inside
        # the ⚙ settings screen below (their own view classes/logic are
        # unchanged, only where they're mounted changed).
        settings_view = SettingsView(
            on_save=handle_save_settings,
            on_reset=handle_reset_settings,
            on_login_telegram=handle_login_telegram,
            on_logout_telegram=handle_logout_telegram,
            channels_view=channels_view,
            log_view=log_view,
        )
        print("main.py: SettingsView() constructed OK (Канали/Лог embedded).")

        def handle_bell_tap(_: ft.ControlEvent) -> None:
            page.run_task(_toggle_notifications)

        bell_button = ft.IconButton(
            icon=ft.Icons.NOTIFICATIONS_ROUNDED,
            icon_color=theme.ACCENT_BLUE,
            tooltip="Сповіщення",
            on_click=handle_bell_tap,
        )
        gear_button = ft.IconButton(
            icon=ft.Icons.SETTINGS_ROUNDED,
            icon_color=theme.TEXT_SECONDARY,
            tooltip="Налаштування",
            on_click=lambda e: handle_gear_tap(),
        )
        header_title = ft.Text(
            "сповіщення",
            size=16,
            weight=ft.FontWeight.W_600,
            color=theme.TEXT_PRIMARY,
        )

        header = ft.Container(
            padding=ft.padding.symmetric(horizontal=4),
            content=ft.Row(
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Row(
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[bell_button, header_title],
                    ),
                    gear_button,
                ],
            ),
        )
        print("main.py: header row built OK.")

        # --- Two swipeable screens (Огляд / Рух загроз), no top tab bar ----
        #
        # Flet 0.28.3 has no PageView-style swipeable-pager control, so this
        # is built from primitives already used elsewhere in the project:
        # an AnimatedSwitcher cross-fades between the two screens, and a
        # GestureDetector on top turns a left/right drag into a switch
        # between them (a true sliding-page transition would need a custom
        # widget beyond what Flet 0.28.3 exposes -- the fade is the closest
        # available approximation of "swipe to switch screen").
        screens: list[ft.Control] = [overview_view, movement_view]
        current_screen_index = [0]

        screen_switcher = ft.AnimatedSwitcher(
            content=screens[0],
            transition=ft.AnimatedSwitcherTransition.FADE,
            duration=220,
        )

        dot_overview = ft.Container(width=8, height=8, border_radius=4, bgcolor=theme.ACCENT_BLUE)
        dot_movement = ft.Container(width=8, height=8, border_radius=4, bgcolor=theme.BORDER)

        def _update_dots() -> None:
            dot_overview.bgcolor = theme.ACCENT_BLUE if current_screen_index[0] == 0 else theme.BORDER
            dot_movement.bgcolor = theme.ACCENT_BLUE if current_screen_index[0] == 1 else theme.BORDER
            if page is not None:
                dot_overview.update()
                dot_movement.update()

        def _set_screen(index: int) -> None:
            index = max(0, min(len(screens) - 1, index))
            if index == current_screen_index[0]:
                return
            current_screen_index[0] = index
            screen_switcher.content = screens[index]
            _update_dots()
            if page is not None:
                screen_switcher.update()

        dot_row = ft.Row(
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=8,
            controls=[
                ft.GestureDetector(content=dot_overview, on_tap_up=lambda e: _set_screen(0)),
                ft.GestureDetector(content=dot_movement, on_tap_up=lambda e: _set_screen(1)),
            ],
        )

        _drag_dx = [0.0]
        _SWIPE_THRESHOLD_PX = 40.0

        def handle_horizontal_drag_start(_: ft.DragStartEvent) -> None:
            _drag_dx[0] = 0.0

        def handle_horizontal_drag_update(e: ft.DragUpdateEvent) -> None:
            if e.delta_x is not None:
                _drag_dx[0] += e.delta_x

        def handle_horizontal_drag_end(_: ft.DragEndEvent) -> None:
            if _drag_dx[0] <= -_SWIPE_THRESHOLD_PX:
                _set_screen(current_screen_index[0] + 1)  # swiped left -> next screen
            elif _drag_dx[0] >= _SWIPE_THRESHOLD_PX:
                _set_screen(current_screen_index[0] - 1)  # swiped right -> previous screen
            _drag_dx[0] = 0.0

        swipe_area = ft.GestureDetector(
            content=screen_switcher,
            expand=True,
            on_horizontal_drag_start=handle_horizontal_drag_start,
            on_horizontal_drag_update=handle_horizontal_drag_update,
            on_horizontal_drag_end=handle_horizontal_drag_end,
        )

        main_area = ft.Container(
            expand=True,
            padding=ft.padding.only(top=12),
            content=ft.Column(expand=True, spacing=8, controls=[swipe_area, dot_row]),
        )

        # --- ⚙ Settings screen toggle (replaces the old "Налаштування" tab) -
        settings_open = [False]
        body_container = ft.Container(expand=True, content=main_area)

        def _show_settings() -> None:
            settings_open[0] = True
            body_container.content = _pad(settings_view)
            gear_button.icon = ft.Icons.CLOSE_ROUNDED
            gear_button.tooltip = "Закрити налаштування"
            header_title.value = "Налаштування"
            if page is not None:
                body_container.update()
                gear_button.update()
                header_title.update()

        def _show_main() -> None:
            settings_open[0] = False
            body_container.content = main_area
            gear_button.icon = ft.Icons.SETTINGS_ROUNDED
            gear_button.tooltip = "Налаштування"
            header_title.value = "сповіщення"
            if page is not None:
                body_container.update()
                gear_button.update()
                header_title.update()

        def handle_gear_tap() -> None:
            if settings_open[0]:
                _show_main()
            else:
                _show_settings()

        splash_overlay = ft.Container(
            expand=True,
            bgcolor=theme.BACKGROUND,
            alignment=ft.alignment.center,
            opacity=1.0,
            animate_opacity=ft.Animation(400, ft.AnimationCurve.EASE_OUT),
            content=ft.Column(
                alignment=ft.MainAxisAlignment.CENTER,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=28,
                controls=[
                    ft.ProgressRing(width=56, height=56, stroke_width=3, color=ft.Colors.WHITE),
                    ft.Text(
                        "MADE BY DEVELOPER XADOMOY",
                        size=13,
                        weight=ft.FontWeight.BOLD,
                        color=theme.SPLASH_TEXT_COLOR,
                        style=ft.TextStyle(letter_spacing=1.4),
                    ),
                ],
            ),
        )

        async def _hide_splash() -> None:
            """Fade the splash out, then remove it from hit-testing entirely."""
            splash_overlay.opacity = 0.0
            if page is not None:
                splash_overlay.update()
            await asyncio.sleep(0.42)
            splash_overlay.visible = False
            if page is not None:
                splash_overlay.update()

        page.add(
            ft.Stack(
                expand=True,
                controls=[
                    ft.Column(
                        expand=True,
                        spacing=12,
                        controls=[header, body_container],
                    ),
                    splash_overlay,
                ],
            )
        )
        print("main.py: page.add() OK -- control tree attached.")
        page.update()
        print("main.py: page.update() OK -- first frame should now be visible.")

        def handle_page_resized(_: ft.ControlEvent) -> None:
            if page.width:
                overview_view.ukraine_map.resize(float(page.width))
                movement_view.movement_map.resize(float(page.width))

        page.on_resized = handle_page_resized
        if page.width:
            overview_view.ukraine_map.resize(float(page.width))
            movement_view.movement_map.resize(float(page.width))

        # --- Background wiring (safe to define now that controls exist) ---

        def on_snapshot_changed(snapshot: ThreatSnapshot, region_states: dict) -> None:
            previous_level = getattr(on_snapshot_changed, "_previous_level", None)
            overview_view.update_snapshot(snapshot)
            overview_view.update_region_states(region_states)
            if (
                settings_holder[0].notifications_enabled
                and previous_level is not None
                and previous_level != snapshot.overall_risk
            ):
                _show_snackbar(page, f"Рівень загрози змінено: {snapshot.overall_risk.label_uk}")
                logger.info(f"Рівень загрози змінено на: {snapshot.overall_risk.label_uk}")
            on_snapshot_changed._previous_level = snapshot.overall_risk  # type: ignore[attr-defined]

        alert_service.set_listener(on_snapshot_changed)

        def on_log_added(entry: LogEntry) -> None:
            log_view.append_entry(entry)

        logger.set_listener(on_log_added)

        def on_telegram_message(username: str, text: str) -> None:
            page.run_task(_process_telegram_message, username, text)

        telegram_service.set_message_listener(on_telegram_message)

        def on_channel_status_changed(username: str, connected: bool) -> None:
            for channel in channels:
                if channel.username == username:
                    channel.connected = connected
                    channel.last_update = datetime.now()
                    channels_view.refresh_channel(channel)
                    _refresh_channels_summary()
                    break

        telegram_service.set_channel_status_listener(on_channel_status_changed)

        # --- Small shared helpers --------------------------------------------

        def _refresh_channels_summary() -> None:
            connected = sum(1 for c in channels if c.connected)
            overview_view.set_channels_summary(len(channels), connected)

        def _refresh_bell_icon() -> None:
            enabled = settings_holder[0].notifications_enabled
            bell_button.icon = (
                ft.Icons.NOTIFICATIONS_ROUNDED if enabled else ft.Icons.NOTIFICATIONS_OFF_ROUNDED
            )
            bell_button.icon_color = theme.ACCENT_BLUE if enabled else theme.TEXT_MUTED
            if page is not None:
                bell_button.update()

        def _prune_expired_movements() -> bool:
            """Drop movement entries older than ``_MOVEMENT_TTL_SECONDS``.

            Returns True if anything was actually removed, so callers can
            decide whether the tab needs a redraw.
            """
            cutoff = datetime.now() - timedelta(seconds=_MOVEMENT_TTL_SECONDS)
            before = len(movements)
            movements[:] = [m for m in movements if m.received_at >= cutoff]
            return len(movements) != before

        # --- Async task implementations -------------------------------------

        async def _process_telegram_message(username: str, text: str) -> None:
            try:
                analysis = risk_analyzer.analyze(text)
                for channel in channels:
                    if channel.username == username:
                        channel.messages_count += 1
                        channel.last_update = datetime.now()
                        channels_view.refresh_channel(channel)
                        break
                alert_service.apply_message_analysis(analysis, username)

                # Free, no-API-key region source (see region_alert_parser.py):
                # if this monitored channel names an oblast directly together
                # with a тривога/відбій marker, reflect that immediately.
                # Only applied as a fallback while alerts.in.ua isn't the
                # working/authoritative source (no token, or last request
                # failed) -- otherwise both sources would fight over the
                # same region's state on every refresh/message.
                if alert_service.snapshot().api_status != ApiStatus.OK:
                    for region, is_active in parse_region_alerts(text):
                        alert_service.set_region_active(region, is_active)

                # ChannelMessage.risk_contribution/matched_keywords are kept
                # exactly as they were (a display float + keyword list) so
                # OverviewView's message list needs no changes -- the float
                # shown is now this message's tier severity (0/22/45/72/96)
                # rather than an additive score, and matched_keywords is the
                # new analyzer's matched_terms.
                overview_view.add_message(
                    ChannelMessage(
                        channel_username=username,
                        text=text,
                        risk_contribution=analysis.tier.value,
                        matched_keywords=list(analysis.matched_terms),
                    )
                )
                if analysis.matched_terms:
                    logger.info(
                        f"Повідомлення з {username}: {analysis.family}/{analysis.status.value}, "
                        f"збіги {list(analysis.matched_terms)} (тир. {analysis.tier.value:.0f})"
                    )

                movement = parse_message(text, username, datetime.now())
                if movement is not None:
                    movements.append(movement)
                    _prune_expired_movements()
                    movement_view.set_movements(movements)
                    logger.info(
                        f"Рух загроз: {movement.threat_type.label_uk} "
                        f"({movement.short_description}) з {username}"
                    )
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Помилка обробки повідомлення: {exc}")

        async def _add_channel(username: str) -> None:
            try:
                if any(c.username == username for c in channels):
                    _show_snackbar(page, "Такий канал вже додано.")
                    return
                channels.append(TelegramChannel(username=username, display_name=username))
                channels_view.set_channels(channels)
                _refresh_channels_summary()
                await storage.save_channels(channels)
                await telegram_service.update_monitored_channels(_enabled_usernames(channels))
                logger.info(f"Додано канал: {username}")
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Не вдалося додати канал {username}: {exc}")

        async def _toggle_channel(username: str, enabled: bool) -> None:
            try:
                for channel in channels:
                    if channel.username == username:
                        channel.enabled = enabled
                        break
                await storage.save_channels(channels)
                await telegram_service.update_monitored_channels(_enabled_usernames(channels))
                logger.info(f"Канал {username}: {'увімкнено' if enabled else 'вимкнено'}")
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Не вдалося оновити канал {username}: {exc}")

        async def _remove_channel(username: str) -> None:
            try:
                channels[:] = [c for c in channels if c.username != username]
                channels_view.set_channels(channels)
                _refresh_channels_summary()
                await storage.save_channels(channels)
                await telegram_service.update_monitored_channels(_enabled_usernames(channels))
                logger.info(f"Видалено канал: {username}")
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Не вдалося видалити канал {username}: {exc}")

        async def _clear_log() -> None:
            try:
                await logger.clear()
                log_view.set_entries(logger.entries)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Не вдалося очистити журнал: {exc}")

        async def _toggle_notifications() -> None:
            try:
                current = settings_holder[0]
                updated = AppSettings(
                    api_id=current.api_id,
                    api_hash=current.api_hash,
                    alerts_api_token=current.alerts_api_token,
                    update_interval_seconds=current.update_interval_seconds,
                    auto_start_monitoring=current.auto_start_monitoring,
                    notifications_enabled=not current.notifications_enabled,
                    watched_regions=current.watched_regions,
                )
                settings_holder[0] = updated
                await storage.save_settings(updated)
                settings_view.set_settings(updated)
                _refresh_bell_icon()
                logger.info(
                    f"Сповіщення: {'увімкнено' if updated.notifications_enabled else 'вимкнено'}"
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Не вдалося змінити сповіщення: {exc}")

        async def _save_settings(new_settings: AppSettings) -> None:
            try:
                old_settings = settings_holder[0]
                settings_holder[0] = new_settings
                interval_box.seconds = new_settings.update_interval_seconds
                await storage.save_settings(new_settings)
                _refresh_bell_icon()
                logger.info("Налаштування збережено.")
                _show_snackbar(page, "Налаштування збережено.")

                credentials_changed = (
                    old_settings.api_id != new_settings.api_id
                    or old_settings.api_hash != new_settings.api_hash
                )
                if credentials_changed and new_settings.api_id and new_settings.api_hash:
                    await telegram_service.stop()
                    page.run_task(_start_telegram)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Не вдалося зберегти налаштування: {exc}")

        async def _reset_settings() -> None:
            try:
                defaults = AppSettings.default()
                settings_holder[0] = defaults
                interval_box.seconds = defaults.update_interval_seconds
                settings_view.set_settings(defaults)
                await storage.save_settings(defaults)
                _refresh_bell_icon()
                movement_view.set_selected_regions(_watched_regions_from_settings(defaults))
                logger.info("Налаштування скинуто до типових.")
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Не вдалося скинути налаштування: {exc}")

        async def _persist_selected_regions(regions: set[Region]) -> None:
            """Save the oblast selection made on the Рух загроз screen.

            Reuses the existing ``AppSettings.watched_regions`` field/
            storage key (no new LocalStorage entry needed) -- only where
            it's edited moved, from Налаштування to this screen, per the
            redesign. ``settings_view`` is told about the new value too
            so its own (unrelated) Save button doesn't overwrite this
            with a stale one later.
            """
            try:
                current = settings_holder[0]
                watched_values = sorted(region.value for region in regions)
                updated = AppSettings(
                    api_id=current.api_id,
                    api_hash=current.api_hash,
                    alerts_api_token=current.alerts_api_token,
                    update_interval_seconds=current.update_interval_seconds,
                    auto_start_monitoring=current.auto_start_monitoring,
                    notifications_enabled=current.notifications_enabled,
                    watched_regions=watched_values,
                )
                settings_holder[0] = updated
                await storage.save_settings(updated)
                settings_view.set_watched_regions(watched_values)
                logger.info(f"Обрані області на карті руху загроз: {watched_values or '—'}")
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Не вдалося зберегти обрані області: {exc}")

        async def _login_telegram() -> None:
            logger.info("Telegram: натиснуто кнопку 'Увійти в Telegram'.")
            try:
                phone_future: asyncio.Future = asyncio.get_event_loop().create_future()
                _ask_for_input(page, "Введіть номер телефону", "+380...", phone_future)
                phone = await phone_future
                logger.info(f"Telegram: номер телефону отримано від користувача (довжина={len(phone)}).")

                async def request_code() -> str:
                    # IMPORTANT: this dialog must be shown only when Telethon actually
                    # calls this callback -- which happens AFTER send_code_request()
                    # has already run inside TelegramService._interactive_login().
                    # Previously this project pre-collected the code via a dialog
                    # shown immediately after the phone dialog, i.e. BEFORE
                    # send_code_request() had even been called -- so the user was
                    # asked for a code that Telegram had not been told to send yet.
                    # That was the actual root cause of "code never arrives".
                    logger.info("Telegram: send_code_request() виконано, показ діалогу введення коду.")
                    future: asyncio.Future = asyncio.get_event_loop().create_future()
                    _ask_for_input(page, "Введіть код із Telegram", "12345", future)
                    code = await future
                    logger.info(f"Telegram: код отримано від користувача (довжина={len(code)}).")
                    return code

                async def request_password() -> str:
                    future: asyncio.Future = asyncio.get_event_loop().create_future()
                    _ask_for_input(page, "Пароль двофакторної автентифікації", "", future, is_password=True)
                    return await future

                callbacks = TelegramAuthCallbacks(
                    request_phone=lambda: _completed_future(phone),
                    request_code=request_code,
                    request_password=request_password,
                )
                page.run_task(_start_telegram, callbacks)
            except Exception as exc:  # noqa: BLE001 - must be visible, never hidden
                logger.error(f"Помилка входу в Telegram: {type(exc).__name__}: {exc}")
                _show_snackbar(page, f"Помилка входу в Telegram: {exc}")

        async def _handle_session_ready(session_string: str) -> None:
            """Called by TelegramService the moment auth succeeds (see telegram_service.py).

            Fires immediately, unlike this function's caller (`start()`)
            which keeps running for the whole connected session -- this is
            the only reliable place to flip the UI status to "connected"
            right when it actually happens, not only after disconnect.
            """
            await storage.save_session_string(session_string)
            settings_view.set_telegram_status(True)
            settings_view.set_session_status(True)

        async def _start_telegram(auth_callbacks: Optional[TelegramAuthCallbacks] = None) -> None:
            try:
                settings = settings_holder[0]
                logger.info(
                    "Telegram: запуск _start_telegram "
                    f"(api_id={'вказано' if settings.api_id else 'ПОРОЖНЬО'}, "
                    f"api_hash={'вказано' if settings.api_hash else 'ПОРОЖНЬО'})."
                )
                session_string = await storage.load_session_string()
                logger.info(f"Telegram: збережена сесія завантажена (є сесія: {bool(session_string)}).")
                await telegram_service.start(
                    settings.api_id,
                    settings.api_hash,
                    session_string,
                    auth_callbacks,
                    on_auth_error=lambda message: _show_snackbar(page, message),
                    on_session_ready=_handle_session_ready,
                )
            except Exception as exc:  # noqa: BLE001 - must be visible, never hidden
                logger.error(f"Не вдалося запустити Telegram: {type(exc).__name__}: {exc}")
                _show_snackbar(page, f"Не вдалося запустити Telegram: {exc}")
            finally:
                # start() only reaches here after the connection has
                # actually ended (stop() called, or given up reconnecting)
                # -- so this path correctly reflects "disconnected", while
                # "connected" is reported separately, immediately, above.
                try:
                    if telegram_service.session_string:
                        await storage.save_session_string(telegram_service.session_string)
                except Exception as exc:  # noqa: BLE001 - must be visible, never hidden
                    logger.error(f"Не вдалося зберегти сесію Telegram: {type(exc).__name__}: {exc}")
                settings_view.set_telegram_status(telegram_service.is_running)
                settings_view.set_session_status(bool(telegram_service.session_string))

        async def _logout_telegram() -> None:
            """Disconnect Telegram and clear the locally saved session string.

            This clears the session cached on this device only (so the next
            "Увійти в Telegram" needs a fresh code) -- it does not revoke the
            session on Telegram's servers, which would need a separate
            ``client.log_out()`` call the existing TelegramService does not
            expose; adding that is a bigger change than "reset session"
            implies, so this stays a local-only reset for now.
            """
            logger.info("Telegram: натиснуто кнопку 'Скинути сесію'.")
            try:
                await telegram_service.stop()
                await storage.save_session_string("")
                settings_view.set_telegram_status(False)
                settings_view.set_session_status(False)
                logger.info("Telegram: сесію скинуто, з'єднання зупинено.")
                _show_snackbar(page, "Сесію Telegram скинуто.")
            except Exception as exc:  # noqa: BLE001 - must be visible, never hidden
                logger.error(f"Не вдалося скинути сесію Telegram: {type(exc).__name__}: {exc}")
                _show_snackbar(page, f"Не вдалося скинути сесію Telegram: {exc}")

        vocabulary_cache_holder: list[VocabularyCache] = [VocabularyCache()]

        def _apply_learned_phrases() -> None:
            cache = vocabulary_cache_holder[0]
            risk_analyzer.set_learned_phrases(
                [(p.family, p.as_tier(), p.phrase) for p in cache.learned_phrases]
            )

        async def _run_vocabulary_update() -> None:
            """One Vocabulary Builder pass over every enabled channel's new history."""
            enabled = _enabled_usernames(channels)
            if not enabled or not telegram_service.is_running:
                logger.info("Vocabulary Builder: пропуск -- немає активних каналів або Telegram не підключено.")
                return
            try:
                updated_cache = await vocabulary_builder.run_incremental_update(
                    enabled, vocabulary_cache_holder[0]
                )
                vocabulary_cache_holder[0] = updated_cache
                _apply_learned_phrases()
                await storage.save_vocabulary_cache(updated_cache)
            except Exception as exc:  # noqa: BLE001 - one bad run must not kill the periodic loop
                logger.error(f"Vocabulary Builder: помилка оновлення: {type(exc).__name__}: {exc}")

        async def _vocabulary_update_loop() -> None:
            """Periodic background task -- see VocabularyBuilder's module docstring.

            Waits a while after startup (so it never competes with initial
            Telegram connection/UI setup), then re-runs every
            ``_VOCABULARY_UPDATE_INTERVAL_SECONDS`` -- each run is
            incremental (only new history since last time), so repeat runs
            are cheap regardless of how much total history exists.
            """
            await asyncio.sleep(_VOCABULARY_INITIAL_DELAY_SECONDS)
            while True:
                await _run_vocabulary_update()
                await asyncio.sleep(_VOCABULARY_UPDATE_INTERVAL_SECONDS)

        async def _tick_loop() -> None:
            """Background loop: periodic API refresh, risk decay, persistence."""
            last_flush = datetime.now()
            while True:
                try:
                    await asyncio.sleep(1)
                    elapsed = 1.0
                    overview_view.tick(datetime.now())
                    alert_service.decay_telegram_risk(elapsed)

                    seconds_since_flush = (datetime.now() - last_flush).total_seconds()
                    if seconds_since_flush >= interval_box.seconds:
                        await alert_service.refresh_from_api(settings_holder[0].alerts_api_token)
                        if _prune_expired_movements():
                            movement_view.set_movements(movements)
                        await logger.flush()
                        await storage.save_channels(channels)
                        last_flush = datetime.now()
                except Exception as exc:  # noqa: BLE001 - the tick loop must never die
                    logger.error(f"Помилка фонового оновлення: {exc}")

        async def _initialize() -> None:
            """Load persisted state, then start background services."""
            print("main.py: _initialize() STARTED (background task).")
            try:
                await logger.load_persisted()
                log_view.set_entries(logger.entries)
                logger.info("Застосунок запущено.")
                print("main.py: _initialize(): logger/log_view ready.")

                loaded_settings = await storage.load_settings()
                settings_holder[0] = loaded_settings
                interval_box.seconds = loaded_settings.update_interval_seconds
                settings_view.set_settings(loaded_settings)
                _refresh_bell_icon()
                movement_view.set_selected_regions(_watched_regions_from_settings(loaded_settings))
                print("main.py: _initialize(): settings loaded and applied.")

                initial_session_string = await storage.load_session_string()
                settings_view.set_session_status(bool(initial_session_string))
                print("main.py: _initialize(): session status applied.")

                logger.info("alerts.in.ua: самодіагностика при старті...")
                diagnostic_steps = await alert_service.run_self_diagnostic(
                    loaded_settings.alerts_api_token
                )
                for passed, message in diagnostic_steps:
                    mark = "✓" if passed else "✗"
                    logger.info(f"alerts.in.ua: {mark} {message}")
                print(f"main.py: _initialize(): alerts.in.ua self-diagnostic -> {len(diagnostic_steps)} step(s) logged.")

                if loaded_settings.alerts_api_token:
                    await alert_service.refresh_from_api(loaded_settings.alerts_api_token)
                    print("main.py: _initialize(): first alerts.in.ua refresh attempted.")

                loaded_channels = await storage.load_channels()
                channels[:] = loaded_channels
                channels_view.set_channels(channels)
                _refresh_channels_summary()
                print(f"main.py: _initialize(): {len(channels)} channel(s) loaded.")

                # ROOT CAUSE FIX: this call was previously missing. Loading
                # channels into the `channels` list/UI above does NOT tell
                # TelegramService which chats to subscribe to -- only
                # _add_channel/_toggle_channel/_remove_channel did that.
                # Without it, a fresh app start (or restart) restored the
                # channel list visually, but Telethon's internal monitored-
                # set stayed empty and _register_handlers() subscribed to
                # nothing, so no messages were ever received until the user
                # toggled a channel (which happened to call this). That in
                # turn meant risk_analyzer never saw a single message, so
                # risk/active-region counts never moved off 0 either.
                enabled_at_startup = _enabled_usernames(channels)
                await telegram_service.update_monitored_channels(enabled_at_startup)
                logger.info(
                    f"Telegram: підписано на {len(enabled_at_startup)} збережений(і) канал(и) "
                    f"зі сховища: {enabled_at_startup or '—'}."
                )
                print("main.py: _initialize(): monitored channels applied to TelegramService.")

                overview_view.update_snapshot(alert_service.snapshot())
                overview_view.update_region_states(alert_service.region_states)
                print("main.py: _initialize(): overview snapshot/region states applied.")

                vocabulary_cache_holder[0] = await storage.load_vocabulary_cache()
                _apply_learned_phrases()
                logger.info(
                    f"Vocabulary Builder: завантажено {len(vocabulary_cache_holder[0].learned_phrases)} "
                    "раніше вивчену(і) фразу(и) з кешу."
                )
                print("main.py: _initialize(): vocabulary cache loaded and applied.")

                page.run_task(_tick_loop)
                page.run_task(_vocabulary_update_loop)

                if loaded_settings.auto_start_monitoring and loaded_settings.api_id and loaded_settings.api_hash:
                    print("main.py: _initialize(): auto-start monitoring -> scheduling _start_telegram().")
                    page.run_task(_start_telegram)
                print("main.py: _initialize() FINISHED without exceptions.")
            except Exception as exc:  # noqa: BLE001 - must be visible, never hidden
                full_traceback = traceback.format_exc()
                print("main.py: _initialize() RAISED -- full traceback follows:")
                print(full_traceback)
                logger.error(f"Помилка ініціалізації: {type(exc).__name__}: {exc}")
                _show_snackbar(page, f"Помилка ініціалізації: {type(exc).__name__}: {exc}")
            finally:
                # Whatever happened above, the splash must not stay on
                # screen forever -- that would look exactly like the
                # "frozen white screen" this splash exists to be
                # distinguishable from.
                await _hide_splash()

        page.run_task(_initialize)
        print("main.py: main() FINISHED building the UI without exceptions.")
    except Exception as exc:  # noqa: BLE001 - this is the last line of defense:
        # if ANYTHING above raises, the user must still see something instead
        # of a permanently black/blank screen. Print full details (visible via
        # `adb logcat` / the platform's own log capture) and render a minimal,
        # dependency-free fallback screen with the exact error, so a failure
        # anywhere in UI construction is always visible and diagnosable.
        _render_fatal_error_screen(page, exc)


def _render_fatal_error_screen(page: ft.Page, exc: BaseException) -> None:
    """Last line of defense: show the exact error instead of a blank screen.

    Deliberately uses no theme, no custom components, and no other
    project code -- only bare Flet primitives with hardcoded colors --
    so that whatever broke the real UI cannot also break this fallback.
    Prints the full traceback (visible in ``adb logcat`` / build logs)
    and also renders it on screen, selectable, so it can be copied or
    photographed directly from the device.
    """
    full_traceback = traceback.format_exc()
    print("=" * 70)
    print("main.py: FATAL ERROR DURING STARTUP -- full traceback follows:")
    print(full_traceback)
    print("=" * 70)

    try:
        page.clean()
    except Exception:  # noqa: BLE001 - page.clean() itself must never block this fallback
        pass

    page.bgcolor = "#1A0000"
    page.padding = 16
    page.scroll = ft.ScrollMode.ALWAYS
    page.add(
        ft.Column(
            spacing=12,
            controls=[
                ft.Text(
                    "Помилка запуску застосунку",
                    size=20,
                    weight=ft.FontWeight.BOLD,
                    color="#FF6B6B",
                ),
                ft.Text(
                    f"{type(exc).__name__}: {exc}",
                    size=14,
                    color="#FFFFFF",
                    selectable=True,
                ),
                ft.Divider(color="#553333"),
                ft.Text(
                    "Повний traceback (можна скопіювати):",
                    size=12,
                    color="#CCCCCC",
                ),
                ft.Text(
                    full_traceback,
                    size=11,
                    color="#FFCCCC",
                    selectable=True,
                ),
            ],
        )
    )
    try:
        page.update()
    except Exception:  # noqa: BLE001 - nothing more we can safely do if even this fails
        print("main.py: page.update() for the fallback error screen ALSO raised. See traceback above.")


def _configure_page(page: ft.Page) -> None:
    """Apply page-level settings that are safe on every platform."""
    page.title = "Air Alert Analyzer"
    page.theme_mode = ft.ThemeMode.DARK
    page.theme = build_page_theme()
    page.bgcolor = theme.BACKGROUND
    page.padding = theme.PAGE_PADDING
    page.scroll = None

    # Desktop-only window sizing. Never touch page.window on Android/iOS —
    # this was one of the crash causes in the previous project. This app
    # is Android-first; the desktop branch below only makes local dev
    # convenient and is skipped entirely on Android/iOS.
    is_desktop = page.platform in (
        ft.PagePlatform.WINDOWS,
        ft.PagePlatform.MACOS,
        ft.PagePlatform.LINUX,
    )
    if is_desktop:
        page.window.width = 420
        page.window.height = 860
        page.window.min_width = 360
        page.window.min_height = 640


def _pad(control: ft.Control) -> ft.Container:
    """Wrap a tab's content with consistent padding."""
    return ft.Container(content=control, padding=ft.padding.only(top=12), expand=True)


def _enabled_usernames(channels: list[TelegramChannel]) -> list[str]:
    """Return usernames of channels currently enabled for monitoring."""
    return [c.username for c in channels if c.enabled]


def _watched_regions_from_settings(settings: AppSettings) -> set[Region]:
    """Parse the persisted ``watched_regions`` string list back into a set of Region.

    Silently skips any value that no longer matches a known Region (e.g.
    after a future rename) instead of raising -- the movement filter
    degrading to "no regions selected" is harmless, unlike a startup crash.
    """
    result: set[Region] = set()
    for raw_value in settings.watched_regions:
        try:
            result.add(Region(raw_value))
        except ValueError:
            continue
    return result


async def _completed_future(value: str) -> str:
    """Wrap an already-known value as an awaitable (keeps callback types uniform)."""
    return value


def _show_snackbar(page: ft.Page, message: str) -> None:
    """Show a short, auto-dismissing snackbar message."""
    page.open(ft.SnackBar(content=ft.Text(message), open=True))


def _show_region_info_dialog(page: ft.Page, region: Region, state: Optional[RegionState]) -> None:
    """Show a dialog with the current alert info for a tapped region."""
    is_active = bool(state and state.is_active)
    status_text = "Активна повітряна тривога" if is_active else "Тривоги немає"
    changed_text = (
        f"Змінено: {state.last_changed:%H:%M:%S}" if state is not None else ""
    )

    dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text(region.value),
        content=ft.Column(
            tight=True,
            spacing=6,
            controls=[
                ft.Text(status_text),
                ft.Text(changed_text, size=12, color=theme.TEXT_SECONDARY),
            ],
        ),
        actions=[ft.TextButton("Закрити", on_click=lambda e: page.close(dialog))],
    )
    page.open(dialog)


def _show_movement_dialog(page: ft.Page, movement: ThreatMovement) -> None:
    """Show a dialog with the full source message behind one movement entry."""
    direction_text = (
        f"Напрямок: {movement.short_description}"
        if movement.origin_name or movement.destination_name
        else "Напрямок у повідомленні не вказано"
    )

    dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text(f"{movement.threat_type.icon} {movement.threat_type.label_uk}"),
        content=ft.Column(
            tight=True,
            spacing=8,
            scroll=ft.ScrollMode.AUTO,
            controls=[
                ft.Text(direction_text, size=13, weight=ft.FontWeight.W_600, color=theme.ACCENT_BLUE),
                ft.Text(
                    f"Канал: {movement.channel_username}",
                    size=12,
                    color=theme.TEXT_SECONDARY,
                ),
                ft.Text(
                    f"Час: {movement.received_at:%H:%M:%S %d.%m.%Y}",
                    size=12,
                    color=theme.TEXT_SECONDARY,
                ),
                ft.Divider(color=theme.BORDER),
                ft.Text("Повний текст повідомлення:", size=12, color=theme.TEXT_MUTED),
                ft.Text(movement.text, size=13, color=theme.TEXT_PRIMARY, selectable=True),
            ],
        ),
        actions=[ft.TextButton("Закрити", on_click=lambda e: page.close(dialog))],
    )
    page.open(dialog)


def _ask_for_input(
    page: ft.Page,
    title: str,
    hint: str,
    future: "asyncio.Future[str]",
    is_password: bool = False,
) -> None:
    """Show a small dialog collecting one text value into ``future``."""
    field = ft.TextField(hint_text=hint, password=is_password, can_reveal_password=is_password)

    def submit(_: ft.ControlEvent) -> None:
        value = field.value or ""
        page.close(dialog)
        if not future.done():
            future.set_result(value)

    dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text(title),
        content=field,
        actions=[ft.ElevatedButton("OK", on_click=submit)],
    )
    page.open(dialog)


if __name__ == "__main__":
    ft.app(target=main)
