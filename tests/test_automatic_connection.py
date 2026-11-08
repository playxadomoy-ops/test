"""Automatic first-launch connection: no code dialog when the server can be found; persistence; JWT renewal; log hygiene."""
from __future__ import annotations

import asyncio
import json

import pytest

from app.services.server_client import ServerClient
from app.storage.device_store import DeviceCredentialStore, DeviceCredentials

from .fakes import SID, FakeLogger, FakeServer, MemCredStore, MemPairStore, wait_until
from .test_client import Rig


async def test_no_code_dialog_when_the_server_is_found_by_discovery_even_if_the_first_probe_misses():
    """REGRESSION: pairing_required was published BEFORE the first discovery attempt, so the app asked for a code on networks
    where it would have paired itself a moment later (UDP broadcast often misses once on Wi-Fi)."""
    s = FakeServer(gateway_style=False).start()
    calls = {"n": 0}

    def discover():
        calls["n"] += 1
        return [] if calls["n"] == 1 else [{"ip": "127.0.0.1", "port": s.port, "server_id": SID}]
    try:
        async with Rig(paired=None, lan=discover, gateway_url="http://127.0.0.1:1", discovery_grace_seconds=3.0) as r:
            await r.until("waiting_approval")                     # it paired and registered by itself
            assert "pairing_required" not in r.statuses, r.statuses   # the code dialog was NEVER requested
            assert r.pair.server_id == SID and calls["n"] >= 2
    finally:
        s.stop()


async def test_code_is_requested_only_when_discovery_genuinely_finds_nothing():
    async with Rig(paired=None, lan=lambda: [], discovery_grace_seconds=0.4) as r:
        await r.until("pairing_required")
        assert r.statuses[0] != "pairing_required"                # discovery was attempted first


# ============================================================ persistence with the REAL storage classes

from app.storage.device_store import PairedServerStore  # noqa: E402

from .fakes import FakeClientStorage, FakePage, RecLogger  # noqa: E402


def launch(storage, server, logger=None, **kw):
    """One 'app launch': NEW store/client objects over the SAME persistent app data (as after killing and reopening the app)."""
    page = FakePage(storage)
    client = ServerClient(logger or FakeLogger(), DeviceCredentialStore(page), PairedServerStore(page),
                          poll_interval_seconds=0.03, gateway_url=server.base, discover=lambda: [],
                          base_retry_delay=0.05, registration_poll_seconds=0.03, discovery_grace_seconds=0.2, **kw)
    seen: list[str] = []
    client.set_status_listener(lambda c, m: seen.append(c))
    return client, seen


async def run_until(client, seen, status, timeout=8.0):
    task = asyncio.create_task(client.start("Phone"))
    assert await wait_until(lambda: status in seen, timeout), seen
    return task


async def close(client, task):
    await client.stop()
    try:
        await asyncio.wait_for(task, 5)
    except Exception:
        task.cancel()


async def test_first_launch_creates_one_persistent_identity_and_later_launches_reuse_it():
    srv = FakeServer(gateway_style=True).start()
    storage = FakeClientStorage({"air_alert.paired_server": json.dumps({"server_id": SID})})   # server known (provisioned/paired)
    try:
        c1, seen1 = launch(storage, srv)
        t = await run_until(c1, seen1, "waiting_approval")
        first_id = c1.device_id
        assert first_id and srv.register_calls == 1
        srv.approve_all()
        assert await wait_until(lambda: "connected" in seen1)
        await close(c1, t)

        for launch_no in (2, 3):                                  # app killed and reopened twice
            c, seen = launch(storage, srv)
            t = await run_until(c, seen, "connected")
            assert c.device_id == first_id, "the device id must never change between launches"
            assert "registering" not in seen and "waiting_approval" not in seen
            await close(c, t)
        assert srv.register_calls == 1 and len(srv.devices) == 1  # registered exactly once, ever
    finally:
        srv.stop()


async def test_credentials_saved_by_the_previous_app_version_still_work():
    """Old versions stored {device_id, token} without an 'approved' flag (they only saved AFTER approval)."""
    srv = FakeServer(gateway_style=True).start()
    srv.devices["a" * 16] = {"secret": "s" * 43, "status": "active"}
    storage = FakeClientStorage({"air_alert.device_credentials": json.dumps({"device_id": "a" * 16, "token": "s" * 43}),
                                 "air_alert.paired_server": json.dumps({"server_id": SID})})
    try:
        c, seen = launch(storage, srv)
        t = await run_until(c, seen, "connected")
        assert "registering" not in seen and srv.register_calls == 0
        await close(c, t)
    finally:
        srv.stop()


@pytest.mark.parametrize("garbage", ['"just a string"', "[]", json.dumps({"device_id": "x"}), json.dumps({"token": "y"}),
                                     json.dumps({"device_id": "", "token": ""}), "null"])
async def test_corrupted_stored_credentials_lead_to_one_fresh_identity_not_a_crash(garbage):
    srv = FakeServer(gateway_style=True).start()
    storage = FakeClientStorage({"air_alert.device_credentials": garbage,
                                 "air_alert.paired_server": json.dumps({"server_id": SID})})
    try:
        c, seen = launch(storage, srv)
        t = await run_until(c, seen, "waiting_approval")
        assert len(srv.devices) == 1
        saved = json.loads(storage.data["air_alert.device_credentials"])
        assert saved["device_id"] == c.device_id and len(saved["token"]) >= 32      # replaced by a valid persisted identity
        await close(c, t)
    finally:
        srv.stop()


# ============================================================ JWT lifecycle

async def test_expired_jwt_mid_session_is_renewed_silently_without_re_registering():
    srv = FakeServer(gateway_style=True).start()
    storage = FakeClientStorage({"air_alert.paired_server": json.dumps({"server_id": SID})})
    try:
        c, seen = launch(storage, srv)
        t = await run_until(c, seen, "waiting_approval")
        srv.approve_all()
        assert await wait_until(lambda: "connected" in seen)
        logins, regs = srv.login_calls, srv.register_calls
        seen.clear()
        srv.expire_jwt_n = 2                                      # the next two data requests answer 401 (expired/invalid JWT)
        assert await wait_until(lambda: srv.login_calls > logins, 5)          # it logged in AGAIN by itself...
        await asyncio.sleep(0.3)
        assert srv.register_calls == regs and c.is_connected       # ...without re-registering, and is connected again
        assert "rejected" not in seen and "pairing_required" not in seen
        await close(c, t)
    finally:
        srv.stop()


# ============================================================ log hygiene

async def test_no_credential_ever_appears_in_the_application_log():
    srv = FakeServer(gateway_style=True).start()
    storage = FakeClientStorage({"air_alert.paired_server": json.dumps({"server_id": SID})})
    log = RecLogger()
    try:
        c, seen = launch(storage, srv, logger=log)
        t = await run_until(c, seen, "waiting_approval")
        srv.approve_all()
        assert await wait_until(lambda: "connected" in seen)
        srv.expire_jwt_n = 1
        await asyncio.sleep(0.4)
        secret = json.loads(storage.data["air_alert.device_credentials"])["token"]
        await close(c, t)
        text = "\n".join(log.lines)
        assert log.lines, "the client should have logged something for this test to be meaningful"
        for sensitive in (secret, "jwt-secret-value", "Bearer", c.device_id):
            assert sensitive not in text, f"leaked into the log: {sensitive[:8]}..."
    finally:
        srv.stop()
