"""
Fully automatic connection to the Air Alert Analyzer centralized SERVER.

No IP address, port, Device ID, or token is ever typed by the user, and
none of it is hardcoded in the app -- see the flow below. This is the
ONLY thing this app knows about the server; it never touches Telegram
directly (see the module docstring history in this file's git blame /
the project's architecture docs -- Telegram integration lives entirely
server-side).

State machine, one loop that never needs a "Connect" button:

    DISCOVER  --(no reply)-->  DISCOVER (backoff, retry -- handles
                                "no server yet" AND "server's IP
                                changed" identically, since discovery
                                never trusts a previously-known address)
        |
        v (server found)
    have stored credentials? --NO--> REGISTER --> WAIT FOR APPROVAL
        |                                              |
        YES                                     (polls until approved
        |                                        or rejected; server
        v                                        going away mid-wait
    LOGIN                                         restarts at DISCOVER)
        |                                              |
        v                                              v (approved)
    POLL threats/stats forever  <----------------  LOGIN

Any network failure at any stage drops back to DISCOVER (backoff),
which is what makes this resilient to both "server temporarily off"
and "server's IP changed" without special-casing either -- the app
simply never assumes yesterday's address is still correct.

A 401 from /auth/login (device_id/token not recognized at all --
e.g. the account was deleted) clears the stored credentials and
restarts at REGISTER. A 403 (recognized but blocked/pending/rejected)
does NOT clear credentials -- if an administrator later re-enables the
device, it should reconnect as the same identity, not register a
second time.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable, Optional

import httpx

from app.services.logger_service import LoggerService
from app.services.server_discovery import DiscoveredServer, discover_server
from app.storage.device_store import DeviceCredentials, DeviceCredentialStore

_POLL_INTERVAL_SECONDS = 5.0
_REGISTRATION_POLL_INTERVAL_SECONDS = 4.0
_BASE_RETRY_DELAY_SECONDS = 3
_MAX_RETRY_DELAY_SECONDS = 60
_DISCOVERY_TIMEOUT_SECONDS = 3.0
_REQUEST_TIMEOUT_SECONDS = 10.0

#: (status_code, human message). status_code is one of: "discovering",
#: "waiting_approval", "connected", "disconnected", "rejected" -- the
#: UI (see app/ui/components/server_status_row.py) maps each to one of
#: the four icons the project's UI spec calls for (🟡🟠🟢🔴).
OnStatusChanged = Callable[[str, str], None]
OnThreatEvent = Callable[[str, dict], None]
OnStatsUpdate = Callable[[dict], None]


class _CredentialsInvalid(Exception):
    """Raised internally when the server no longer recognizes this device_id/token at all."""


@dataclass(slots=True)
class _LoginResult:
    access_token: str


class ServerClient:
    """Owns the entire discover -> register -> authenticate -> poll lifecycle."""

    def __init__(
        self,
        logger: LoggerService,
        credential_store: DeviceCredentialStore,
        poll_interval_seconds: float = _POLL_INTERVAL_SECONDS,
    ) -> None:
        self._logger = logger
        self._credential_store = credential_store
        self._poll_interval_seconds = poll_interval_seconds

        self._running = False
        self._start_in_progress = False
        self._connected = False
        self._last_status = "discovering"

        self._on_status_changed: Optional[OnStatusChanged] = None
        self._on_threat_event: Optional[OnThreatEvent] = None
        self._on_stats_update: Optional[OnStatsUpdate] = None

        #: id -> last-seen payload, used to diff consecutive polls into
        #: threat_new/threat_updated/threat_destroyed events.
        self._known_threats: dict[str, dict] = {}

        #: Surfaced in the optional "Додатково" (advanced/debug) section
        #: of Settings -- never required reading for normal use.
        self.discovered_server: Optional[DiscoveredServer] = None
        self._current_credentials: Optional[DeviceCredentials] = None
        #: Set once POST /auth/register succeeds, cleared only once the
        #: device is approved/rejected/no-longer-known -- NOT cleared on
        #: a transient network error while waiting. This is what makes a
        #: brief connectivity blip during the (potentially long) approval
        #: wait resume polling the SAME pending device instead of
        #: silently registering a brand-new one every time the
        #: connection hiccups (which would otherwise leave a growing
        #: trail of abandoned PENDING entries for what is, to the user,
        #: a single device that just wants to keep waiting).
        self._pending_device_id: Optional[str] = None

    @property
    def device_id(self) -> Optional[str]:
        """This device's own ID, once known -- for the optional "Додатково" panel only."""
        return self._current_credentials.device_id if self._current_credentials else None

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

    async def start(self, device_name: str = "") -> None:
        """Run the full automatic connection lifecycle until :meth:`stop`.

        Intended to be launched once as a background task
        (``page.run_task``) right after the app starts, unconditionally
        -- there is no "enabled" flag to check and no credentials the
        user needs to have entered first.
        """
        if self._start_in_progress:
            self._logger.warning("ServerClient: start() вже виконується -- повторний виклик проігноровано.")
            return
        self._start_in_progress = True
        self._running = True
        try:
            await self._run(device_name)
        finally:
            self._start_in_progress = False

    async def stop(self) -> None:
        self._running = False
        self._connected = False
        self._known_threats.clear()

    # --- main loop -----------------------------------------------------

    async def _run(self, device_name: str) -> None:
        delay = _BASE_RETRY_DELAY_SECONDS
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
            while self._running:
                self._connected = False
                self._set_status("discovering", "Пошук сервера...")
                server = await discover_server(timeout=_DISCOVERY_TIMEOUT_SECONDS)
                if server is None:
                    self._set_status("disconnected", "Сервер не знайдено. Повторний пошук...")
                    await self._sleep_backoff(delay)
                    delay = min(delay * 2, _MAX_RETRY_DELAY_SECONDS)
                    continue

                delay = _BASE_RETRY_DELAY_SECONDS  # found a server -- reset backoff
                self.discovered_server = server
                self._logger.info(f"Сервер знайдено: {server.ip}:{server.api_port}.")

                credentials = await self._credential_store.load()
                if credentials is None:
                    credentials = await self._register_and_wait(client, server, device_name)
                    if credentials is None:
                        if not self._running:
                            return
                        # Either rejected (terminal for this run -- status
                        # already set by _register_and_wait) or the server
                        # vanished mid-registration/mid-wait -- either way,
                        # back to discovery.
                        if self._last_status != "rejected":
                            await self._sleep_backoff(delay)
                            delay = min(delay * 2, _MAX_RETRY_DELAY_SECONDS)
                            continue
                        return
                self._current_credentials = credentials

                try:
                    await self._login_and_poll(client, server, credentials)
                except _CredentialsInvalid:
                    self._logger.warning("Сервер: облікові дані пристрою більше не дійсні -- повторна реєстрація.")
                    await self._credential_store.clear()
                    continue  # straight back to discovery -> registration, no backoff needed

                # _login_and_poll only returns normally after losing the
                # connection (network error, server restart, etc) -- go
                # back to discovery, which also naturally handles the
                # server having moved to a new IP in the meantime.
                if not self._running:
                    return
                await self._sleep_backoff(delay)
                delay = min(delay * 2, _MAX_RETRY_DELAY_SECONDS)

    async def _sleep_backoff(self, delay: float) -> None:
        # Slept in small slices so stop() takes effect quickly instead of
        # waiting out a potentially long backoff -- keeps shutdown snappy
        # without needing a separate cancellation mechanism.
        remaining = delay
        while remaining > 0 and self._running:
            step = min(1.0, remaining)
            await asyncio.sleep(step)
            remaining -= step

    # --- registration ----------------------------------------------------

    async def _register_and_wait(
        self, client: httpx.AsyncClient, server: DiscoveredServer, device_name: str
    ) -> Optional[DeviceCredentials]:
        """Ensure this device is registered, then poll until a decision.

        Returns the new credentials once approved (already saved to
        local storage), or None if the server was unreachable, the
        registration itself failed (e.g. server at its user limit), or
        the device was rejected (self._last_status will be "rejected").

        Reuses ``self._pending_device_id`` across calls -- see its
        docstring -- so a caller that lost the connection mid-wait and
        comes back here after rediscovering the server resumes polling
        the SAME device_id instead of registering a new one.
        """
        device_id = self._pending_device_id
        if device_id is None:
            try:
                response = await client.post(
                    f"{server.base_url}/auth/register", json={"device_name": device_name}
                )
                response.raise_for_status()
                device_id = response.json()["device_id"]
            except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError,
                    httpx.TransportError, KeyError):
                return None
            self._pending_device_id = device_id
            self._logger.info("Сервер: пристрій зареєстровано, очікування підтвердження адміністратора.")

        self._set_status("waiting_approval", "Очікування підтвердження сервера...")

        while self._running:
            await self._sleep_backoff(_REGISTRATION_POLL_INTERVAL_SECONDS)
            if not self._running:
                return None
            try:
                response = await client.get(
                    f"{server.base_url}/auth/register/status", params={"device_id": device_id}
                )
                response.raise_for_status()
                data = response.json()
            except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError, httpx.TransportError):
                # Transient -- caller falls back to discovery, but
                # self._pending_device_id stays set so the NEXT attempt
                # resumes waiting on this same registration instead of
                # creating another one.
                return None

            reg_status = data.get("status")
            if reg_status == "approved":
                token = data.get("token")
                if not token:
                    continue  # defensive -- shouldn't happen, just keep waiting
                credentials = DeviceCredentials(device_id=device_id, token=token)
                await self._credential_store.save(credentials)
                self._pending_device_id = None
                self._logger.info("Сервер: пристрій підтверджено адміністратором.")
                return credentials
            if reg_status == "rejected":
                self._pending_device_id = None
                self._logger.warning("Сервер: у підключенні пристрою відмовлено адміністратором.")
                self._set_status("rejected", "У доступі відмовлено адміністратором сервера.")
                return None
            if reg_status == "not_found":
                # Server-side data reset (e.g. reinstalled) since we
                # registered -- this device_id means nothing anymore,
                # clear it so the next attempt registers fresh instead
                # of polling a status that will always come back
                # "not_found".
                self._pending_device_id = None
                return None
            # "pending" (the normal case) or "blocked" -- keep waiting.

        return None

    # --- login + polling ---------------------------------------------------

    async def _login_and_poll(
        self, client: httpx.AsyncClient, server: DiscoveredServer, credentials: DeviceCredentials
    ) -> None:
        try:
            access_token = await self._login(client, server, credentials)
        except _CredentialsInvalid:
            raise  # device_id/token itself is no longer valid -- caller re-registers
        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError, httpx.TransportError) as exc:
            # Covers both "server unreachable" and "recognized but not
            # currently allowed" (403 -- blocked/pending/rejected): either
            # way, this is not a crash, just "not connected right now".
            # _login() already called _set_status() with the specific
            # reason before raising, for the 403 case.
            self._connected = False
            if not isinstance(exc, httpx.HTTPStatusError):
                self._set_status("disconnected", f"Сервер: немає з'єднання ({type(exc).__name__}).")
            return

        while self._running:
            headers = {"Authorization": f"Bearer {access_token}"}
            try:
                threats = await self._fetch_json(client, f"{server.base_url}/threats/active", headers)
                self._diff_and_emit_threats(threats)

                stats = await self._fetch_json(client, f"{server.base_url}/stats", headers)
                if self._on_stats_update is not None:
                    self._on_stats_update(stats)

                if not self._connected:
                    self._connected = True
                    self._set_status("connected", "Сервер підключено")

            except _CredentialsInvalid:
                raise  # device_id/token itself is no longer valid -- caller re-registers
            except _ReauthRequired:
                # Short-lived JWT expired -- same device, same server,
                # just get a fresh token and keep polling without a full
                # rediscovery/backoff cycle.
                try:
                    access_token = await self._login(client, server, credentials)
                except _CredentialsInvalid:
                    raise
                except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError, httpx.TransportError) as exc:
                    self._connected = False
                    if not isinstance(exc, httpx.HTTPStatusError):
                        self._set_status("disconnected", f"Сервер: немає з'єднання ({type(exc).__name__}).")
                    return
                continue
            except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError, httpx.TransportError) as exc:
                self._connected = False
                self._set_status("disconnected", f"Сервер: немає з'єднання ({type(exc).__name__}).")
                return  # back to discovery -- also covers the server having moved to a new IP

            await asyncio.sleep(self._poll_interval_seconds)

    async def _login(
        self, client: httpx.AsyncClient, server: DiscoveredServer, credentials: DeviceCredentials
    ) -> str:
        try:
            response = await client.post(
                f"{server.base_url}/auth/login",
                json={"device_id": credentials.device_id, "token": credentials.token},
            )
        except (httpx.ConnectError, httpx.TimeoutException, httpx.TransportError):
            raise  # let the caller's except clause classify this as "disconnected"

        if response.status_code == 401:
            raise _CredentialsInvalid("device_id/token not recognized by server")
        if response.status_code == 403:
            # Recognized but currently not allowed (blocked, pending,
            # rejected). Credentials stay valid for a future retry --
            # this is deliberately NOT _CredentialsInvalid.
            detail = ""
            try:
                detail = response.json().get("detail", "")
            except Exception:
                pass
            self._set_status("disconnected", detail or "Обліковий запис недоступний.")
            raise httpx.HTTPStatusError("403", request=response.request, response=response)

        response.raise_for_status()
        return response.json()["access_token"]

    async def _fetch_json(self, client: httpx.AsyncClient, url: str, headers: dict) -> object:
        response = await client.get(url, headers=headers)
        if response.status_code == 401:
            raise _ReauthRequired("access token expired")
        response.raise_for_status()
        return response.json()

    def _diff_and_emit_threats(self, threats: object) -> None:
        """Turn a GET /threats/active snapshot into new/updated/destroyed events."""
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

            for threat_id, previous in self._known_threats.items():
                if threat_id not in current:
                    destroyed_payload = dict(previous)
                    destroyed_payload["is_active"] = False
                    self._on_threat_event("threat_destroyed", destroyed_payload)

        self._known_threats = current

    def _set_status(self, status_code: str, message: str) -> None:
        self._last_status = status_code
        if self._on_status_changed is not None:
            self._on_status_changed(status_code, message)


class _ReauthRequired(Exception):
    """Raised internally when a data request's JWT has expired but the device_id/token are still fine."""
