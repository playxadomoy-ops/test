"""Pairing helpers: server code normalization and LAN discovery. No Flet imports (unit-testable anywhere)."""

from __future__ import annotations

import json
import re
import socket
import time
from typing import Optional

#: Crockford base32 -- identical to the server's services/server_identity.py.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ID_RE = re.compile(rf"^[{_ALPHABET}]{{12}}$")


def normalize_server_code(raw: object) -> Optional[str]:
    """Typed code ('abcd-efgh-jkmn', spaces, O/I/L typos) -> canonical 12-char server_id, else None."""
    if not isinstance(raw, str):
        return None
    cleaned = re.sub(r"[\s\-_]", "", raw).upper().replace("O", "0").replace("I", "1").replace("L", "1")
    return cleaned if _ID_RE.match(cleaned) else None


def format_server_code(server_id: str) -> str:
    return "-".join(server_id[i:i + 4] for i in range(0, len(server_id), 4))


def lan_discover(timeout: float = 1.2, port: int = 37020, broadcast_addr: str = "255.255.255.255") -> list[dict]:
    """UDP-broadcast "who is there" and collect replies. BLOCKING -- call via asyncio.to_thread.

    Returns [{"ip", "port", "server_id"}] (empty on mobile data / no Wi-Fi / nobody answering). This is only an
    optimization and an auto-pairing convenience: the app works without it, through the gateway.
    """
    found: dict[tuple[str, int], dict] = {}
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return []
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.25)
        message = json.dumps({"type": "air_alert_discovery"}).encode("utf-8")
        deadline = time.monotonic() + timeout
        next_send = 0.0
        while time.monotonic() < deadline:
            if time.monotonic() >= next_send:  # re-send: UDP may drop the first packet
                try:
                    sock.sendto(message, (broadcast_addr, port))
                except OSError:
                    return []  # no usable network interface (e.g. airplane mode / mobile data only)
                next_send = time.monotonic() + 0.5
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                reply = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if not isinstance(reply, dict) or reply.get("service") != "air-alert-analyzer":
                continue
            server_id = normalize_server_code(reply.get("server_id", ""))
            api_port = reply.get("api_port")
            if server_id and isinstance(api_port, int) and 0 < api_port < 65536:
                found[(addr[0], api_port)] = {"ip": addr[0], "port": api_port, "server_id": server_id}
    finally:
        sock.close()
    return list(found.values())
