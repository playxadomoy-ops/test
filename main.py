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
full-screen Налаштування view (server connection, alert source token,
update interval, behavior switches, and watched regions, all in one
place) until tapped again.

Note on architecture: this app does NOT connect to Telegram directly and
has no Telethon dependency. All Telegram monitoring/parsing happens in
the separate server application; this app only consumes the server's
already-processed data via app.services.server_client.ServerClient
(plain HTTP polling, see that module for why it avoids WebSockets too).
"""

from __future__ import annotations


import asyncio
import dataclasses
import traceback
from datetime import datetime, timedelta
from typing import Optional

import flet as ft


from app.models.alert_models import Region, RegionState, RiskLevel, ThreatSnapshot
from app.models.channel_models import ChannelMessage
from app.models.compass_models import CompassSnapshot
from app.models.movement_models import MAX_POSITION_HISTORY, ThreatMovement, ThreatType
from app.models.settings_models import AppSettings
from app.services.alert_service import AlertService
from app.services.compass_builder import build_compass_snapshot
from app.services.compass_sensor import CompassSensorService
from app.services.logger_service import LoggerService
from app.services.risk_analyzer import RiskAnalyzer
from app.services.server_client import ServerClient
from app.storage.local_storage import LocalStorage


from app.ukraine_geo import project_lat_lon


from app.ui.components.icon_glyph import icon_glyph
from app.ui.icon_assets import (
    ICON_SIREN_ACTIVE,
    ICON_SIREN_CLEAR,
    color_for_movement,
    icon_for_movement,
)
from app.ui.theme import colors as theme
from app.ui.theme.colors import build_page_theme
from app.ui.views.movement_view import MovementView
from app.ui.views.overview_view import OverviewView
from app.ui.views.settings_view import SettingsView
from app.ui.views.threat_compass_view import ThreatCompassView



class _MutableInterval:
    """Tiny box so the background tick loop can see interval updates live."""

    seconds: int = 30


#: How long a "Рух загроз" entry stays on the map/list after its message
#: arrived. These are transient, time-sensitive alerts (a drone/missile in
#: flight), not a permanent log -- 2 hours comfortably covers the typical
#: flight time of a Shahed-type drone across Ukraine while still clearing
#: out stale entries automatically. Also used as a safety-net expiry for
#: server-sourced movements, in case a "destroyed"/"expired" event from
#: the server is ever missed by a poll.
_MOVEMENT_TTL_SECONDS = 2 * 60 * 60


async def main(page: ft.Page) -> None:
    """Flet entry point: build the UI, then asynchronously bring it to life."""
    try:
        _configure_page(page)

        # --- Dependency graph (constructor injection, no globals) ----------
        storage = LocalStorage(page)
        logger = LoggerService(storage)
        risk_analyzer = RiskAnalyzer()
        alert_service = AlertService(logger, risk_analyzer)
        server_client = ServerClient(logger)

        interval_box = _MutableInterval()
        movements: list[ThreatMovement] = []
        settings_holder: list[AppSettings] = [AppSettings.default()]

        # --- UI shell, built and shown immediately --------------------------

        def handle_region_tap(region: Region) -> None:
            _show_region_info_dialog(page, region, alert_service.region_states.get(region))

        def handle_district_tap(region: Region, district_name: str) -> None:
            _show_district_info_dialog(page, region, district_name, alert_service.region_states.get(region))

        def handle_open_server_settings() -> None:
            _show_settings()

        overview_view = OverviewView(
            on_region_tap=handle_region_tap,
            on_district_tap=handle_district_tap,
            on_open_server_settings=handle_open_server_settings,
        )

        def handle_movement_tap(movement: ThreatMovement) -> None:
            _show_movement_dialog(page, movement)

        def handle_selected_regions_changed(regions: set[Region]) -> None:
            page.run_task(_persist_selected_regions, regions)

        movement_view = MovementView(
            on_movement_tap=handle_movement_tap,
            on_region_tap=handle_region_tap,  # same info dialog as the Огляд map, per spec
            on_district_tap=handle_district_tap,  # same district dialog as the Огляд map, per spec
            on_selected_regions_changed=handle_selected_regions_changed,
        )

        compass_view = ThreatCompassView()
        compass_view.set_snapshot(CompassSnapshot.empty())

        # Real hardware compass (Android only -- see compass_sensor.py's
        # module docstring). Constructing CompassSensorService here is
        # safe and trivial (it only sets plain Python attributes, no
        # JNI/Android access at all -- see its __init__). The actual
        # sensor work (import pyjnius, touch SensorManager, ...) is
        # deliberately NOT started here: it's scheduled to run only
        # AFTER the main UI has actually been shown (see
        # _start_compass_sensor_deferred, scheduled right after
        # page.update() below) so a failure there can never prevent the
        # app from starting in the first place.
        compass_sensor_service = CompassSensorService(log=logger.debug)

        def handle_save_settings(settings: AppSettings) -> None:
            page.run_task(_save_settings, settings)

        def handle_reset_settings() -> None:
            page.run_task(_reset_settings)

        def handle_connect_server() -> None:
            page.run_task(_connect_server)

        def handle_disconnect_server() -> None:
            page.run_task(_disconnect_server)

        settings_view = SettingsView(
            on_save=handle_save_settings,
            on_reset=handle_reset_settings,
            on_watched_regions_changed=handle_selected_regions_changed,
            on_connect_server=handle_connect_server,
            on_disconnect_server=handle_disconnect_server,
        )

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

        # --- Three swipeable screens (Огляд / Рух загроз / Компас загроз),
        # no top tab bar ----
        #
        # Flet 0.28.3 has no PageView-style swipeable-pager control, so this
        # is built from primitives already used elsewhere in the project:
        # an AnimatedSwitcher cross-fades between the screens, and a
        # GestureDetector on top turns a left/right drag into a switch
        # between them (a true sliding-page transition would need a custom
        # widget beyond what Flet 0.28.3 exposes -- the fade is the closest
        # available approximation of "swipe to switch screen").
        screens: list[ft.Control] = [overview_view, movement_view, compass_view]
        current_screen_index = [0]

        screen_switcher = ft.AnimatedSwitcher(
            content=screens[0],
            transition=ft.AnimatedSwitcherTransition.FADE,
            duration=220,
        )

        dot_overview = ft.Container(width=8, height=8, border_radius=4, bgcolor=theme.ACCENT_BLUE)
        dot_movement = ft.Container(width=8, height=8, border_radius=4, bgcolor=theme.BORDER)
        dot_compass = ft.Container(width=8, height=8, border_radius=4, bgcolor=theme.BORDER)

        def _update_dots() -> None:
            dot_overview.bgcolor = theme.ACCENT_BLUE if current_screen_index[0] == 0 else theme.BORDER
            dot_movement.bgcolor = theme.ACCENT_BLUE if current_screen_index[0] == 1 else theme.BORDER
            dot_compass.bgcolor = theme.ACCENT_BLUE if current_screen_index[0] == 2 else theme.BORDER
            if page is not None:
                dot_overview.update()
                dot_movement.update()
                dot_compass.update()

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
                ft.GestureDetector(content=dot_compass, on_tap_up=lambda e: _set_screen(2)),
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

        # A small branded lockup (app name + accent-colored spinner) rather
        # than a bare spinner -- gives the splash a "this is a real
        # product" identity instead of reading as a loading placeholder.
        # ``scale`` starts slightly below 1.0 and animates up on mount
        # (see below) for a soft, premium entrance instead of popping in
        # at full size the instant the frame renders.
        splash_content = ft.Column(
            alignment=ft.MainAxisAlignment.CENTER,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=22,
            controls=[
                ft.Text(
                    "AIR ALERT ANALYZER",
                    size=15,
                    weight=ft.FontWeight.BOLD,
                    color=theme.TEXT_PRIMARY,
                    style=ft.TextStyle(letter_spacing=2.2),
                ),
                ft.ProgressRing(
                    width=52, height=52, stroke_width=3, color=theme.ACCENT_BLUE
                ),
                ft.Text(
                    "MADE BY DEVELOPER XADOMOY",
                    size=11,
                    weight=ft.FontWeight.W_600,
                    color=theme.TEXT_MUTED,
                    style=ft.TextStyle(letter_spacing=1.4),
                ),
            ],
        )

        splash_overlay = ft.Container(
            expand=True,
            bgcolor=theme.BACKGROUND,
            alignment=ft.alignment.center,
            opacity=1.0,
            scale=1.0,
            animate_opacity=theme.ANIM_SLOW,
            animate_scale=theme.ANIM_SLOW,
            content=splash_content,
        )

        async def _hide_splash() -> None:
            """Fade + gently scale the splash out, then remove it from hit-testing entirely."""
            splash_overlay.opacity = 0.0
            splash_overlay.scale = 1.03
            if page is not None:
                splash_overlay.update()
            # Matches theme.ANIM_SLOW's duration (380ms) plus a small
            # buffer so the splash is never yanked away mid-transition.
            await asyncio.sleep(0.45)
            splash_overlay.visible = False
            if page is not None:
                splash_overlay.update()

        # HapticFeedback for the region-alert notification -- NOT ft.Audio.
        # ft.Audio's Python wrapper ships in core `flet`, but the actual
        # player is a separate Flutter plugin (the `flet-audio` package)
        # that must be declared as a project dependency for `flet build`
        # to bundle it. This project only declares `flet==0.28.3` (see
        # pyproject.toml/requirements.txt) -- nothing bundles that plugin,
        # so the built app's Flutter shell has no renderer registered for
        # "Audio" at all, producing exactly the "Unknown control: Audio"
        # panel this replaces. HapticFeedback has no such gap: it wraps
        # Flutter's own built-in `services.dart` HapticFeedback class, not
        # a third-party plugin, so it's guaranteed present in every Flet
        # 0.28.3 build with zero extra packaging -- genuinely supported
        # out of the box, not a workaround.
        #
        # Note the real tradeoff: this vibrates (Android/iOS only; it's a
        # silent no-op on Windows/Linux/macOS, which have no vibration
        # motor) rather than playing an audible tone on every platform.
        # True cross-platform audible sound in Flet has no code-only
        # solution -- it requires adding `flet-audio` as a declared
        # dependency and rebuilding, which is a deliberate choice left to
        # the project owner rather than made silently here. The existing
        # snackbar + log line below (unaffected by any of this) remains
        # the guaranteed-visible notification on every platform.
        region_alert_haptic = ft.HapticFeedback()
        page.overlay.append(region_alert_haptic)

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
        page.update()

        # --- Diagnostic kill-switch (temporary, for isolating the
        # reported "crashes on any tap" bug) -----------------------------
        # False: probe() still runs (imports pyjnius, detects whether the
        # magnetometer/accelerometer/gyroscope exist, populates real
        # diagnostics) but start_listening() -- which creates the
        # PythonJavaClass SensorEventListener, registers it, and starts a
        # CONTINUOUS stream of callbacks on an Android-owned background
        # thread for as long as the app runs -- is skipped entirely. The
        # Compass page still opens normally and shows real
        # Magnetometer/Accelerometer/Gyroscope availability; only the
        # live heading stays "—" ("Компас: датчик недоступний"-style,
        # since compass_functional requires a heading to actually rotate
        # anything).
        #
        # If the crash stops happening with this False, that confirms the
        # continuous background-thread listener (not just importing
        # pyjnius or detecting sensors) as the cause. If it still
        # crashes, the cause is elsewhere. Flip to True only after that's
        # been confirmed safe on a real device.
        _COMPASS_LIVE_LISTENER_ENABLED = False

        async def _start_compass_sensor_deferred() -> None:
            """Run the real (Android-only) compass sensor init, well after
            the app has already started and rendered -- see the
            CompassSensorService construction above for why this is
            deliberately not done any earlier.

            Two separately-logged stages, per the project's staged
            rollout requirement: probe() (import pyjnius, detect
            sensors -- no listener yet) first, then, only if that
            reports both required sensors present AND the diagnostic
            kill-switch above is enabled, start_listening() (creates and
            registers the real-time listener). A short delay before even
            the first stage keeps this fully out of the way of the app's
            first paint and the rest of this function's own startup work
            below.

            CompassSensorService's own methods already catch every
            failure internally and always return a safe
            ``CompassDiagnostics`` (see compass_sensor.py) -- the
            try/except here is a deliberate second safety net, not a
            sign that the inner ones aren't trusted: this general area
            previously caused an unhandled startup crash, so belt and
            suspenders is intentional, not normal style for this project.
            """
            try:
                await asyncio.sleep(2.0)
                logger.debug("compass: starting deferred sensor probe")
                diagnostics = compass_sensor_service.probe()
                logger.debug(f"compass: probe result -- {diagnostics}")
                if diagnostics.compass_functional and _COMPASS_LIVE_LISTENER_ENABLED:
                    diagnostics = compass_sensor_service.start_listening()
                    compass_view.set_compass_diagnostics(diagnostics)
                elif diagnostics.compass_functional:
                    logger.debug(
                        "compass: sensors detected OK, but live listener is disabled (diagnostic build) -- "
                        "page stays on its default 'Компас: датчик недоступний' state"
                    )
                else:
                    compass_view.set_compass_diagnostics(diagnostics)
            except Exception as exc:  # noqa: BLE001 -- deliberate outer safety net, see docstring
                logger.error(f"Компас: не вдалося ініціалізувати датчик, компас вимкнено: {exc!r}")

        page.run_task(_start_compass_sensor_deferred)

        # Holds the in-flight debounce task (if any) so a new resize event
        # can cancel the previous one -- see _debounced_resize below.
        pending_resize_task: list[Optional["asyncio.Future"]] = [None]

        async def _debounced_resize(width: float) -> None:
            # Rapid window-drag resizing (desktop) or a device rotation
            # animation (Android) can fire many resize events a fraction
            # of a second apart. Each one re-runs both maps' full layout
            # math AND, when an oblast is focused, the settlement label
            # anti-overlap algorithm -- real work, not free. Waiting a
            # short beat for events to stop arriving means only the
            # *final* size actually gets laid out, instead of every
            # intermediate size on the way there.
            await asyncio.sleep(0.09)
            if page.width:
                overview_view.ukraine_map.resize(float(page.width))
                movement_view.movement_map.resize(float(page.width))
                compass_view.resize(float(page.width))

        def handle_page_resized(_: ft.ControlEvent) -> None:
            if not page.width:
                return
            previous = pending_resize_task[0]
            if previous is not None and not previous.done():
                previous.cancel()
            pending_resize_task[0] = page.run_task(_debounced_resize, float(page.width))

        page.on_resized = handle_page_resized
        if page.width:
            overview_view.ukraine_map.resize(float(page.width))
            movement_view.movement_map.resize(float(page.width))
            compass_view.resize(float(page.width))

        # --- Background wiring (safe to define now that controls exist) ---

        #: Minimum time between "risk level changed" toasts. The listener
        #: below fires on every AlertService._notify() -- including the
        #: once-a-second decay tick -- so a burst of messages that pushes
        #: the level up and back down several times within a few seconds
        #: (a real scenario during a heavy barrage) would otherwise pop a
        #: snackbar for every single flap. The Overview card itself is
        #: NOT throttled -- it always reflects the true current level
        #: instantly; only the supplementary toast is rate-limited, and
        #: every change is still written to the log regardless.
        _RISK_NOTIFICATION_COOLDOWN = timedelta(seconds=4)
        last_risk_notification_at: list[Optional[datetime]] = [None]

        def on_snapshot_changed(snapshot: ThreatSnapshot, region_states: dict) -> None:
            previous_level = getattr(on_snapshot_changed, "_previous_level", None)
            overview_view.update_snapshot(snapshot)
            overview_view.update_region_states(region_states)
            settings_view.set_source_status(snapshot.api_status)
            if (
                settings_holder[0].notifications_enabled
                and previous_level is not None
                and previous_level != snapshot.overall_risk
            ):
                logger.info(f"Рівень загрози змінено на: {snapshot.overall_risk.label_uk}")
                now = datetime.now()
                last_at = last_risk_notification_at[0]
                if last_at is None or (now - last_at) >= _RISK_NOTIFICATION_COOLDOWN:
                    _show_snackbar(page, f"Рівень загрози змінено: {snapshot.overall_risk.label_uk}")
                    last_risk_notification_at[0] = now
            on_snapshot_changed._previous_level = snapshot.overall_risk  # type: ignore[attr-defined]

        alert_service.set_listener(on_snapshot_changed)

        def on_region_alert_triggered(regions: list[Region]) -> None:
            """A watched region just went No Alert -> Air Raid Alert.

            Called at most once per API refresh (see
            AlertService._apply_api_payload), already deduplicated and
            already restricted to the watched set -- this handler's only
            job is to react, not to re-check either of those conditions.
            """
            names = ", ".join(region.value for region in regions)
            try:
                if settings_holder[0].notifications_enabled:
                    region_alert_haptic.vibrate()
                _show_snackbar(page, f"Повітряна тривога: {names}")
                logger.info(f"Сповіщення про тривогу для: {names}")
            except Exception as exc:  # noqa: BLE001 - never let a notification hiccup break polling
                logger.error(f"Не вдалося показати сповіщення про тривогу: {exc}")

        alert_service.set_region_alert_listener(on_region_alert_triggered)

        # --- Small shared helpers --------------------------------------------

        def _refresh_bell_icon() -> None:
            enabled = settings_holder[0].notifications_enabled
            bell_button.icon = (
                ft.Icons.NOTIFICATIONS_ROUNDED if enabled else ft.Icons.NOTIFICATIONS_OFF_ROUNDED
            )
            bell_button.icon_color = theme.ACCENT_BLUE if enabled else theme.TEXT_MUTED
            if page is not None:
                bell_button.update()

        #: Destroyed entries are kept far longer than active ones -- the
        #: "never delete history" requirement, balanced against a
        #: practical memory bound (same tradeoff as LoggerService's
        #: MAX_HISTORY_ENTRIES cap: generous enough to be "permanent" for
        #: any realistic session, not literally unbounded).
        _DESTROYED_RETENTION_SECONDS = 24 * 3600.0

        def _prune_expired_movements() -> bool:
            """Drop STILL-ACTIVE entries older than ``_MOVEMENT_TTL_SECONDS``,
            and DESTROYED entries older than ``_DESTROYED_RETENTION_SECONDS``.

            A destroyed entry is already permanently recorded in the Log
            tab's history (see ``logger.history`` calls below) by the time
            it would ever be pruned here -- this only bounds how long the
            in-memory list itself holds onto it, not whether it's
            recorded at all. Returns True if anything was actually
            removed, so callers can decide whether the tab needs a redraw.
            """
            now = datetime.now()
            active_cutoff = now - timedelta(seconds=_MOVEMENT_TTL_SECONDS)
            destroyed_cutoff = now - timedelta(seconds=_DESTROYED_RETENTION_SECONDS)
            before = len(movements)
            movements[:] = [
                m for m in movements
                if (m.destroyed_at is None and m.received_at >= active_cutoff)
                or (m.destroyed_at is not None and m.destroyed_at >= destroyed_cutoff)
            ]
            return len(movements) != before

        def _refresh_movement_views() -> None:
            """Push the current ACTIVE movements to both the Рух загроз
            map AND the Компас загроз page -- the single place both are
            kept in sync from the same real data, so no call site can
            update one and forget the other.

            ``movements`` itself keeps every entry, active or destroyed
            (see ``_prune_expired_movements``'s much longer retention for
            destroyed ones) -- a destroyed entry is filtered out of what
            the live map/compass render here, without ever being deleted
            from the underlying list, per "remove from the map, keep in
            history".
            """
            active_movements = [m for m in movements if m.destroyed_at is None]
            movement_view.set_movements(active_movements)
            compass_view.set_snapshot(
                build_compass_snapshot(
                    active_movements,
                    is_online=server_client.is_connected,
                    threat_level_label=alert_service.snapshot().overall_risk.label_uk,
                )
            )

        # --- Server (centralized backend) event handlers --------------------
        #
        # This is the ONLY source of live threat/movement data in this
        # app -- the mobile app no longer connects to Telegram directly
        # (see app/services/server_client.py's module docstring). These
        # handlers translate the server's already-processed JSON payload
        # into ThreatMovement objects and feed them into the `movements`
        # list / `_refresh_movement_views()` that drive the Рух загроз
        # map and Компас загроз page.

        def _server_movement_from_payload(payload: dict) -> Optional[ThreatMovement]:
            """Build a ThreatMovement from a server threat_new/threat_updated payload.

            Never touches Telegram credentials -- the payload contains
            only already-processed threat data (see server/services/
            threat_engine.py's _threat_to_payload), the same shape
            regardless of which server user or channel it came from.
            """
            try:
                threat_type = ThreatType(payload.get("type", "unknown"))
            except ValueError:
                threat_type = ThreatType.UNKNOWN

            origin_lat_lon = payload.get("origin_lat_lon")
            destination_lat_lon = payload.get("destination_lat_lon")
            origin_point = project_lat_lon(*origin_lat_lon) if origin_lat_lon else None
            destination_point = project_lat_lon(*destination_lat_lon) if destination_lat_lon else None

            origin_region_value = payload.get("origin_region")
            destination_region_value = payload.get("destination_region")
            try:
                origin_region = Region(origin_region_value) if origin_region_value else None
            except ValueError:
                origin_region = None
            try:
                destination_region = Region(destination_region_value) if destination_region_value else None
            except ValueError:
                destination_region = None

            try:
                first_seen_at = datetime.fromisoformat(payload["first_seen_at"])
                received_at = datetime.fromisoformat(payload["last_seen_at"])
            except (KeyError, ValueError):
                return None

            return ThreatMovement(
                id=payload["id"],
                threat_type=threat_type,
                channel_username=payload.get("channel", "сервер"),
                received_at=received_at,
                text=payload.get("text", ""),
                first_seen_at=first_seen_at,
                origin_name=payload.get("origin"),
                destination_name=payload.get("destination"),
                origin_point=origin_point,
                destination_point=destination_point,
                origin_region=origin_region,
                destination_region=destination_region,
                origin_settlement=payload.get("origin"),
                destination_settlement=payload.get("destination"),
                group_count=int(payload.get("group_count") or 1),
                status_label=payload.get("status_label"),
            )

        def _on_server_threat_event(event_type: str, payload: dict) -> None:
            """Called synchronously by ServerClient on a new WebSocket message."""
            try:
                movement_id = payload.get("id")
                if not movement_id:
                    return

                if event_type in ("threat_destroyed", "threat_expired"):
                    for existing in movements:
                        if existing.id == movement_id:
                            existing.destroyed_at = existing.received_at
                            if payload.get("status_label"):
                                existing.status_label = payload["status_label"]
                            break
                    _refresh_movement_views()
                    return

                movement = _server_movement_from_payload(payload)
                if movement is None:
                    return

                existing_index = next(
                    (i for i, m in enumerate(movements) if m.id == movement.id), None
                )
                if existing_index is not None:
                    previous = movements[existing_index]
                    if previous.origin_point is not None:
                        movement.position_history = (
                            previous.position_history + [previous.origin_point]
                        )[-MAX_POSITION_HISTORY:]
                    movements[existing_index] = movement
                else:
                    movements.append(movement)

                overview_view.add_message(
                    ChannelMessage(
                        channel_username=f"сервер · {movement.channel_username}",
                        text=movement.text or movement.short_description,
                        received_at=movement.received_at,
                        risk_contribution=0.0,
                    )
                )
                _refresh_movement_views()
                logger.info(f"Сервер: {event_type} -- {movement.short_description}")
            except Exception as exc:  # noqa: BLE001 - must be visible, never hidden
                logger.error(f"Помилка обробки події сервера: {type(exc).__name__}: {exc}")

        def _on_server_stats(stats: dict) -> None:
            overview_view.set_server_stats(stats)

        def _on_server_status_changed(connected: bool, message: str) -> None:
            settings = settings_holder[0]
            configured = bool(settings.server_url and settings.server_device_id and settings.server_token)
            overview_view.set_server_status(connected, configured)
            settings_view.set_server_status(connected, message)

        server_client.set_threat_event_listener(_on_server_threat_event)
        server_client.set_stats_listener(_on_server_stats)
        server_client.set_status_listener(_on_server_status_changed)

        # --- Async task implementations -------------------------------------

        async def _toggle_notifications() -> None:
            try:
                current = settings_holder[0]
                updated = dataclasses.replace(current, notifications_enabled=not current.notifications_enabled)
                settings_holder[0] = updated
                await storage.save_settings(updated)
                settings_view.set_settings(updated)
                _refresh_bell_icon()
                logger.info(
                    f"Сповіщення: {'увімкнено' if updated.notifications_enabled else 'вимкнено'}"
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Не вдалося змінити сповіщення: {exc}")

        async def _save_settings(new_settings: AppSettings, silent: bool = False) -> None:
            try:
                settings_holder[0] = new_settings
                interval_box.seconds = new_settings.update_interval_seconds
                await storage.save_settings(new_settings)
                _refresh_bell_icon()
                if not silent:
                    logger.info("Налаштування збережено.")
                    _show_snackbar(page, "Налаштування збережено.")
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
                alert_service.set_watched_regions(_watched_regions_from_settings(defaults))
                logger.info("Налаштування скинуто до типових.")
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Не вдалося скинути налаштування: {exc}")

        async def _persist_selected_regions(regions: set[Region]) -> None:
            """Save the oblast selection -- shared by two entry points.

            Called with the new full selection whether it was changed on
            the Рух загроз screen's chips OR Налаштування's new region
            checkboxes -- both edit the same
            ``AppSettings.watched_regions`` field/storage key, through
            this one function, so there is exactly one save path no
            matter which UI the user used. Pushes the result back to
            *both* views (each is a no-op if that view already shows this
            exact selection, e.g. the one that originated the change) and
            to ``AlertService``, which needs the same set to know which
            regions should trigger a sound notification.
            """
            try:
                current = settings_holder[0]
                watched_values = sorted(region.value for region in regions)
                updated = dataclasses.replace(current, watched_regions=watched_values)
                settings_holder[0] = updated
                await storage.save_settings(updated)
                settings_view.set_watched_regions(watched_values)
                movement_view.set_selected_regions(regions)
                alert_service.set_watched_regions(regions)
                logger.info(f"Обрані області (карта руху загроз / сповіщення): {watched_values or '—'}")
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Не вдалося зберегти обрані області: {exc}")

        async def _connect_server() -> None:
            """Connect to the centralized Air Alert Analyzer server.

            Mirrors ``_start_telegram``'s shape: runs for as long as the
            connection loop is alive (including through reconnects), and
            only returns once ``server_client.stop()`` was called or the
            credentials were rejected outright (see ServerAuthError in
            server_client.py, which stops the retry loop instead of
            hammering the server with doomed login attempts).
            """
            settings = settings_holder[0]
            logger.info(
                "Сервер: запуск підключення "
                f"(адреса={'вказано' if settings.server_url else 'ПОРОЖНЬО'}, "
                f"device_id={'вказано' if settings.server_device_id else 'ПОРОЖНЬО'})."
            )
            try:
                await server_client.start(
                    settings.server_url, settings.server_device_id, settings.server_token
                )
            except Exception as exc:  # noqa: BLE001 - must be visible, never hidden
                logger.error(f"Не вдалося підключитися до сервера: {type(exc).__name__}: {exc}")
                _show_snackbar(page, f"Не вдалося підключитися до сервера: {exc}")
            finally:
                configured = bool(
                    settings.server_url and settings.server_device_id and settings.server_token
                )
                overview_view.set_server_status(server_client.is_connected, configured)
                settings_view.set_server_status(server_client.is_connected)

        async def _disconnect_server() -> None:
            """Disconnect from the centralized server (credentials are kept, just not used)."""
            logger.info("Сервер: натиснуто кнопку 'Відключити'.")
            try:
                await server_client.stop()
                overview_view.set_server_status(False, configured=bool(settings_holder[0].server_url))
                settings_view.set_server_status(False, "відключено вручну")
                _show_snackbar(page, "Відключено від сервера.")
            except Exception as exc:  # noqa: BLE001 - must be visible, never hidden
                logger.error(f"Не вдалося відключитися від сервера: {type(exc).__name__}: {exc}")
                _show_snackbar(page, f"Не вдалося відключитися від сервера: {exc}")

        async def _tick_loop() -> None:
            """Background loop: periodic API refresh, risk decay, persistence."""
            last_flush = datetime.now()
            while True:
                try:
                    await asyncio.sleep(1)
                    elapsed = 1.0
                    overview_view.tick(datetime.now())
                    alert_service.decay_telegram_risk(elapsed)

                    # Real hardware compass heading, if a new reading
                    # arrived since the last tick (see compass_sensor.py
                    # -- the sensor callback runs on an Android-owned
                    # thread and hands off through a thread-safe queue;
                    # this is the single place that queue is drained).
                    if compass_sensor_service.drain_latest_heading() is not None:
                        compass_view.set_compass_diagnostics(compass_sensor_service.diagnostics)

                    seconds_since_flush = (datetime.now() - last_flush).total_seconds()
                    if seconds_since_flush >= interval_box.seconds:
                        await alert_service.refresh_from_api(settings_holder[0].alerts_api_token)
                        if _prune_expired_movements():
                            _refresh_movement_views()
                        await logger.flush()
                        last_flush = datetime.now()
                except Exception as exc:  # noqa: BLE001 - the tick loop must never die
                    logger.error(f"Помилка фонового оновлення: {exc}")

        async def _initialize() -> None:
            """Load persisted state, then start background services."""
            try:
                await logger.load_persisted()
                logger.info("Застосунок запущено.")

                loaded_settings = await storage.load_settings()
                settings_holder[0] = loaded_settings
                interval_box.seconds = loaded_settings.update_interval_seconds
                settings_view.set_settings(loaded_settings)
                _refresh_bell_icon()
                movement_view.set_selected_regions(_watched_regions_from_settings(loaded_settings))
                alert_service.set_watched_regions(_watched_regions_from_settings(loaded_settings))

                logger.info("alerts.in.ua: самодіагностика при старті...")
                diagnostic_steps = await alert_service.run_self_diagnostic(
                    loaded_settings.alerts_api_token
                )
                for passed, message in diagnostic_steps:
                    mark = "✓" if passed else "✗"
                    logger.info(f"alerts.in.ua: {mark} {message}")

                if loaded_settings.alerts_api_token:
                    await alert_service.refresh_from_api(loaded_settings.alerts_api_token)

                overview_view.update_snapshot(alert_service.snapshot())
                overview_view.update_region_states(alert_service.region_states)
                settings_view.set_source_status(alert_service.snapshot().api_status)

                server_configured = bool(
                    loaded_settings.server_url
                    and loaded_settings.server_device_id
                    and loaded_settings.server_token
                )
                overview_view.set_server_status(False, server_configured)
                settings_view.set_server_status(False)

                page.run_task(_tick_loop)

                if server_configured and loaded_settings.server_enabled:
                    page.run_task(_connect_server)
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


#: Shared dialog chrome (background/shape/title style) so every dialog in
#: the app -- region info, movement detail, the Telegram login prompts --
#: looks like one consistent product surface instead of Flet's unstyled
#: default white-ish AlertDialog. Passed as **kwargs to every AlertDialog
#: built below rather than duplicated per call site.
def _dialog_style() -> dict:
    return {
        "bgcolor": theme.SURFACE_ELEVATED,
        "shape": ft.RoundedRectangleBorder(radius=theme.RADIUS_LG),
        "title_padding": ft.padding.only(left=20, top=20, right=20, bottom=8),
        "content_padding": ft.padding.only(left=20, top=8, right=20, bottom=8),
        "actions_padding": ft.padding.only(left=12, right=12, bottom=12, top=4),
    }


def _dialog_title(text: str) -> ft.Text:
    """A consistently-styled dialog title (matches the card section headers)."""
    return ft.Text(text, size=16, weight=ft.FontWeight.W_700, color=theme.TEXT_PRIMARY)


def _dialog_close_button(page: ft.Page, dialog: ft.AlertDialog) -> ft.TextButton:
    """A consistently-styled 'Закрити' action, used by every read-only dialog."""
    return ft.TextButton(
        "Закрити",
        style=ft.ButtonStyle(color=theme.ACCENT_BLUE),
        on_click=lambda e: page.close(dialog),
    )


def _show_snackbar(page: ft.Page, message: str) -> None:
    """Show a short, auto-dismissing snackbar message, styled to match the app's theme."""
    page.open(
        ft.SnackBar(
            content=ft.Text(message, color=theme.TEXT_PRIMARY),
            bgcolor=theme.SURFACE_ELEVATED,
            behavior=ft.SnackBarBehavior.FLOATING,
            shape=ft.RoundedRectangleBorder(radius=theme.RADIUS_MD),
            margin=ft.margin.all(12),
            open=True,
        )
    )


def _show_region_info_dialog(page: ft.Page, region: Region, state: Optional[RegionState]) -> None:
    """Show a dialog with the current alert info for a tapped region."""
    is_active = bool(state and state.is_active)
    status_color = theme.THREAT_STATUS_ACTIVE_COLOR if is_active else theme.THREAT_STATUS_CLEAR_COLOR
    status_icon_asset = ICON_SIREN_ACTIVE if is_active else ICON_SIREN_CLEAR
    status_text = "Активна повітряна тривога" if is_active else "Тривоги немає"
    changed_text = (
        f"Змінено: {state.last_changed:%H:%M:%S}" if state is not None else ""
    )

    dialog = ft.AlertDialog(
        modal=True,
        title=_dialog_title(region.value),
        content=ft.Column(
            tight=True,
            spacing=8,
            controls=[
                ft.Row(
                    spacing=8,
                    controls=[
                        icon_glyph(status_icon_asset, status_color, size=20),
                        ft.Text(status_text, weight=ft.FontWeight.W_600, color=theme.TEXT_PRIMARY),
                    ],
                ),
                ft.Text(changed_text, size=12, color=theme.TEXT_SECONDARY),
            ],
        ),
        **_dialog_style(),
    )
    dialog.actions = [_dialog_close_button(page, dialog)]
    page.open(dialog)


def _show_district_info_dialog(page: ft.Page, region: Region, district_name: str, state: Optional[RegionState]) -> None:
    """Show a dialog with info for a tapped district ("raion").

    Ukraine's actual air-raid alert system operates at oblast granularity,
    not raion granularity -- there is no separate per-district alert
    signal anywhere in this app's data (``AlertService`` only ever tracks
    ``RegionState`` per oblast). So a district's alert status/risk
    level/start time shown here are its PARENT OBLAST's -- accurate to
    how alerts actually work (a district doesn't have its own siren),
    not a fabricated separate per-district value.
    """
    is_active = bool(state and state.is_active)
    status_color = theme.THREAT_STATUS_ACTIVE_COLOR if is_active else theme.THREAT_STATUS_CLEAR_COLOR
    status_icon_asset = ICON_SIREN_ACTIVE if is_active else ICON_SIREN_CLEAR
    status_text = "Повітряна тривога активна" if is_active else "Тривоги немає"

    info_rows: list[ft.Control] = [
        ft.Row(
            spacing=8,
            controls=[
                icon_glyph(status_icon_asset, status_color, size=20),
                ft.Text(status_text, weight=ft.FontWeight.W_600, color=theme.TEXT_PRIMARY),
            ],
        ),
    ]
    if state is not None and state.risk_level != RiskLevel.NONE:
        info_rows.append(
            ft.Text(f"Рівень ризику: {state.risk_level.label_uk}", size=12, color=theme.TEXT_SECONDARY)
        )
    if state is not None and is_active:
        info_rows.append(
            ft.Text(f"Тривога з: {state.last_changed:%H:%M:%S}", size=12, color=theme.TEXT_SECONDARY)
        )

    dialog = ft.AlertDialog(
        modal=True,
        title=_dialog_title(district_name),
        content=ft.Column(
            tight=True,
            spacing=8,
            controls=[
                ft.Text(region.value, size=12, weight=ft.FontWeight.W_600, color=theme.ACCENT_BLUE),
                *info_rows,
            ],
        ),
        **_dialog_style(),
    )
    dialog.actions = [_dialog_close_button(page, dialog)]
    page.open(dialog)


def _show_movement_dialog(page: ft.Page, movement: ThreatMovement) -> None:
    """Show a dialog with full details for one movement entry.

    Every row is built as (label, value) and only added when ``value``
    is truthy -- "never display empty fields", per spec. A settlement
    name gracefully falls back to its oblast name when the settlement
    itself didn't resolve (see ``origin``/``destination`` below), rather
    than the row just disappearing whenever only the oblast is known.
    """
    origin = movement.origin_settlement or (movement.origin_region.value if movement.origin_region else None)
    destination = movement.destination_settlement or (
        movement.destination_region.value if movement.destination_region else None
    )
    origin_oblast = movement.origin_region.value if movement.origin_region else None
    destination_oblast = movement.destination_region.value if movement.destination_region else None
    direction = movement.short_description if (movement.origin_name or movement.destination_name) else None
    status = (
        f"Оновлено {len(movement.position_history)} раз(и) з моменту першого повідомлення"
        if movement.position_history
        else "Перше повідомлення про ціль"
    )

    fields: list[tuple[str, Optional[str]]] = [
        ("Походження (населений пункт)", origin),
        ("Напрямок (населений пункт)", destination),
        ("Область походження", origin_oblast),
        ("Область напрямку", destination_oblast),
        ("Напрямок", direction),
        ("Виявлено", f"{movement.received_at:%H:%M:%S %d.%m.%Y}"),
        ("Кількість цілей", f"×{movement.group_count}" if movement.group_count > 1 else None),
        ("Статус", status),
        ("Канал", movement.channel_username),
    ]

    info_rows: list[ft.Control] = [
        ft.Row(
            spacing=8,
            controls=[
                icon_glyph(icon_for_movement(movement), color_for_movement(movement), size=22),
                ft.Text(movement.threat_type.label_uk, size=15, weight=ft.FontWeight.W_700, color=theme.ACCENT_BLUE),
            ],
        ),
        ft.Divider(color=theme.BORDER),
    ]
    for label, value in fields:
        if not value:
            continue
        info_rows.append(
            ft.Column(
                spacing=0,
                tight=True,
                controls=[
                    ft.Text(label, size=11, color=theme.TEXT_MUTED),
                    ft.Text(value, size=13, weight=ft.FontWeight.W_600, color=theme.TEXT_PRIMARY),
                ],
            )
        )

    if movement.has_direction:
        info_rows.append(
            ft.Text(
                "Пунктирна лінія за ціллю на мапі -- орієнтовне продовження "
                "напрямку, а не підтверджений прогноз.",
                size=11,
                italic=True,
                color=theme.TEXT_MUTED,
            )
        )
    info_rows.extend(
        [
            ft.Divider(color=theme.BORDER),
            ft.Text("Повний текст повідомлення:", size=12, color=theme.TEXT_MUTED),
            ft.Text(movement.text, size=13, color=theme.TEXT_PRIMARY, selectable=True),
        ]
    )

    dialog = ft.AlertDialog(
        modal=True,
        title=_dialog_title(movement.threat_type.label_uk),
        content=ft.Column(
            tight=True,
            spacing=8,
            scroll=ft.ScrollMode.AUTO,
            controls=info_rows,
        ),
        **_dialog_style(),
    )
    dialog.actions = [_dialog_close_button(page, dialog)]
    page.open(dialog)


if __name__ == "__main__":
    ft.app(target=main, assets_dir="assets")
