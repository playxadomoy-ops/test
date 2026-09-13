"""
Fully automatic connection to the Air Alert Analyzer centralized SERVER,
over the Internet -- NOT dependent on the phone and server being on the
same Wi-Fi/LAN.

No IP address, port, Device ID, or token is ever typed by the user.
This is the ONLY thing this app knows about the server; it never
touches Telegram directly -- that integration lives entirely
server-side (see server/telegram/telegram_manager.py).

Architecture change from the previous LAN-discovery build: the app now
connects to a single, FIXED, known address (see app/config.py's
``get_server_base_url()`` -- a stable hostname/domain in production,
never a phone-guessed local IP). There is no more UDP broadcast, no
"find the server on this network" step, and therefore no dependency on
being on the same network as the server at all -- mobile data, a
different Wi-Fi, a different city, all work identically, because the
app always talks to the same address regardless of which network it's
currently on.

State machine, one loop that never needs a "Connect" button:

    CONNECT (fixed address) --(unreachable)--> backoff --> CONNECT (retry)
        |
        v (reachable)
    have stored credentials? --NO--> REGISTER --> WAIT FOR APPROVAL
        |                                              |
        YES                                     (polls until approved
        |                                        or rejected; server
        v                                        unreachable mid-wait
    LOGIN                                         just retries later)
        |                                              |
        v                                              v (approved)
    POLL threats/stats forever  <----------------  LOGIN

Any network failure at any stage (Wi-Fi drop, mobile-data handoff,
server restart, server temporarily unreachable) drops back to the
CONNECT step with exponential backoff, retrying the SAME fixed
address -- this is what makes network changes and temporary outages
transparent: unlike LAN discovery, there's no address to "lose" and
re-find, just one address to keep retrying.

A 401 from /auth/login (device_id/token not recognized at all --
e.g. the account was deleted) clears the stored credentials and
restarts at REGISTER. A 403 (recognized but blocked/pending/rejected)
does NOT clear credentials -- if an administrator later re-enables the
device, it should reconnect as the same identity, not register a
second time.
"""

from __future__ import annotations

import asyncio
from typing import Callable, Optional

import httpx

from app.config import get_server_base_url
from app.services.logger_service import LoggerService
from app.storage.device_store import DeviceCredentials, DeviceCredentialStore

_POLL_INTERVAL_SECONDS = 5.0
_REGISTRATION_POLL_INTERVAL_SECONDS = 4.0
_BASE_RETRY_DELAY_SECONDS = 3
_MAX_RETRY_DELAY_SECONDS = 60
_REQUEST_TIMEOUT_SECONDS = 10.0

#: (status_code, human message). status_code is one of: "connecting",
#: "waiting_approval", "connected", "unreachable", "reconnecting",
#: "rejected" -- the UI (see app/ui/components/server_status_row.py)
#: maps each to an icon and the exact wording requested for this
#: architecture: "Підключення...", "Сервер недоступний",
#: "Повторне підключення...", "🟢 Підключено".
OnStatusChanged = Callable[[str, str], None]
OnThreatEvent = Callable[[str, dict], None]
OnStatsUpdate = Callable[[dict], None]


class _CredentialsInvalid(Exception):
    """Raised internally when the server no longer recognizes this device_id/token at all."""


class _ReauthRequired(Exception):
    """Raised internally when a data request's JWT has expired but the device_id/token are still fine."""


def _normalize_base_url(raw: str) -> str:
    """Accept "https://api.example.com" or "https://api.example.com/" alike."""
    raw = raw.strip().rstrip("/")
    if "://" not in raw:
        raw = f"https://{raw}"
    return raw


class ServerClient:
    """Owns the entire connect -> register -> authenticate -> poll lifecycle."""

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
        self._last_status = "connecting"
        #: Distinguishes the very first connection attempt ("Підключення...")
        #: from subsequent automatic retries ("Повторне підключення...") --
        #: purely a wording choice, both mean the same thing structurally.
        self._ever_connected_once = False

        self._on_status_changed: Optional[OnStatusChanged] = None
        self._on_threat_event: Optional[OnThreatEvent] = None
        self._on_stats_update: Optional[OnStatsUpdate] = None

        #: id -> last-seen payload, used to diff consecutive polls into
        #: threat_new/threat_updated/threat_destroyed events.
        self._known_threats: dict[str, dict] = {}

        #: The fixed server address this app is configured to use --
        #: surfaced read-only in the optional "Додатково" panel of
        #: Settings, never edited there.
        self.server_base_url: str = _normalize_base_url(get_server_base_url())
        self._current_credentials: Optional[DeviceCredentials] = None
        #: Set once POST /auth/register succeeds, cleared only once the
        #: device is approved/rejected/no-longer-known -- NOT cleared on
        #: a transient network error while waiting. This is what makes a
        #: brief connectivity blip during the (potentially long) approval
        #: wait resume polling the SAME pending device instead of
        #: silently registering a brand-new one every time the
        #: connection hiccups.
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
                self._set_status(
                    "connecting" if not self._ever_connected_once else "reconnecting",
                    "Підключення..." if not self._ever_connected_once else "Повторне підключення...",
                )

                credentials = await self._credential_store.load()
                if credentials is None:
                    credentials = await self._register_and_wait(client, device_name)
                    if credentials is None:
                        if not self._running:
                            return
                        # Either rejected (terminal for this run -- status
                        # already set by _register_and_wait) or the server
                        # was unreachable mid-registration/mid-wait --
                        # either way, back to the top to retry the SAME
                        # fixed address after a backoff.
                        if self._last_status != "rejected":
                            await self._sleep_backoff(delay)
                            delay = min(delay * 2, _MAX_RETRY_DELAY_SECONDS)
                            continue
                        return
                self._current_credentials = credentials

                try:
                    await self._login_and_poll(client, credentials)
                except _CredentialsInvalid:
                    self._logger.warning("Сервер: облікові дані пристрою більше не дійсні -- повторна реєстрація.")
                    await self._credential_store.clear()
                    continue  # straight back to the top -> registration, no backoff needed

                # _login_and_poll only returns normally after losing the
                # connection (network error, server restart, phone
                # switched networks, etc) -- retry the same fixed
                # address after a backoff; there is no "different
                # address to try" the way LAN discovery used to imply.
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
        self, client: httpx.AsyncClient, device_name: str
    ) -> Optional[DeviceCredentials]:
        """Ensure this device is registered, then poll until a decision.

        Returns the new credentials once approved (already saved to
        local storage), or None if the server was unreachable, the
        registration itself failed (e.g. server at its user limit), or
        the device was rejected (self._last_status will be "rejected").

        Reuses ``self._pending_device_id`` across calls -- see its
        docstring -- so a caller that lost the connection mid-wait
        resumes polling the SAME device_id instead of registering a new
        one once connectivity returns.
        """
        device_id = self._pending_device_id
        if device_id is None:
            try:
                response = await client.post(
                    f"{self.server_base_url}/auth/register", json={"device_name": device_name}
                )
                response.raise_for_status()
                device_id = response.json()["device_id"]
            except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError,
                    httpx.TransportError, KeyError):
                self._set_status("unreachable", "Сервер недоступний")
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
                    f"{self.server_base_url}/auth/register/status", params={"device_id": device_id}
                )
                response.raise_for_status()
                data = response.json()
            except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError, httpx.TransportError):
                # Transient -- caller falls back to a retry after backoff,
                # but self._pending_device_id stays set so the NEXT
                # attempt resumes waiting on this same registration
                # instead of creating another one.
                self._set_status("unreachable", "Сервер недоступний")
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

    async def _login_and_poll(self, client: httpx.AsyncClient, credentials: DeviceCredentials) -> None:
        try:
            access_token = await self._login(client, credentials)
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
                self._set_status("unreachable", "Сервер недоступний")
            return

        while self._running:
            headers = {"Authorization": f"Bearer {access_token}"}
            try:
                threats = await self._fetch_json(client, f"{self.server_base_url}/threats/active", headers)
                self._diff_and_emit_threats(threats)

                stats = await self._fetch_json(client, f"{self.server_base_url}/stats", headers)
                if self._on_stats_update is not None:
                    self._on_stats_update(stats)

                if not self._connected:
                    self._connected = True
                    self._ever_connected_once = True
                    self._set_status("connected", "Підключено")

            except _CredentialsInvalid:
                raise  # device_id/token itself is no longer valid -- caller re-registers
            except _ReauthRequired:
                # Short-lived JWT expired -- same device, same server,
                # just get a fresh token and keep polling without a full
                # reconnect/backoff cycle.
                try:
                    access_token = await self._login(client, credentials)
                except _CredentialsInvalid:
                    raise
                except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError,
                        httpx.TransportError) as exc:
                    self._connected = False
                    if not isinstance(exc, httpx.HTTPStatusError):
                        self._set_status("unreachable", "Сервер недоступний")
                    return
                continue
            except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError, httpx.TransportError) as exc:
                self._connected = False
                self._set_status("unreachable", "Сервер недоступний")
                return  # back to the top of _run() -- retries the same fixed address after backoff

            await asyncio.sleep(self._poll_interval_seconds)

    async def _login(self, client: httpx.AsyncClient, credentials: DeviceCredentials) -> str:
        try:
            response = await client.post(
                f"{self.server_base_url}/auth/login",
                json={"device_id": credentials.device_id, "token": credentials.token},
            )
        except (httpx.ConnectError, httpx.TimeoutException, httpx.TransportError):
            raise  # let the caller's except clause classify this as "unreachable"

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
            self._set_status("unreachable", detail or "Обліковий запис недоступний.")
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
