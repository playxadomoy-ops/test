"""Phone side of the end-to-end encrypted channel (counterpart of server/services/e2e_channel.py).

TLS 1.3 from the standard library (OpenSSL) over memory buffers, carried as opaque request/response bodies so the
app stays on plain REST polling. The phone trusts ONLY the server certificate it was provisioned with (a customer-
specific pin), so it can complete a handshake with no other server -- not another customer's, not the gateway, not an
attacker on the LAN. Everything the app sends (enrollment, login/JWT, polling) is inside the tunnel: the gateway
relays ciphertext it cannot read.

    POST <base>/e2e/<conn_id>   body = seq(8, big-endian) || TLS bytes   ->   200 + TLS bytes

Reliability: a lost response is retried with the SAME bytes and seq (the server answers from cache, never re-executes);
an unknown/expired/out-of-order connection (server restart, session renewal) transparently triggers a fresh handshake
and one resend. No dependencies beyond httpx.
"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import ssl
import struct
from typing import Optional

import httpx

TLS_HOSTNAME = "airalert-server"
_GATEWAY_ERROR_HEADER = "x-airalert-gateway"
_MAX_FRAME = 1 << 20


class SecureChannelError(Exception):
    """The encrypted session could not be established or was corrupted."""


class ServerIdentityError(SecureChannelError):
    """The peer is NOT the provisioned server (wrong certificate): a different customer's server or an impostor."""


class _SessionLost(Exception):
    pass


class SecureApiClient:
    """httpx-like facade (``get``/``post`` returning real ``httpx.Response``) over the encrypted channel."""

    def __init__(self, http: httpx.AsyncClient, base_url: str, server_cert_pem: str,
                 request_timeout: float = 15.0) -> None:
        self._http = http
        self.base_url = base_url.rstrip("/")
        self._cert_pem = server_cert_pem
        self._timeout = request_timeout
        self._lock = asyncio.Lock()
        self._tls: Optional[ssl.SSLObject] = None
        self._inc: Optional[ssl.MemoryBIO] = None
        self._out: Optional[ssl.MemoryBIO] = None
        self._conn_id = ""
        self._seq = 0
        self._next_id = 0
        self.handshakes = 0  # how many full handshakes this client performed (renewals/reconnects visible to tests)

    # ------------------------------------------------------------------ session
    def _context(self) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(cadata=self._cert_pem)  # the ONLY trust anchor: the provisioned server certificate
        return ctx

    async def reset(self) -> None:
        self._tls = self._inc = self._out = None
        self._conn_id, self._seq = "", 0

    async def ensure_session(self) -> None:
        async with self._lock:
            await self._ensure_session_locked()

    async def _ensure_session_locked(self) -> None:
        if self._tls is not None:
            return
        self._inc, self._out = ssl.MemoryBIO(), ssl.MemoryBIO()
        self._tls = self._context().wrap_bio(self._inc, self._out, server_hostname=TLS_HOSTNAME)
        self._conn_id, self._seq = secrets.token_hex(16), 0
        try:
            while True:
                try:
                    self._tls.do_handshake()
                    break
                except ssl.SSLWantReadError:
                    flight = self._out.read()
                    if not flight:
                        raise SecureChannelError("TLS handshake stalled")
                    self._inc.write(await self._exchange(flight))
            pending = self._out.read()           # client Finished: send now so the server side is fully established too
            if pending:
                self._inc.write(await self._exchange(pending))
        except ssl.SSLCertVerificationError as exc:
            await self.reset()
            raise ServerIdentityError("Server certificate does not match the provisioned server") from exc
        except ssl.SSLError as exc:
            await self.reset()
            raise SecureChannelError(f"TLS error: {exc.reason}") from exc
        except _SessionLost as exc:
            await self.reset()
            raise SecureChannelError("Handshake rejected by server") from exc
        except BaseException:
            await self.reset()
            raise
        self.handshakes += 1

    # ------------------------------------------------------------------ transport
    async def _exchange(self, data: bytes) -> bytes:
        url = f"{self.base_url}/e2e/{self._conn_id}"
        body = struct.pack(">Q", self._seq) + data
        last_exc: Optional[Exception] = None
        for _ in range(2):  # one retry with IDENTICAL bytes+seq: the server de-duplicates, never re-executes
            try:
                resp = await self._http.post(url, content=body, timeout=self._timeout,
                                             headers={"content-type": "application/octet-stream"})
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                continue
            if resp.headers.get(_GATEWAY_ERROR_HEADER):
                resp.raise_for_status()                    # server_offline / busy / rate_limited ... (not from the server)
            if resp.status_code == 200:
                self._seq += 1
                return resp.content
            if resp.status_code in (400, 404, 409, 410):
                raise _SessionLost(str(resp.status_code))
            resp.raise_for_status()
            raise SecureChannelError(f"unexpected status {resp.status_code}")
        assert last_exc is not None
        raise last_exc

    # ------------------------------------------------------------------ requests
    async def request(self, method: str, path: str, *, json_body=None, params=None, headers=None) -> httpx.Response:
        async with self._lock:
            for attempt in (1, 2):
                await self._ensure_session_locked()
                try:
                    return await self._roundtrip(method, path, json_body, params, headers)
                except _SessionLost:
                    await self.reset()          # server restarted / session renewed / desync: re-handshake, resend once
                    if attempt == 2:
                        raise SecureChannelError("Session could not be re-established")
                except ssl.SSLError as exc:
                    await self.reset()
                    raise SecureChannelError(f"TLS error: {exc.reason}") from exc
        raise SecureChannelError("unreachable")

    async def _roundtrip(self, method, path, json_body, params, headers) -> httpx.Response:
        assert self._tls and self._inc and self._out
        self._next_id += 1
        content = b"" if json_body is None else json.dumps(json_body).encode()
        hdrs = {k.lower(): v for k, v in (headers or {}).items() if k.lower() in ("authorization", "accept")}
        if json_body is not None:
            hdrs["content-type"] = "application/json"
        frame = {"id": self._next_id, "method": method.upper(), "path": path,
                 "query": str(httpx.QueryParams(params)) if params else "", "headers": hdrs,
                 "body": base64.b64encode(content).decode("ascii")}
        blob = json.dumps(frame, separators=(",", ":")).encode()
        self._tls.write(struct.pack(">I", len(blob)) + blob)
        self._inc.write(await self._exchange(self._out.read()))
        buf = bytearray()
        while True:
            try:
                chunk = self._tls.read(65536)
            except ssl.SSLWantReadError:
                raise SecureChannelError("Incomplete response from server")
            if not chunk:
                raise SecureChannelError("Connection closed by server")
            buf += chunk
            if len(buf) >= 4:
                (length,) = struct.unpack(">I", bytes(buf[:4]))
                if length > _MAX_FRAME:
                    raise SecureChannelError("Response too large")
                if len(buf) >= 4 + length:
                    break
        reply = json.loads(bytes(buf[4:4 + length]))
        return httpx.Response(int(reply["status"]), headers=reply.get("headers") or {},
                              content=base64.b64decode(reply.get("body", "")),
                              request=httpx.Request(method.upper(), f"{self.base_url}{path}"))

    # ------------------------------------------------------------------ httpx-like convenience (full URLs)
    def _path(self, url: str) -> str:
        return url[len(self.base_url):] if url.startswith(self.base_url) else httpx.URL(url).raw_path.decode()

    async def get(self, url: str, params=None, headers=None, timeout=None) -> httpx.Response:
        return await self.request("GET", self._path(url), params=params, headers=headers)

    async def post(self, url: str, json=None, headers=None, timeout=None) -> httpx.Response:
        return await self.request("POST", self._path(url), json_body=json, headers=headers)
