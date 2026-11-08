"""
Automatic connection to the user's OWN Air Alert Analyzer server -- over the Internet or the LAN.

The phone and the Windows PC do NOT need to share a network. How:

* PAIRING. Every server installation has a persistent ``server_id`` (shown to its owner as a code
  ``XXXX-XXXX-XXXX``). A phone is paired with exactly one server_id: automatically when exactly one server
  answers on the LAN (nothing to type), otherwise by typing the code ONCE. The code only LOCATES a server --
  the server's administrator must still approve this phone (unchanged registration/approval system).
* REACHING IT. Each (re)connection attempt picks a route, verifying the server's identity (``GET /identity``
  must return the paired server_id) so a phone never talks to somebody else's server:
    1. LAN direct   -- if UDP discovery finds the paired server right now (optimization only);
    2. Internet     -- ``<gateway>/s/<server_id>/...``: the gateway relays to that PC over the PC's own
                       OUTBOUND tunnel, so home routers, NAT, CGNAT, changing IPs and port forwarding are
                       irrelevant. Works on mobile data and any other Wi-Fi.
* IDENTITY. The device_id and secret are generated ON THE PHONE and persisted BEFORE registering, so
  retries are idempotent and killing the app while waiting for approval resumes the same registration
  (no duplicate devices). The server stores only a bcrypt hash.

State machine (never needs a Connect button):

    PAIR --> RESOLVE ROUTE --(none reachable)--> backoff --> RESOLVE ROUTE
                |
                v
    approved creds? --NO--> REGISTER (idempotent) --> WAIT FOR APPROVAL --(approved)--+
                |                                                                       |
                YES <-------------------------------------------------------------------+
                v
              LOGIN --> POLL threats/stats; on any failure / network change -> RESOLVE ROUTE again

A 401 from /auth/login clears credentials (a new identity is registered); a 403 (blocked/pending/rejected)
keeps them. While on the Internet route the app re-checks the LAN periodically and switches to it if found.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Callable, Optional

import httpx

from app.config import GATEWAY_CONFIG, get_gateway_base_url
from app.provisioning import Provisioning
from app.services.logger_service import LoggerService
from app.services import pow as pow_solver
from app.services.pairing import lan_discover, normalize_server_code
from app.services.secure_channel import SecureApiClient, SecureChannelError, ServerIdentityError
from app.storage.device_store import DeviceCredentials, DeviceCredentialStore, PairedServerStore

_POLL_INTERVAL_SECONDS = 5.0
_REGISTRATION_POLL_INTERVAL_SECONDS = 4.0
_BASE_RETRY_DELAY_SECONDS = 3
_MAX_RETRY_DELAY_SECONDS = 60
_REQUEST_TIMEOUT_SECONDS = 10.0
_PROBE_TIMEOUT_SECONDS = 5.0
_LAN_UPGRADE_CHECK_SECONDS = 60.0
_GATEWAY_ERROR_HEADER = "x-airalert-gateway"

#: (status_code, human message). status_code is one of: "initializing", "registering", "connecting", "connected",
#: "reconnecting", "server_offline", "gateway_unavailable", "rejected" (the user-facing set), plus "waiting_approval"
#: (server policy requires manual approval), "pairing_required"/"gateway_unconfigured" (unprovisioned dev builds) and
#: "unreachable" (legacy/LAN-only).
OnStatusChanged = Callable[[str, str], None]
OnThreatEvent = Callable[[str, dict], None]
OnStatsUpdate = Callable[[dict], None]
DiscoverFn = Callable[[], list]

_TRANSPORT_ERRORS = (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError, httpx.TransportError,
                     SecureChannelError)


def _is_transient_403(response) -> bool:
    """A 403 caused by an expired/replayed/over-capacity CHALLENGE is retryable with a fresh one; any other 403 is final."""
    try:
        detail = str(response.json().get("detail", ""))
    except ValueError:
        return False
    return any(f"({reason})" in detail for reason in ("expired", "replayed", "busy", "weak"))


#: Why this device needs MANUAL approval (server-reported, non-secret). Shown so nobody has to guess.
_WAITING_MESSAGES = {
    "no_enroll_key": "Очікування підтвердження: ця збірка застосунку не містить ключа автопідключення.",
    "auto_enroll_disabled": "Очікування підтвердження: на сервері вимкнено автоматичне підтвердження.",
    "rate_brake": "Очікування підтвердження: забагато нових пристроїв за годину.",
    "no_proof_of_work": "Очікування підтвердження: сервер не отримав підтвердження роботи від застосунку.",
}


class _CredentialsInvalid(Exception):
    """The server no longer recognizes this device_id/secret at all."""


class _ReauthRequired(Exception):
    """A data request's JWT expired but the device credentials are still fine."""


class _SwitchRoute(Exception):
    """Internal: leave the poll loop immediately and re-resolve the route (e.g. LAN became available)."""


def _is_placeholder(url: str) -> bool:
    host = httpx.URL(url).host.lower() if url else ""
    return not host or host.endswith(".example") or host == "example"


class ServerClient:
    """Owns pairing -> route resolution -> register -> authenticate -> poll, forever, with no user input."""

    def __init__(
        self,
        logger: LoggerService,
        credential_store: DeviceCredentialStore,
        paired_store: PairedServerStore,
        poll_interval_seconds: float = _POLL_INTERVAL_SECONDS,
        gateway_url: Optional[str] = None,
        discover: Optional[DiscoverFn] = None,
        lan_upgrade_check_seconds: float = _LAN_UPGRADE_CHECK_SECONDS,
        base_retry_delay: float = _BASE_RETRY_DELAY_SECONDS,
        registration_poll_seconds: float = _REGISTRATION_POLL_INTERVAL_SECONDS,
        provisioning: Optional[Provisioning] = None,
        attestation_provider=None,
        discovery_grace_seconds: float = 8.0,
    ) -> None:
        self._logger = logger
        self._credential_store = credential_store
        self._paired_store = paired_store
        self._poll_interval_seconds = poll_interval_seconds
        self._prov = provisioning
        #: How long an UNPROVISIONED build keeps trying LAN auto-pairing before it asks a human for a code.
        self._discovery_grace_seconds = discovery_grace_seconds
        #: Optional async callable (device_id, challenge) -> Play Integrity token or None. NOT available in the current
        #: Flet 0.28.3 app (no native bridge); present so the protocol/hook exists for a future build.
        self._attestation_provider = attestation_provider
        default_gw = provisioning.gateway_url if provisioning else get_gateway_base_url()
        self._gateway_url = (gateway_url if gateway_url is not None else default_gw).rstrip("/")
        self._secure_cache: dict[str, SecureApiClient] = {}
        self._discover: DiscoverFn = discover or (lambda: lan_discover(port=GATEWAY_CONFIG.LAN_DISCOVERY_PORT))
        self._lan_upgrade_check_seconds = lan_upgrade_check_seconds
        self._base_retry_delay = base_retry_delay
        self._registration_poll_seconds = registration_poll_seconds

        self._running = False
        self._start_in_progress = False
        self._connected = False
        self._connected_this_cycle = False
        self._last_status = "connecting"
        self._ever_connected_once = False

        self._on_status_changed: Optional[OnStatusChanged] = None
        self._on_threat_event: Optional[OnThreatEvent] = None
        self._on_stats_update: Optional[OnStatsUpdate] = None
        self._known_threats: dict[str, dict] = {}

        self._server_id: Optional[str] = None
        self._pending_code: Optional[str] = None
        self._pair_event: Optional[asyncio.Event] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._force_repair = False
        self._raw_http: Optional[httpx.AsyncClient] = None
        self._approval_reason: Optional[str] = None

        #: Currently used route ("" until resolved) and its kind: "lan" | "internet".
        self.server_base_url: str = ""
        self.connection_kind: str = ""
        self._current_credentials: Optional[DeviceCredentials] = None

    # --- read-only info for the optional "Додатково" panel -------------------

    @property
    def device_id(self) -> Optional[str]:
        return self._current_credentials.device_id if self._current_credentials else None

    @property
    def server_id(self) -> Optional[str]:
        return self._server_id

    @property
    def is_connected(self) -> bool:
        return self._connected

    # --- listener registration --------------------------------------------

    def set_status_listener(self, listener: Optional[OnStatusChanged]) -> None:
        self._on_status_changed = listener

    def set_threat_event_listener(self, listener: Optional[OnThreatEvent]) -> None:
        self._on_threat_event = listener

    def set_stats_listener(self, listener: Optional[OnStatsUpdate]) -> None:
        self._on_stats_update = listener

    # --- pairing API (called from UI) --------------------------------------

    def set_pairing_code(self, raw_code: str) -> bool:
        """Accept a typed server code. Returns False if it is not a well-formed code (nothing changes)."""
        server_id = normalize_server_code(raw_code)
        if server_id is None:
            return False
        self._pending_code = server_id
        self._wake_pairing()
        return True

    async def unpair(self) -> None:
        """Forget the paired server AND this phone's identity on it (user chose a different server)."""
        await self._paired_store.clear()
        await self._credential_store.clear()
        self._server_id = None
        self._current_credentials = None
        self._force_repair = True
        self._wake_pairing()

    def _wake_pairing(self) -> None:
        if self._loop is not None and self._pair_event is not None:
            try:
                self._loop.call_soon_threadsafe(self._pair_event.set)
            except RuntimeError:
                pass

    # --- lifecycle -----------------------------------------------------

    async def start(self, device_name: str = "") -> None:
        """Run the whole automatic lifecycle until :meth:`stop` (launch once as a background task)."""
        if self._start_in_progress:
            self._logger.warning("ServerClient: start() вже виконується -- повторний виклик проігноровано.")
            return
        self._start_in_progress = True
        self._running = True
        self._loop = asyncio.get_running_loop()
        self._pair_event = asyncio.Event()
        try:
            await self._run(device_name)
        finally:
            self._start_in_progress = False

    async def stop(self) -> None:
        self._running = False
        self._connected = False
        self._known_threats.clear()
        self._wake_pairing()

    # --- main loop -----------------------------------------------------

    async def _run(self, device_name: str) -> None:
        delay = self._base_retry_delay
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
            self._raw_http = client
            while self._running:
                self._connected = False
                self._connected_this_cycle = False
                self._secure_cache.clear()  # every (re)connection performs a fresh, forward-secret handshake
                if self._ever_connected_once:
                    self._set_status("reconnecting", "Повторне підключення...")
                else:
                    self._set_status("initializing" if self._prov else "connecting",
                                     "Ініціалізація..." if self._prov else "Підключення...")

                server_id = await self._ensure_paired()
                if server_id is None:
                    return  # stopped while waiting for pairing

                route = await self._resolve_route(client, server_id)
                if route is None:
                    if not self._running:
                        return
                    await self._sleep_backoff(delay)
                    delay = min(delay * 2, _MAX_RETRY_DELAY_SECONDS)
                    continue
                self.server_base_url, self.connection_kind = route
                api = self._secure_cache.get(self.server_base_url, client) if self._prov else client

                credentials = await self._obtain_credentials(api, device_name)
                if credentials is None:
                    if not self._running:
                        return
                    if self._last_status == "rejected":
                        return  # terminal until the app is restarted (status already shown)
                    await self._sleep_backoff(delay)
                    delay = min(delay * 2, _MAX_RETRY_DELAY_SECONDS)
                    continue
                self._current_credentials = credentials

                try:
                    await self._login_and_poll(api, credentials, server_id)
                except _CredentialsInvalid:
                    self._logger.warning("Сервер: облікові дані пристрою більше не дійсні -- повторна реєстрація.")
                    await self._credential_store.clear()
                    continue
                except _SwitchRoute:
                    delay = self._base_retry_delay
                    continue

                if not self._running:
                    return
                if self._connected_this_cycle:
                    delay = self._base_retry_delay  # a connection that worked resets the backoff
                await self._sleep_backoff(delay)
                delay = min(delay * 2, _MAX_RETRY_DELAY_SECONDS)

    async def _sleep_backoff(self, delay: float) -> None:
        # Small slices so stop()/unpair() take effect quickly.
        remaining = delay
        while remaining > 0 and self._running and not self._force_repair:
            step = min(0.5, remaining)
            await asyncio.sleep(step)
            remaining -= step

    # --- pairing ---------------------------------------------------------

    async def _ensure_paired(self) -> Optional[str]:
        if self._force_repair:
            self._force_repair = False
        if self._prov is not None:
            # Provisioned build: the server is fixed by the build itself -- nothing to pair, nothing to type.
            sid = self._prov.server_id
            stored = await self._paired_store.load()
            if stored != sid:
                if stored is not None:  # this phone previously belonged to a different server: never reuse its identity
                    await self._credential_store.clear()
                await self._paired_store.save(sid)
            self._server_id = sid
            return sid
        if self._server_id:
            return self._server_id
        stored = await self._paired_store.load()
        if stored:
            self._server_id = stored
            return stored

        # Try to find the server by ITSELF first. The code dialog used to be requested before the first probe, so it popped up
        # on networks where the server would have been found a moment later (a single UDP broadcast often misses on Wi-Fi).
        self._set_status("connecting", "Пошук сервера в локальній мережі...")
        deadline = time.monotonic() + self._discovery_grace_seconds
        while self._running and time.monotonic() < deadline and not self._pending_code:
            ids = {f["server_id"] for f in await asyncio.to_thread(self._discover)}
            if len(ids) == 1:
                server_id = ids.pop()
                await self._paired_store.save(server_id)
                self._server_id = server_id
                self._logger.info("Сервер: знайдено в локальній мережі, сполучення виконано автоматично.")
                return server_id
            await asyncio.sleep(min(1.0, max(0.0, deadline - time.monotonic())))

        # Genuinely not found (other network / several servers / blocked broadcast): only NOW is a human asked, as a fallback.
        self._set_status("pairing_required", "Сервер не знайдено автоматично. Введіть код з програми сервера")
        while self._running:
            if self._pending_code:
                server_id, self._pending_code = self._pending_code, None
                await self._paired_store.save(server_id)
                self._server_id = server_id
                self._logger.info("Сервер: виконано сполучення за кодом.")
                return server_id
            found = await asyncio.to_thread(self._discover)
            ids = {f["server_id"] for f in found}
            if len(ids) == 1:  # exactly one server on this network: pair automatically, nothing to type
                server_id = ids.pop()
                await self._paired_store.save(server_id)
                self._server_id = server_id
                self._logger.info("Сервер: знайдено в локальній мережі, сполучення виконано автоматично.")
                return server_id
            try:
                assert self._pair_event is not None
                await asyncio.wait_for(self._pair_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            if self._pair_event is not None:
                self._pair_event.clear()
        return None

    # --- route resolution ------------------------------------------------

    async def _probe(self, client: httpx.AsyncClient, base: str, server_id: str) -> str:
        """"ok" | "offline" (gateway says the PC is off) | "unreachable" | "mismatch" (wrong server)."""
        if self._prov is not None:
            api = SecureApiClient(client, base, self._prov.server_cert_pem)
            try:
                await api.ensure_session()   # succeeds ONLY if the peer proves it is the provisioned server
            except ServerIdentityError:
                self._logger.warning("Сервер: сертифікат не збігається з очікуваним -- з'єднання відхилено.")
                return "mismatch"
            except httpx.HTTPStatusError as exc:
                return "offline" if exc.response.headers.get(_GATEWAY_ERROR_HEADER, "").lower() == "server_offline" \
                    else "unreachable"
            except (SecureChannelError, httpx.TransportError, httpx.TimeoutException):
                return "unreachable"
            self._secure_cache[base] = api
            return "ok"
        try:
            r = await client.get(f"{base}/identity", timeout=_PROBE_TIMEOUT_SECONDS)
        except (httpx.TransportError, httpx.TimeoutException):
            return "unreachable"
        if r.headers.get(_GATEWAY_ERROR_HEADER, "").lower() == "server_offline":
            return "offline"
        if r.status_code != 200:
            return "unreachable"
        try:
            reported = normalize_server_code(r.json().get("server_id", ""))
        except ValueError:
            return "unreachable"
        return "ok" if reported == server_id else "mismatch"

    async def _lan_route(self, client: httpx.AsyncClient, server_id: str) -> Optional[str]:
        for f in await asyncio.to_thread(self._discover):
            if f.get("server_id") == server_id:
                base = f"http://{f['ip']}:{f['port']}"
                if await self._probe(client, base, server_id) == "ok":
                    return base
        return None

    async def _resolve_route(self, client: httpx.AsyncClient, server_id: str) -> Optional[tuple[str, str]]:
        lan = await self._lan_route(client, server_id)
        if lan:
            self._logger.info("Сервер: маршрут -- локальна мережа.")
            return lan, "lan"
        if _is_placeholder(self._gateway_url):
            self._set_status("gateway_unconfigured", "Інтернет-шлюз не налаштовано у цій збірці застосунку.")
            return None
        base = f"{self._gateway_url}/s/{server_id}"
        outcome = await self._probe(client, base, server_id)
        if outcome == "ok":
            self._logger.info("Сервер: маршрут -- інтернет-шлюз.")
            return base, "internet"
        if outcome == "offline":
            self._set_status("server_offline", "Сервер вимкнено або не підключений до інтернету")
        elif outcome == "mismatch":
            self._set_status("server_offline", "Сервер недоступний (його ідентичність не підтверджено)")
        elif self._prov is not None:
            self._set_status("gateway_unavailable", "Немає зв'язку зі шлюзом")
        else:
            self._set_status("unreachable", "Сервер недоступний")
        return None

    # --- credentials / registration ----------------------------------------

    async def _public_proof(self, client, device_id: str) -> Optional[dict]:
        """Public build: fetch a challenge, solve the proof-of-work OFF the event loop, optionally attach attestation."""
        try:
            r = await client.get(f"{self.server_base_url}/auth/enroll/challenge")
        except _TRANSPORT_ERRORS:
            self._set_status(*self._unreachable_status())
            return None
        if r.status_code == 404:
            return {}                    # server is not in public mode: register without proof (=> manual approval)
        if r.status_code != 200:
            self._set_status("registering", "Сервер зайнятий, повторна спроба...")
            return None
        try:
            body = r.json()
            challenge, bits = str(body["challenge"]), int(body["difficulty"])
        except (ValueError, KeyError, TypeError):
            return None
        if not 8 <= bits <= 28:          # never let a hostile server pin the CPU with an absurd difficulty
            return None
        self._set_status("registering", "Підготовка реєстрації...")
        nonce = await asyncio.to_thread(pow_solver.solve, challenge, device_id, bits)
        proof = {"pow_challenge": challenge, "pow_nonce": str(nonce)}
        if self._attestation_provider is not None:
            try:
                token = await self._attestation_provider(device_id, challenge)
                if token:
                    proof["attestation"] = token
            except Exception:  # noqa: BLE001 - attestation is optional; never block enrollment on the provider crashing
                self._logger.warning("Атестація пристрою недоступна.")
        return proof

    async def _obtain_credentials(self, client, device_name: str) -> Optional[DeviceCredentials]:
        creds = await self._credential_store.load()
        if creds is None:
            # Generated and PERSISTED before the first network call: a crash/retry can never lose the secret or
            # create a second device.
            creds = DeviceCredentials(device_id=secrets.token_hex(16), token=secrets.token_urlsafe(32), approved=False)
            await self._credential_store.save(creds)
        self._current_credentials = creds   # visible (e.g. in Додатково) as soon as it exists, incl. while awaiting approval
        if creds.approved:
            return creds
        return await self._register_and_wait(client, creds, device_name)

    async def _register_and_wait(
        self, client, creds: DeviceCredentials, device_name: str
    ) -> Optional[DeviceCredentials]:
        registered = False
        while self._running and not self._force_repair:
            if not registered:
                self._set_status("registering", "Реєстрація пристрою...")
                payload = {"device_name": device_name, "device_id": creds.device_id, "device_secret": creds.token}
                if self._prov is not None and not self._prov.is_public:
                    payload["enroll_key"] = self._prov.enroll_key          # private build with a scoped token
                elif self._prov is not None:
                    proof = await self._public_proof(client, creds.device_id)
                    if proof is None:
                        return None                                          # transient: retry later with a NEW challenge
                    payload.update(proof)
                try:
                    r = await client.post(f"{self.server_base_url}/auth/register", json=payload)
                except _TRANSPORT_ERRORS:
                    self._set_status(*self._unreachable_status())
                    return None
                if r.status_code == 409:  # id taken by a different secret: start over with a fresh identity
                    await self._credential_store.clear()
                    return None
                if r.status_code == 403 and self._prov is not None and self._prov.is_public and _is_transient_403(r):
                    self._set_status("registering", "Повторна спроба реєстрації...")   # expired/replayed/busy challenge
                    return None
                if r.status_code in (400, 403):  # bad/rotated enrollment key, or the server's device limit is reached
                    self._logger.warning(f"Сервер відхилив реєстрацію пристрою ({r.status_code}).")
                    self._set_status("rejected", "Підключення відхилено сервером.")
                    return None
                if r.status_code == 429:  # rate limited (e.g. many phones behind one carrier IP): retry with backoff
                    self._set_status("registering", "Сервер зайнятий, повторна спроба...")
                    return None
                if r.status_code != 200:
                    self._set_status(*self._unreachable_status())
                    return None
                try:
                    body = r.json()
                    auto_approved = bool(body.get("auto_approved"))
                    self._approval_reason = body.get("approval_reason")
                except ValueError:
                    auto_approved, self._approval_reason = False, None
                if auto_approved:  # server policy approved this device at once: no waiting, nobody involved
                    approved = DeviceCredentials(device_id=creds.device_id, token=creds.token, approved=True)
                    await self._credential_store.save(approved)
                    self._logger.info("Сервер: пристрій автоматично підтверджено.")
                    return approved
                registered = True
                self._logger.info("Сервер: пристрій зареєстровано, очікування підтвердження адміністратора.")
            self._set_status("waiting_approval", _WAITING_MESSAGES.get(self._approval_reason or "",
                                                                       "Очікування підтвердження сервера..."))

            await self._sleep_backoff(self._registration_poll_seconds)
            if not self._running:
                return None
            try:
                response = await client.get(
                    f"{self.server_base_url}/auth/register/status", params={"device_id": creds.device_id}
                )
                response.raise_for_status()
                data = response.json()
            except _TRANSPORT_ERRORS:
                self._set_status(*self._unreachable_status())
                return None  # credentials stay persisted: the next attempt resumes THIS registration

            reg_status = data.get("status")
            if reg_status == "approved":
                # Legacy servers hand back their own token; new servers return none and our secret stays valid.
                approved = DeviceCredentials(device_id=creds.device_id, token=data.get("token") or creds.token,
                                             approved=True)
                await self._credential_store.save(approved)
                self._logger.info("Сервер: пристрій підтверджено адміністратором.")
                return approved
            if reg_status == "rejected":
                self._logger.warning("Сервер: у підключенні пристрою відмовлено адміністратором.")
                self._set_status("rejected", "У доступі відмовлено адміністратором сервера.")
                return None
            if reg_status == "not_found":
                registered = False  # server data was reset: register the SAME identity again
        return None

    # --- login + polling ---------------------------------------------------

    def _unreachable_status(self) -> tuple[str, str]:
        if self._prov is not None and self.connection_kind == "internet":
            return "gateway_unavailable", "Немає зв'язку зі шлюзом"
        return "unreachable", "Сервер недоступний"

    def _status_for_http_error(self, exc: Exception) -> tuple[str, str]:
        if isinstance(exc, httpx.HTTPStatusError) and \
                exc.response.headers.get(_GATEWAY_ERROR_HEADER, "").lower() == "server_offline":
            return "server_offline", "Сервер вимкнено або не підключений до інтернету"
        return self._unreachable_status()

    async def _login_and_poll(self, client, credentials: DeviceCredentials, server_id: str) -> None:
        raw_http = self._raw_http or client
        try:
            access_token = await self._login(client, credentials)
        except _CredentialsInvalid:
            raise
        except _TRANSPORT_ERRORS as exc:
            self._connected = False
            if not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code != 403:
                self._set_status(*self._status_for_http_error(exc))
            return

        last_lan_check = time.monotonic()
        while self._running and not self._force_repair:
            headers = {"Authorization": f"Bearer {access_token}"}
            try:
                threats = await self._fetch_json(client, f"{self.server_base_url}/threats/active", headers)
                self._diff_and_emit_threats(threats)

                stats = await self._fetch_json(client, f"{self.server_base_url}/stats", headers)
                if self._on_stats_update is not None:
                    self._on_stats_update(stats)

                if not self._connected:
                    self._connected = True
                    self._connected_this_cycle = True
                    self._ever_connected_once = True
                    self._set_status("connected", "Підключено")

            except _CredentialsInvalid:
                raise
            except _ReauthRequired:
                try:
                    access_token = await self._login(client, credentials)
                except _CredentialsInvalid:
                    raise
                except _TRANSPORT_ERRORS as exc:
                    self._connected = False
                    if not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code != 403:
                        self._set_status(*self._status_for_http_error(exc))
                    return
                continue
            except _TRANSPORT_ERRORS as exc:
                self._connected = False
                self._set_status(*self._status_for_http_error(exc))
                return  # network change / server restart: the caller re-resolves the route

            await asyncio.sleep(self._poll_interval_seconds)

            if self.connection_kind == "internet" and \
                    time.monotonic() - last_lan_check >= self._lan_upgrade_check_seconds:
                last_lan_check = time.monotonic()
                lan = await self._lan_route(raw_http, server_id)
                if lan:
                    self._logger.info("Сервер: знайдено в локальній мережі -- перемикання на пряме з'єднання.")
                    raise _SwitchRoute()

    async def _login(self, client: httpx.AsyncClient, credentials: DeviceCredentials) -> str:
        response = await client.post(
            f"{self.server_base_url}/auth/login",
            json={"device_id": credentials.device_id, "token": credentials.token},
        )
        if response.status_code == 401:
            raise _CredentialsInvalid("device_id/token not recognized by server")
        if response.status_code == 403:
            detail = ""
            try:
                detail = response.json().get("detail", "")
            except Exception:
                pass
            if self._prov is not None:   # blocked / revoked by the server's administrator: do NOT re-enroll around it
                self._set_status("rejected", detail or "Доступ відкликано адміністратором сервера.")
            else:
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
