"""Mobile ServerClient logic: UNIT tests against a programmable fake server (no real network beyond localhost)."""
from __future__ import annotations

import asyncio
import json
import socket
import threading

import pytest

from app.services.pairing import format_server_code, lan_discover, normalize_server_code
from app.services.server_client import ServerClient
from app.storage.device_store import DeviceCredentials

from .fakes import SID, FakeLogger, FakeServer, MemCredStore, MemPairStore, wait_until


class Rig:
    """One phone: stores + client + recorded statuses, with fast timings."""

    def __init__(self, gateway_url="http://127.0.0.1:1", lan=None, paired=SID, creds=None, **kw):
        self.cred = MemCredStore()
        self.cred.creds = creds
        self.pair = MemPairStore(paired)
        self.lan = lan if lan is not None else (lambda: [])
        self.statuses: list[str] = []
        self.client = ServerClient(FakeLogger(), self.cred, self.pair, poll_interval_seconds=0.03,
                                   gateway_url=gateway_url, discover=lambda: self.lan(),
                                   base_retry_delay=0.05, registration_poll_seconds=0.03,
                                   **{"discovery_grace_seconds": 0.3, **kw})
        self.client.set_status_listener(lambda c, m: self.statuses.append(c))
        self.task = None

    async def __aenter__(self):
        self.task = asyncio.create_task(self.client.start("Test phone"))
        return self

    async def __aexit__(self, *exc):
        await self.client.stop()
        try:
            await asyncio.wait_for(self.task, 5)
        except Exception:
            self.task.cancel()

    async def until(self, status, timeout=8.0):
        assert await wait_until(lambda: status in self.statuses, timeout), f"never saw {status}: {self.statuses}"


@pytest.fixture()
def srv():
    s = FakeServer(gateway_style=True).start()
    yield s
    s.stop()


def gw_url(s):  # a "gateway" that is just the fake serving under /s/<id>
    return s.base


# ----------------------------------------------------------------------------- pairing

@pytest.mark.parametrize("raw,expected", [("abcd-efgh-jkmn", SID), ("ABCD EFGH JKMN", SID), ("abcdefghjkmn", SID),
                                         ("o0o0-i1l1-ABCD", "00001111ABCD"), ("short", None), ("", None), (None, None),
                                         ("UUUU-UUUU-UUUU", None)])
def test_normalize(raw, expected):
    assert normalize_server_code(raw) == expected
    assert format_server_code(SID) == "ABCD-EFGH-JKMN"


def test_lan_discover_parses_replies_and_ignores_garbage():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]

    def responder():
        sock.settimeout(3)
        try:
            for _ in range(2):
                _, addr = sock.recvfrom(1024)
                sock.sendto(b"not json", addr)
                sock.sendto(json.dumps({"service": "other"}).encode(), addr)
                sock.sendto(json.dumps({"service": "air-alert-analyzer", "server_id": "abcd-efgh-jkmn",
                                        "api_port": 8765}).encode(), addr)
        except OSError:
            pass
    threading.Thread(target=responder, daemon=True).start()
    found = lan_discover(timeout=1.0, port=port, broadcast_addr="127.0.0.1")
    sock.close()
    assert found == [{"ip": "127.0.0.1", "port": 8765, "server_id": SID}]


def test_lan_discover_returns_empty_when_nobody_answers():
    assert lan_discover(timeout=0.4, port=9, broadcast_addr="127.0.0.1") == []


async def test_pairing_required_then_typed_code_pairs_and_persists(srv):
    async with Rig(paired=None, gateway_url=gw_url(srv)) as r:
        await r.until("pairing_required")
        assert r.client.set_pairing_code("garbage") is False
        assert r.client.set_pairing_code("abcd-efgh-jkmn") is True
        await r.until("waiting_approval")
        assert r.pair.server_id == SID and r.client.server_id == SID


async def test_lan_single_server_autopairs_with_no_typing(srv):
    lan = lambda: [{"ip": "127.0.0.1", "port": srv.port, "server_id": SID}]
    async with Rig(paired=None, lan=lan, gateway_url="http://127.0.0.1:1") as r:
        await wait_until(lambda: r.pair.server_id == SID)
        assert r.pair.server_id == SID


async def test_lan_with_two_servers_does_not_guess():
    lan = lambda: [{"ip": "10.0.0.1", "port": 1, "server_id": SID}, {"ip": "10.0.0.2", "port": 1, "server_id": "B" * 12}]
    async with Rig(paired=None, lan=lan) as r:
        await r.until("pairing_required")
        await asyncio.sleep(0.3)
        assert r.pair.server_id is None


# ----------------------------------------------------------------------------- registration / identity

async def test_identity_generated_and_persisted_BEFORE_registering(srv):
    async with Rig(gateway_url=gw_url(srv)) as r:
        await r.until("waiting_approval")
        c = r.cred.creds
        assert c and not c.approved and len(c.device_id) == 32 and len(c.token) >= 32
        assert list(srv.devices) == [c.device_id] and srv.devices[c.device_id]["secret"] == c.token


async def test_approval_then_connected_and_credentials_marked_approved(srv):
    async with Rig(gateway_url=gw_url(srv)) as r:
        await r.until("waiting_approval")
        srv.approve_all()
        await r.until("connected")
        assert r.cred.creds.approved and r.client.connection_kind == "internet"


async def test_app_killed_while_waiting_does_not_create_duplicate_device(srv):
    cred, pair = MemCredStore(), MemPairStore(SID)
    for _ in range(3):  # three app launches while the admin hasn't approved yet
        rig = Rig(gateway_url=gw_url(srv)); rig.cred, rig.pair = cred, pair
        rig.client._credential_store, rig.client._paired_store = cred, pair
        async with rig:
            await rig.until("waiting_approval")
    assert len(srv.devices) == 1, "re-launching must resume the SAME registration"


async def test_lost_register_response_retry_is_idempotent(srv):
    async with Rig(gateway_url=gw_url(srv)) as r:
        await r.until("waiting_approval")
    srv.register_calls = 0
    async with Rig(gateway_url=gw_url(srv), creds=r.cred.creds) as r2:
        await r2.until("waiting_approval")
    assert len(srv.devices) == 1


async def test_login_401_clears_identity_and_registers_fresh(srv):
    old = DeviceCredentials("deadbeefdeadbeef", "x" * 43, approved=True)
    async with Rig(gateway_url=gw_url(srv), creds=old) as r:
        await r.until("waiting_approval")
        assert r.cred.creds and r.cred.creds.device_id != old.device_id


async def test_403_blocked_keeps_credentials(srv):
    c = DeviceCredentials("a" * 16, "s" * 43, approved=True)
    srv.devices[c.device_id] = {"secret": c.token, "status": "blocked"}
    async with Rig(gateway_url=gw_url(srv), creds=c) as r:
        await asyncio.sleep(0.5)
        assert r.cred.creds == c and len(srv.devices) == 1


async def test_rejected_is_terminal_and_reported(srv):
    srv.status_override = "rejected"
    async with Rig(gateway_url=gw_url(srv)) as r:
        await r.until("rejected")
        n = srv.register_calls
        await asyncio.sleep(0.4)
        assert srv.register_calls == n


async def test_server_data_reset_reregisters_same_identity(srv):
    async with Rig(gateway_url=gw_url(srv)) as r:
        await r.until("waiting_approval")
        did = r.cred.creds.device_id
        srv.devices.clear()                      # server reinstalled / DB wiped
        await wait_until(lambda: did in srv.devices)
        assert did in srv.devices and len(srv.devices) == 1


# ----------------------------------------------------------------------------- routes

async def test_unconfigured_gateway_is_reported_honestly_not_online():
    async with Rig(gateway_url="https://gateway.airalertanalyzer.example") as r:
        await r.until("gateway_unconfigured")
        assert "connected" not in r.statuses


async def test_lan_preferred_when_found_and_identity_matches():
    s = FakeServer(gateway_style=False).start()
    try:
        lan = lambda: [{"ip": "127.0.0.1", "port": s.port, "server_id": SID}]
        async with Rig(lan=lan, gateway_url="http://127.0.0.1:1") as r:
            await r.until("waiting_approval")
            assert r.client.connection_kind == "lan"
            s.approve_all()
            await r.until("connected")
    finally:
        s.stop()


async def test_lan_failure_falls_back_to_internet(srv):
    lan = lambda: [{"ip": "127.0.0.1", "port": 1, "server_id": SID}]      # advertised but unreachable
    async with Rig(lan=lan, gateway_url=gw_url(srv)) as r:
        await r.until("waiting_approval")
        assert r.client.connection_kind == "internet"


async def test_never_sends_credentials_to_a_server_with_a_different_id():
    """A LAN impostor / wrong gateway mapping must receive neither registration nor login."""
    imposter = FakeServer(server_id="Z" * 12, gateway_style=False).start()
    try:
        c = DeviceCredentials("a" * 16, "s" * 43, approved=True)
        lan = lambda: [{"ip": "127.0.0.1", "port": imposter.port, "server_id": SID}]  # claims to be ours
        async with Rig(lan=lan, gateway_url="http://127.0.0.1:1", creds=c) as r:
            await r.until("unreachable")
            await asyncio.sleep(0.4)
        assert imposter.login_calls == 0 and imposter.register_calls == 0
    finally:
        imposter.stop()


async def test_server_offline_is_distinguished_from_no_internet(srv):
    srv.offline_header = True
    async with Rig(gateway_url=gw_url(srv)) as r:
        await r.until("server_offline")
        assert "unreachable" not in r.statuses
    dead = Rig(gateway_url="http://127.0.0.1:1")
    async with dead:
        await dead.until("unreachable")


async def test_two_customers_each_reach_only_their_own_server():
    a = FakeServer(server_id="A" * 12, gateway_style=True).start()
    b = FakeServer(server_id="B" * 12, gateway_style=True).start()
    try:
        async with Rig(paired="A" * 12, gateway_url=a.base) as ra:
            await ra.until("waiting_approval")
        async with Rig(paired="B" * 12, gateway_url=b.base) as rb:
            await rb.until("waiting_approval")
        assert len(a.devices) == 1 and len(b.devices) == 1 and not set(a.devices) & set(b.devices)
        # Phone A pointed at B's gateway/server gets nothing: wrong id => no registration there.
        before = len(b.devices)
        async with Rig(paired="A" * 12, gateway_url=b.base) as wrong:
            await wrong.until("server_offline")
        assert len(b.devices) == before
    finally:
        a.stop(); b.stop()


# ----------------------------------------------------------------------------- network changes

async def test_reconnects_after_connection_loss_without_reregistering(srv):
    async with Rig(gateway_url=gw_url(srv)) as r:
        await r.until("waiting_approval")
        srv.approve_all()
        await r.until("connected")
        did, regs = r.cred.creds.device_id, srv.register_calls
        r.statuses.clear()
        port = srv.port
        srv.stop()                                         # phone loses the Internet / server unreachable
        await r.until("unreachable")
        assert not r.client.is_connected
        srv2 = FakeServer(port=port, gateway_style=True); srv2.devices = srv.devices; srv2.start()
        try:
            await r.until("connected")                     # automatic recovery, no user action
            assert r.cred.creds.device_id == did and srv2.register_calls == 0 and srv.register_calls == regs
        finally:
            srv2.stop()


async def test_switches_from_internet_to_lan_when_phone_joins_the_lan():
    lan_srv = FakeServer(gateway_style=False).start()
    gw = FakeServer(gateway_style=True).start()
    lan_state = {"visible": False}
    lan = lambda: [{"ip": "127.0.0.1", "port": lan_srv.port, "server_id": SID}] if lan_state["visible"] else []
    for s in (lan_srv, gw):
        s.devices["a" * 16] = {"secret": "s" * 43, "status": "active"}
    c = DeviceCredentials("a" * 16, "s" * 43, approved=True)
    try:
        async with Rig(lan=lan, gateway_url=gw.base, creds=c, lan_upgrade_check_seconds=0.2) as r:
            await r.until("connected")
            assert r.client.connection_kind == "internet"
            lan_state["visible"] = True                    # phone walks onto the home Wi-Fi
            assert await wait_until(lambda: r.client.connection_kind == "lan", 6)
    finally:
        lan_srv.stop(); gw.stop()


async def test_unpair_forgets_server_and_identity(srv):
    async with Rig(gateway_url=gw_url(srv)) as r:
        await r.until("waiting_approval")
        await r.client.unpair()
        await r.until("pairing_required")
        assert r.pair.server_id is None and r.cred.creds is None
