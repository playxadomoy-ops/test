"""
Client for the Air Alert Analyzer centralized SERVER (the separate
Python/FastAPI backend -- see the `server/` project).

REST-only, polling-based -- deliberately NOT using the `websockets`
package. That package isn't available in the Android Flet runtime this
app ships on (see the ModuleNotFoundError this replaced), and the
project's own communication layer was always plain HTTP (httpx is
already a dependency for the alerts.in.ua polling in AlertService), so
polling is "the communication method already intended for the project"
applied to the server too, just on a regular interval instead of a
long-lived socket.

Architecture: a single background loop (launched via ``page.run_task``,
same as every other background task in this app) that, every
``poll_interval_seconds``:
    1. Logs in (POST /auth/login) if not already holding a valid token.
    2. Fetches GET /threats/active and diffs it against what was seen on
       the previous poll, to synthesize the same threat_new/
       threat_updated/threat_destroyed events the old WebSocket push
       used to deliver -- so every caller of this client's listeners
       needed ZERO changes.
    3. Fetches GET /stats and reports it via the stats listener.
    4. On any network failure, reports "disconnected" and retries with
       backoff, then resumes exactly where it left off.

Security: this module only ever sends the device_id/token the operator
entered in Settings -> Сервер, and only ever reads back a JWT + the
already-processed JSON the endpoints above return. It has no knowledge
of -- and the server's responses never include -- the Telegram API
ID/HASH, phone number, 2FA password, or session string; Telegram
integration lives entirely in the separate server application now (see
server/telegram/telegram_manager.py), never in this mobile app.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable, Optional

import httpx

from app.services.logger_service import LoggerService

_POLL_INTERVAL_SECONDS = 5.0
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


class ServerAuthError(Exception):
    """Raised when the server rejects the configured device_id/token."""


class ServerClient:
    """Owns the polling connection to the Air Alert Analyzer server for this device."""

    def __init__(self, logger: LoggerService, poll_interval_seconds: float = _POLL_INTERVAL_SECONDS) -> None:
        self._logger = logger
        self._poll_interval_seconds = poll_interval_seconds
        self._running = False
        self._start_in_progress = False
        self._connected = False

        self._on_status_changed: Optional[OnStatusChanged] = None
        self._on_threat_event: Optional[OnThreatEvent] = None
        self._on_stats_update: Optional[OnStatsUpdate] = None

        #: id -> last-seen payload, used to diff consecutive polls into
        #: threat_new/threat_updated/threat_destroyed events.
        self._known_threats: dict[str, dict] = {}

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
        """Poll the server and keep polling until :meth:`stop`.

        Intended to be launched as a background task (``page.run_task``),
        same as every other long-running service in this app. Reconnects
        automatically with backoff on network loss or the server being
        temporarily offline.
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
            await self._run_poll_loop(ServerCredentials(normalized_url, device_id.strip(), token.strip()))
        finally:
            self._start_in_progress = False

    async def stop(self) -> None:
        """Stop the polling loop."""
        self._running = False
        self._connected = False
        self._known_threats.clear()
        self._set_status(False, "Сервер: відключено вручну.")

    # --- polling loop -----------------------------------------------------

    async def _run_poll_loop(self, creds: ServerCredentials) -> None:
        self._running = True
        delay = _RECONNECT_BASE_DELAY_SECONDS
        access_token: Optional[str] = None

        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
            while self._running:
                try:
                    if access_token is None:
                        access_token = await self._login(client, creds)
                        self._logger.info("Сервер: авторизація успішна, отримання даних.")

                    headers = {"Authorization": f"Bearer {access_token}"}

                    threats = await self._fetch_json(client, f"{creds.base_url}/threats/active", headers)
                    self._diff_and_emit_threats(threats)

                    stats = await self._fetch_json(client, f"{creds.base_url}/stats", headers)
                    if self._on_stats_update is not None:
                        self._on_stats_update(stats)

                    if not self._connected:
                        self._connected = True
                        self._set_status(True, "Підключено до сервера.")
                    delay = _RECONNECT_BASE_DELAY_SECONDS

                except ServerAuthError as exc:
                    # Bad/blocked credentials will never succeed on retry --
                    # stop hammering the server and surface it clearly.
                    message = f"Сервер: помилка авторизації -- {exc}"
                    self._logger.error(message)
                    self._set_status(False, message)
                    self._running = False
                    break
                except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError,
                        httpx.TransportError) as exc:
                    message = f"Сервер: немає з'єднання ({type(exc).__name__})."
                    self._logger.warning(message)
                    self._connected = False
                    self._set_status(False, message)
                    access_token = None  # re-login once connectivity returns, in case the token expired meanwhile
                except Exception as exc:  # noqa: BLE001 - must be visible, never hidden
                    message = f"Сервер: неочікувана помилка -- {type(exc).__name__}: {exc}"
                    self._logger.error(message)
                    self._connected = False
                    self._set_status(False, message)

                if not self._running:
                    break

                wait_seconds = self._poll_interval_seconds if self._connected else delay
                await asyncio.sleep(wait_seconds)
                if not self._connected:
                    delay = min(delay * 2, _RECONNECT_MAX_DELAY_SECONDS)

        self._connected = False
        self._logger.info("Сервер: цикл опитування зупинено.")

    async def _login(self, client: httpx.AsyncClient, creds: ServerCredentials) -> str:
        response = await client.post(
            f"{creds.base_url}/auth/login",
            json={"device_id": creds.device_id, "token": creds.token},
        )
        if response.status_code == 401:
            raise ServerAuthError("невірний Device ID або Token.")
        if response.status_code == 403:
            raise ServerAuthError("обліковий запис заблоковано адміністратором.")
        response.raise_for_status()
        data = response.json()
        return data["access_token"]

    async def _fetch_json(self, client: httpx.AsyncClient, url: str, headers: dict) -> object:
        response = await client.get(url, headers=headers)
        if response.status_code == 401:
            raise ServerAuthError("токен більше не дійсний.")
        response.raise_for_status()
        return response.json()

    def _diff_and_emit_threats(self, threats: object) -> None:
        """Turn a GET /threats/active snapshot into new/updated/destroyed events.

        The server's WebSocket push (used by the desktop admin app) sends
        these as discrete events; polling only gets a snapshot, so this
        reconstructs the same event stream by comparing against what was
        seen on the previous poll -- every downstream listener (built
        against the old WebSocket client) keeps working unchanged.
        """
        if not isinstance(threats, list):
            return

        current: dict[str, dict] = {}
        for payload in threats:
            threat_id = payload.get("id") if isinstance(payload, dict) else None
            if threat_id:
                current[threat_id] = payload

        if self._on_threat_event is not None:
            for threat_id, payload in current.items():
                previous = self._known_threats.get(threat_id)
                if previous is None:
                    self._on_threat_event("threat_new", payload)
                elif previous.get("last_seen_at") != payload.get("last_seen_at") or previous != payload:
                    self._on_threat_event("threat_updated", payload)

            # Anything that was active last poll but is missing now was
            # either destroyed or expired server-side -- either way it
            # should come off this device's map too.
            for threat_id, previous in self._known_threats.items():
                if threat_id not in current:
                    destroyed_payload = dict(previous)
                    destroyed_payload["is_active"] = False
                    self._on_threat_event("threat_destroyed", destroyed_payload)

        self._known_threats = current

    def _set_status(self, connected: bool, message: str) -> None:
        if self._on_status_changed is not None:
            self._on_status_changed(connected, message)
