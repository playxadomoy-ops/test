"""
LAN discovery client.

Finds the Air Alert Analyzer server on the local network by UDP
broadcast -- this app never stores or hardcodes a server IP address.
Every connection attempt (first launch, reconnect after a drop, or
reconnect after the server's IP changed via DHCP) starts by asking
"where are you" again, so a server that moved to a new address is
found automatically without the user doing anything.

Uses a plain blocking socket (matching the server's own
DiscoveryResponder implementation) run in a thread-pool executor via
``run_in_executor`` -- the most portable way to do UDP broadcast from
an asyncio app, including on the Android runtime this app ships on.

Protocol: see server/services/discovery_service.py's module docstring
for the exact request/response shape -- kept in sync manually since
these are two separate deployable projects.
"""

from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import dataclass
from typing import Optional

DISCOVERY_PORT = 37020
PROTOCOL_VERSION = 1
_RECV_BUFFER_SIZE = 2048


@dataclass(slots=True)
class DiscoveredServer:
    ip: str
    api_port: int
    server_name: str
    server_version: str
    registration_allowed: bool

    @property
    def base_url(self) -> str:
        return f"http://{self.ip}:{self.api_port}"


def _blocking_discover(timeout: float) -> Optional[DiscoveredServer]:
    """Runs on a worker thread (see discover_server) -- safe to block here."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(timeout)
        request = json.dumps({"type": "air_alert_discovery"}).encode("utf-8")
        try:
            sock.sendto(request, ("255.255.255.255", DISCOVERY_PORT))
        except OSError:
            # Some Android network states (e.g. mobile data, no WiFi)
            # reject broadcast sends outright -- that's just "no server
            # reachable this way", not a crash-worthy condition.
            return None

        try:
            data, addr = sock.recvfrom(_RECV_BUFFER_SIZE)
        except socket.timeout:
            return None
        except OSError:
            return None

        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

        if not isinstance(payload, dict) or payload.get("service") != "air-alert-analyzer":
            return None
        if payload.get("protocol_version") != PROTOCOL_VERSION:
            return None

        try:
            api_port = int(payload["api_port"])
        except (KeyError, TypeError, ValueError):
            return None

        return DiscoveredServer(
            ip=addr[0],
            api_port=api_port,
            server_name=str(payload.get("server_name", "Air Alert Analyzer Server")),
            server_version=str(payload.get("server_version", "")),
            registration_allowed=bool(payload.get("registration_allowed", True)),
        )
    finally:
        sock.close()


async def discover_server(timeout: float = 3.0) -> Optional[DiscoveredServer]:
    """Broadcast a discovery request and wait up to ``timeout`` seconds for a reply.

    Returns ``None`` (never raises) if no server answered in time, the
    device has no usable network, or the reply didn't look like ours --
    every caller treats "not found" as a normal, expected outcome to
    retry later, not an error.
    """
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, _blocking_discover, timeout)
    except Exception:  # noqa: BLE001 - discovery must never crash the caller
        return None
