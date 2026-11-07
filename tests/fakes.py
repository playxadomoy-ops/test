"""Test doubles: in-memory stores + a programmable fake of the AirAlert server HTTP API."""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

from app.storage.device_store import DeviceCredentials

SID = "ABCDEFGHJKMN"


class MemCredStore:
    def __init__(self) -> None:
        self.creds: Optional[DeviceCredentials] = None
        self.saves: list[DeviceCredentials] = []

    async def load(self):
        return self.creds

    async def save(self, c):
        self.creds = c
        self.saves.append(c)

    async def clear(self):
        self.creds = None


class MemPairStore:
    def __init__(self, server_id: Optional[str] = None) -> None:
        self.server_id = server_id

    async def load(self):
        return self.server_id

    async def save(self, sid):
        self.server_id = sid

    async def clear(self):
        self.server_id = None


class FakeLogger:
    def __getattr__(self, name):
        return lambda *a, **k: None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeServer:
    """Behaves like the real server's phone-facing API (identity, idempotent register, approval, login)."""

    def __init__(self, server_id: str = SID, port: Optional[int] = None, gateway_style: bool = False) -> None:
        self.server_id = server_id
        self.port = port or free_port()
        self.gateway_style = gateway_style      # serve under /s/<id> like the gateway does
        self.devices: dict[str, dict] = {}      # device_id -> {secret, status}
        self.register_calls = 0
        self.login_calls = 0
        self.offline_header = False             # behave like the gateway reporting server_offline
        self.status_override: Optional[str] = None
        self.reject_logins_with: Optional[int] = None
        self._httpd: Optional[ThreadingHTTPServer] = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "FakeServer":
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): ...

            def _send(self, code, body, extra=None):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def _route(self):
                u = urlparse(self.path)
                path = u.path
                if outer.gateway_style:
                    prefix = f"/s/{outer.server_id}"
                    if not path.startswith(prefix):
                        return self._send(503, {"code": "server_offline"}, {"X-AirAlert-Gateway": "server_offline"})
                    path = path[len(prefix):]
                if outer.offline_header:
                    return self._send(503, {"code": "server_offline"}, {"X-AirAlert-Gateway": "server_offline"})
                body = {}
                n = int(self.headers.get("content-length") or 0)
                if n:
                    body = json.loads(self.rfile.read(n))
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                if path == "/identity":
                    return self._send(200, {"service": "air-alert-analyzer", "server_id": outer.server_id})
                if path == "/auth/register":
                    outer.register_calls += 1
                    did, sec = body["device_id"], body["device_secret"]
                    if did in outer.devices and outer.devices[did]["secret"] != sec:
                        return self._send(409, {"detail": "taken"})
                    outer.devices.setdefault(did, {"secret": sec, "status": "pending"})
                    return self._send(200, {"device_id": did, "auto_approved": False})
                if path == "/auth/register/status":
                    d = outer.devices.get(q.get("device_id", ""))
                    if d is None:
                        return self._send(200, {"status": "not_found"})
                    return self._send(200, {"status": outer.status_override or {"active": "approved"}.get(
                        d["status"], d["status"]), "token": None, "display_name": "x"})
                if path == "/auth/login":
                    outer.login_calls += 1
                    if outer.reject_logins_with:
                        return self._send(outer.reject_logins_with, {"detail": "no"})
                    d = outer.devices.get(body.get("device_id", ""))
                    if d is None or d["secret"] != body.get("token"):
                        return self._send(401, {"detail": "bad"})
                    if d["status"] != "active":
                        return self._send(403, {"detail": "Очікує підтвердження адміністратора."})
                    return self._send(200, {"access_token": "jwt", "token_type": "bearer", "user_number": 1,
                                            "role": "user", "display_name": "x"})
                if path == "/threats/active":
                    return self._send(200, [])
                if path == "/stats":
                    return self._send(200, {"users_total": 1})
                self._send(404, {"detail": "nf"})

            do_GET = do_POST = _route

        class S(ThreadingHTTPServer):
            allow_reuse_address = True
            daemon_threads = True
        self._httpd = S(("127.0.0.1", self.port), H)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def approve_all(self) -> None:
        for d in self.devices.values():
            d["status"] = "active"


async def wait_until(cond, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return False
