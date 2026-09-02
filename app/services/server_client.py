"""
Client for the Air Alert Analyzer centralized SERVER (the separate
Python/FastAPI backend -- see the `server/` project). This is a second,
independent data source alongside the existing direct-Telethon
monitoring in :mod:`app.telegram.telegram_service`: the app can use
either or both at the same time, exactly like the existing
alerts.in.ua-vs-Telegram-parsing precedence already implemented in
``AlertService``.

Architecture (mirrors ``TelegramService`` deliberately, since that is
the existing app's established pattern for "own connect/reconnect loop,
callback-driven, launched via ``page.run_task``"):

    REST  POST /auth/login (device_id, token) -> short-lived JWT
    WS    GET  /ws?token=<jwt>                -> live event stream
              -> {"event": "threat_new"|"threat_updated"|"threat_destroyed"
                            |"stats_update", "data": {...}}

Security: this module only ever sends the device_id/token the operator
entered in Settings -> Сервер, and only ever reads back a JWT + the
already-processed JSON payloads described above. It has no knowledge of
-- and the server payloads never include -- the Telegram API ID/HASH,
phone number, 2FA password, or session string; those remain entirely
server-side (see server/telegram/telegram_manager.py) and are never
part of any response this client parses.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx
import websockets
from websockets.exceptions import WebSocketException

from app.services.logger_service import LoggerService

_RECONNECT_BASE_DELAY_SECONDS = 5
_RECONNECT_MAX_DELAY_SECONDS = 60
_REQUEST_TIMEOUT_SECONDS = 10.0

OnStatusChanged = Callable[[bool, str], None]           # (connected, message)
OnThreatEvent = Callable[[str, dict], None]              # (event_type, payload)
OnStatsUpdate = Callable[[dict], None]


@dataclass(slots=True)
class ServerCredentials:
    """What's needed to reach the server -- nothing Telegram-related."""

    base_url: str
    device_id: str
    token: str


def _normalize_base_url(raw: str) -> str:
    """Accept "ip:port", "http://ip:port", or "http://ip:port/" alike."""
    raw = raw.strip().rstrip("/")
    if not raw:
        return raw
    if "://" not in raw:
        raw = f"http://{raw}"
    return raw


def _to_ws_url(http_base_url: str, token: str) -> str:
    parts = urlsplit(http_base_url)
    ws_scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((ws_scheme, parts.netloc, "/ws", f"token={token}", ""))


class ServerClient:
    """Owns the connection to the Air Alert Analyzer server for this device."""

    def __init__(self, logger: LoggerService) -> None:
        self._logger = logger
        self._running = False
        self._start_in_progress = False
        self._connected = False
        self._websocket: Optional["websockets.WebSocketClientProtocol"] = None

        self._on_status_changed: Optional[OnStatusChanged] = None
        self._on_threat_event: Optional[OnThreatEvent] = None
        self._on_stats_update: Optional[OnStatsUpdate] = None

    # --- listener registration --------------------------------------------

    def set_status_listener(self, listener: Optional[OnStatusChanged]) -> None:
        self._on_status_changed = listener

    def set_threat_event_listener(self, listener: Optional[OnThreatEvent]) -> None:
        self._on_threat_event = listener

    def set_stats_listener(self, listener: Optional[OnStatsUpdate]) -> None:
        self._on_stats_update = listener

    @property
    def is_connected(self) -> bool:
        return self._connected

    # --- lifecycle -----------------------------------------------------

    async def start(self, base_url: str, device_id: str, token: str) -> None:
        """Connect to the server and keep the connection alive until :meth:`stop`.

        Intended to be launched as a background task (``page.run_task``),
        same as ``TelegramService.start``. Reconnects automatically with
        backoff on network loss or the server being temporarily offline.
        """
        normalized_url = _normalize_base_url(base_url)
        if not normalized_url or not device_id.strip() or not token.strip():
            message = "Сервер не запущено: не вказано адресу / Device ID / Token у налаштуваннях."
            self._logger.warning(message)
            self._set_status(False, message)
            return

        if self._start_in_progress:
            self._logger.warning("ServerClient: start() вже виконується -- повторний виклик проігноровано.")
            return

        self._start_in_progress = True
        try:
            await self._run_connection_loop(ServerCredentials(normalized_url, device_id.strip(), token.strip()))
        finally:
            self._start_in_progress = False

    async def stop(self) -> None:
        """Stop the connection loop and disconnect cleanly."""
        self._running = False
        await self._safe_close()
        self._set_status(False, "Сервер: відключено вручну.")

    # --- connection loop -------------------------------------------------

    async def _run_connection_loop(self, creds: ServerCredentials) -> None:
        self._running = True
        delay = _RECONNECT_BASE_DELAY_SECONDS
        connected_once = False

        while self._running:
            try:
                access_token = await self._login(creds)
                ws_url = _to_ws_url(creds.base_url, access_token)

                async with websockets.connect(ws_url, open_timeout=_REQUEST_TIMEOUT_SECONDS) as websocket:
                    self._websocket = websocket
                    self._connected = True
                    connected_once = True
                    delay = _RECONNECT_BASE_DELAY_SECONDS
                    self._logger.info("Сервер: підключено, отримання даних у реальному часі.")
                    self._set_status(True, "Підключено до сервера.")

                    async for raw_message in websocket:
                        self._handle_incoming_message(raw_message)

                # Loop exits normally when the server closes the socket.
                if self._running:
                    self._logger.warning(
                        f"Сервер: з'єднання втрачено, повторна спроба через {delay}с."
                    )

            except ServerAuthError as exc:
                # Bad/blocked credentials will never succeed on retry --
                # stop hammering the server and surface it clearly.
                message = f"Сервер: помилка авторизації -- {exc}"
                self._logger.error(message)
                self._set_status(False, message)
                self._running = False
            except (httpx.ConnectError, httpx.TimeoutException, OSError,
                     WebSocketException, ConnectionError, asyncio.TimeoutError) as exc:
                message = f"Сервер: немає з'єднання ({type(exc).__name__}: {exc})."
                self._logger.warning(message)
                self._set_status(False, message)
            except Exception as exc:  # noqa: BLE001 - must be visible, never hidden
                message = f"Сервер: неочікувана помилка -- {type(exc).__name__}: {exc}"
                self._logger.error(message)
                self._set_status(False, message)
            finally:
                self._connected = False
                self._websocket = None

            if self._running:
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RECONNECT_MAX_DELAY_SECONDS)

        if connected_once and not self._running:
            self._logger.info("Сервер: цикл з'єднання зупинено.")

    async def _login(self, creds: ServerCredentials) -> str:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
            try:
                response = await client.post(
                    f"{creds.base_url}/auth/login",
                    json={"device_id": creds.device_id, "token": creds.token},
                )
            except (httpx.ConnectError, httpx.TimeoutException):
                raise
            if response.status_code == 401:
                raise ServerAuthError("невірний Device ID або Token.")
            if response.status_code == 403:
                raise ServerAuthError("обліковий запис заблоковано адміністратором.")
            response.raise_for_status()
            data = response.json()
            return data["access_token"]

    def _handle_incoming_message(self, raw_message: str | bytes) -> None:
        try:
            envelope = json.loads(raw_message)
            event_type = envelope.get("event")
            payload = envelope.get("data", {})
        except (json.JSONDecodeError, AttributeError) as exc:
            self._logger.warning(f"Сервер: некоректне повідомлення WebSocket ({exc}).")
            return

        if event_type == "stats_update":
            if self._on_stats_update is not None:
                self._on_stats_update(payload)
        elif event_type in ("threat_new", "threat_updated", "threat_destroyed", "threat_expired"):
            if self._on_threat_event is not None:
                self._on_threat_event(event_type, payload)
        # Unknown event types are ignored rather than raising -- keeps this
        # client forward-compatible with a server that later adds new
        # event kinds the app doesn't yet know how to render.

    def _set_status(self, connected: bool, message: str) -> None:
        if self._on_status_changed is not None:
            self._on_status_changed(connected, message)

    async def _safe_close(self) -> None:
        if self._websocket is not None:
            try:
                await self._websocket.close()
            except Exception:  # noqa: BLE001 - must never raise out of cleanup
                pass
            self._websocket = None
        self._connected = False


class ServerAuthError(Exception):
    """Raised when the server rejects the configured device_id/token."""
