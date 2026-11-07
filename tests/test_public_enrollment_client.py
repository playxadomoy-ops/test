from __future__ import annotations

import asyncio
import hashlib
import threading
import time

import pytest

from app.provisioning import _validate
from app.services import pow as pow_solver
from app.services.server_client import ServerClient, _is_transient_403

from .fakes import FakeLogger, MemCredStore, MemPairStore

PEM = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
PUBLIC = {"server_id": "ABCDEFGHJKMN", "server_cert_pem": PEM, "enroll_key": "", "gateway_url": "https://gw.example.org"}


def bits_ok(challenge, device, nonce, bits):
    d = hashlib.sha256(f"{challenge}|{device}|{nonce}".encode()).digest()   # independent re-implementation of the rule
    return int.from_bytes(d, "big") >> (256 - bits) == 0


def test_solver_satisfies_the_documented_rule_and_binds_to_the_device():
    n = pow_solver.solve("v1.1.12.abc.mac", "dev-A", 12)
    assert bits_ok("v1.1.12.abc.mac", "dev-A", n, 12) and pow_solver.verify("v1.1.12.abc.mac", "dev-A", n, 12)


def test_a_solution_for_one_device_almost_never_works_for_another():
    wrong = sum(pow_solver.verify("c", f"other-{i}", pow_solver.solve("c", "dev-A", 14), 14) for i in range(50))
    assert wrong <= 1


def test_solver_can_be_cancelled():
    stop = threading.Event()
    stop.set()
    with pytest.raises(InterruptedError):
        pow_solver.solve("c", "d", 40, stop)          # impossible puzzle: must stop immediately, not spin forever


async def test_solving_does_not_freeze_the_ui_loop():
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1
    t = asyncio.create_task(ticker())
    await asyncio.to_thread(pow_solver.solve, "c", "d", 18)       # ~0.3 s of CPU in a worker thread
    t.cancel()
    assert ticks >= 5                                              # the event loop kept running meanwhile


class Resp:
    def __init__(self, status, body=None):
        self.status_code, self._b = status, body

    def json(self):
        if self._b is None:
            raise ValueError
        return self._b


class FakeApi:
    def __init__(self, resp):
        self.resp = resp

    async def get(self, url, **kw):
        return self.resp


def client(provider=None):
    c = ServerClient(FakeLogger(), MemCredStore(), MemPairStore(None), discover=lambda: [], provisioning=_validate(PUBLIC),
                     attestation_provider=provider)
    c.server_base_url = "https://gw/s/x"
    return c


async def test_hostile_server_cannot_pin_the_phone_cpu_with_absurd_difficulty():
    for bits in (40, 64, 4):
        assert await client()._public_proof(FakeApi(Resp(200, {"challenge": "c", "difficulty": bits})), "d") is None


async def test_normal_challenge_is_solved_and_returned_in_wire_format():
    p = await client()._public_proof(FakeApi(Resp(200, {"challenge": "v1.1.10.r.m", "difficulty": 10})), "dev-1")
    assert p["pow_challenge"] == "v1.1.10.r.m" and bits_ok("v1.1.10.r.m", "dev-1", p["pow_nonce"], 10)
    assert "attestation" not in p                                  # no provider in the current Flet app


async def test_server_not_in_public_mode_means_register_without_proof():
    assert await client()._public_proof(FakeApi(Resp(404)), "d") == {}


async def test_busy_or_malformed_challenge_is_retried_later_not_fatal():
    assert await client()._public_proof(FakeApi(Resp(429)), "d") is None
    assert await client()._public_proof(FakeApi(Resp(200, {"nope": 1})), "d") is None


async def test_attestation_provider_is_used_when_present_and_a_crash_never_blocks_enrollment():
    async def good(device, challenge):
        return f"TOKEN-for-{device}"

    async def broken(device, challenge):
        raise RuntimeError("play services unavailable")
    api = FakeApi(Resp(200, {"challenge": "v1.1.10.r.m", "difficulty": 10}))
    assert (await client(good)._public_proof(api, "dev-1"))["attestation"] == "TOKEN-for-dev-1"
    assert "attestation" not in await client(broken)._public_proof(api, "dev-1")


@pytest.mark.parametrize("detail,transient", [("Недійсне підтвердження роботи (expired).", True),
                                              ("Недійсне підтвердження роботи (replayed).", True),
                                              ("Недійсне підтвердження роботи (busy).", True),
                                              ("Недійсне підтвердження роботи (bad_solution).", False),
                                              ("Недійсне підтвердження роботи (forged).", False),
                                              ("Атестацію не пройдено (wrong_package).", False)])
def test_only_challenge_timing_failures_are_retried_everything_else_is_final(detail, transient):
    assert _is_transient_403(Resp(403, {"detail": detail})) is transient
