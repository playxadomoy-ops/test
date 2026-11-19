"""C1 client side: Worker discovery, URL validation, caching, fallbacks. Real HTTP against a local fake Worker; the
certificate-pinned probe is stubbed per URL (its real behaviour is covered by test_client/integration tests)."""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from app.provisioning import _validate
from app.services.bootstrap import GatewayBootstrap, is_usable_bootstrap_url
from app.services.server_client import ServerClient
from app.services.tunnel_url import validate_tunnel_url
from app.storage.device_store import GatewayUrlStore

from .fakes import SID, FakeClientStorage, FakeLogger, FakePage, MemCredStore, MemPairStore, RecLogger, free_port

PEM = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
T1, T2, T3 = ("https://aaa-one.trycloudflare.com", "https://bbb-two.trycloudflare.com", "https://ccc-three.trycloudflare.com")


class FakeWorker:
    """Serves GET /v1/gateway with a programmable body/status. Counts requests."""

    def __init__(self) -> None:
        self.status, self.body, self.raw, self.headers, self.hits = 200, {"v": 1, "url": T1}, None, {}, 0
        self.port = free_port()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): ...

            def do_GET(self):
                outer.hits += 1
                data = outer.raw if outer.raw is not None else json.dumps(outer.body).encode()
                self.send_response(outer.status)
                for k, v in outer.headers.items():
                    self.send_header(k, v)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:   # real code requires https for the bootstrap URL; tests reach the fake over http via a patched check
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


class MemUrlCache:
    def __init__(self, url=None) -> None:
        self.url, self.saves = url, 0

    async def load(self):
        return self.url

    async def save(self, url):
        self.url, self.saves = url, self.saves + 1


@pytest.fixture()
def worker():
    w = FakeWorker()
    yield w
    w.stop()


def make_client(worker_url, cache, outcomes, validator=lambda u: u, **kw):
    """ServerClient whose gateway probe answers ``outcomes[gateway_base]`` (default 'unreachable') and records the order."""
    prov = _validate({"server_id": SID, "server_cert_pem": PEM, "enroll_key": "k" * 40, "gateway_url": "",
                      "bootstrap_url": "https://boot.example.org"})
    boot = GatewayBootstrap("https://boot.example.org", cache, validator=validator)
    client = ServerClient(RecLogger() if kw.pop("rec", False) else FakeLogger(), MemCredStore(), MemPairStore(SID),
                          discover=lambda: [], provisioning=prov, bootstrap=boot, **kw)
    boot._url = worker_url            # AFTER construction: production requires https, the local fake speaks http
    probed: list[str] = []

    async def probe(_c, base, _sid):
        gw = base.rsplit("/s/", 1)[0]
        probed.append(gw)
        return outcomes.get(gw, "unreachable")
    client._probe = probe
    statuses: list[str] = []
    client.set_status_listener(lambda c, m: statuses.append(c))
    return client, probed, statuses


async def resolve(client):
    async with httpx.AsyncClient() as http:
        return await client._resolve_route(http, SID)


# ------------------------------------------------------------------ URL validation (mirrors the Worker)

@pytest.mark.parametrize("url", [T1, "https://Quiet-River-1.TryCloudflare.com/", "https://a1.trycloudflare.com"])
def test_valid_tunnel_urls(url):
    assert validate_tunnel_url(url) == url.lower().rstrip("/")


@pytest.mark.parametrize("url", [
    "http://x-y.trycloudflare.com", "https://evil.com", "https://x-y.trycloudflare.com.evil.com",
    "https://evil.com/x-y.trycloudflare.com", "https://x-y.trycloudflare.com@evil.com", "https://u@x-y.trycloudflare.com",
    "https://a.b.trycloudflare.com", "https://trycloudflare.com", "https://api.trycloudflare.com",
    "https://x-y.trycloudflare.com:8443", "https://x-y.trycloudflare.com:443", "https://x-y.trycloudflare.com/p",
    "https://x-y.trycloudflare.com/?q=1", "https://x-y.trycloudflare.com/#f", "https://-bad.trycloudflare.com",
    "https://x_y.trycloudflare.com", "https://x-y.trycloudflare.com\n", "https://x-y.trycloudflare.com\\.evil.com",
    "http://127.0.0.1:8080", "javascript:alert(1)", "", "x" * 300, None, 42, {}, ["https://a-b.trycloudflare.com"],
])
def test_invalid_tunnel_urls(url):
    assert validate_tunnel_url(url) is None


def test_bootstrap_url_must_be_real_https():
    assert is_usable_bootstrap_url("https://air.me.workers.dev")
    for bad in ("http://air.me.workers.dev", "https://gateway.example", "https://x.example", "ftp://a.b", "", "https://"):
        assert not is_usable_bootstrap_url(bad), bad


# ------------------------------------------------------------------ provisioning bundle

GOOD = {"server_id": "abcd-efgh-jkmn", "server_cert_pem": PEM, "enroll_key": "", "gateway_url": "",
        "bootstrap_url": "https://air.me.workers.dev/"}


def test_bundle_with_bootstrap_needs_no_static_gateway():
    p = _validate(GOOD)
    assert p.bootstrap_url == "https://air.me.workers.dev" and p.gateway_url == "" and p.is_public
    assert _validate({k: v for k, v in GOOD.items() if k != "gateway_url"}).gateway_url == ""


@pytest.mark.parametrize("patch", [{"bootstrap_url": "http://air.me.workers.dev"}, {"bootstrap_url": "https://g.example"},
                                   {"bootstrap_url": 5}, {"bootstrap_url": ""}])   # last: empty gateway AND no bootstrap
def test_bad_bootstrap_bundles_rejected(patch):
    assert _validate({**GOOD, **patch}) is None


# ------------------------------------------------------------------ resolution behaviour

async def test_worker_url_is_used_cached_and_verified(worker):
    worker.body = {"v": 1, "url": T1}
    cache = MemUrlCache()
    client, probed, _ = make_client(worker.url, cache, {T1: "ok"})
    assert await resolve(client) == (f"{T1}/s/{SID}", "internet")
    assert probed == [T1] and cache.url == T1


async def test_tunnel_url_change_is_followed_automatically(worker):
    cache = MemUrlCache(T1)                                  # phone remembers the old tunnel
    worker.body = {"v": 1, "url": T2}                        # PC restarted cloudflared: new URL
    client, probed, _ = make_client(worker.url, cache, {T1: "offline", T2: "ok"})
    assert (await resolve(client))[0] == f"{T2}/s/{SID}"
    assert probed == [T2] and cache.url == T2                # fresh URL first; the stale cache was not even needed


async def test_worker_unreachable_falls_back_to_cached_url(worker):
    cache = MemUrlCache(T1)
    worker.stop()                                            # Worker/workers.dev unreachable
    client, probed, _ = make_client(worker.url, cache, {T1: "ok"})
    assert (await resolve(client))[0] == f"{T1}/s/{SID}" and probed == [T1]


async def test_worker_unreachable_and_nothing_cached_reports_gateway_unavailable_then_recovers(worker):
    cache = MemUrlCache()
    worker.status = 503
    client, probed, statuses = make_client(worker.url, cache, {T1: "ok"})
    assert await resolve(client) is None and probed == [] and statuses[-1] == "gateway_unavailable"
    worker.status = 200                                      # Worker is back
    assert (await resolve(client))[0] == f"{T1}/s/{SID}"


async def test_unregistered_worker_404_is_handled(worker):
    worker.status, worker.body = 404, {"error": "not_registered"}
    client, probed, statuses = make_client(worker.url, MemUrlCache(), {})
    assert await resolve(client) is None and statuses[-1] == "gateway_unavailable" and probed == []


EVIL_ANSWERS = [
    {"v": 1, "url": "https://evil.example.com"}, {"v": 1, "url": "http://aaa-one.trycloudflare.com"},
    {"v": 1, "url": "https://aaa-one.trycloudflare.com:8443"}, {"v": 1, "url": "https://aaa-one.trycloudflare.com/x"},
    {"v": 1, "url": "https://aaa-one.trycloudflare.com@evil.com"}, {"v": 1, "url": "https://api.trycloudflare.com"},
    {"v": 2, "url": T1}, {"url": T1}, {"v": 1}, {"v": 1, "url": None}, {"v": 1, "url": ["x"]}, [T1], "text", 42,
]


@pytest.mark.parametrize("answer", EVIL_ANSWERS)
async def test_invalid_worker_answers_are_never_probed(worker, answer):
    worker.body = answer
    client, probed, _ = make_client(worker.url, MemUrlCache(), {}, validator=validate_tunnel_url)   # PRODUCTION validator
    assert await resolve(client) is None and probed == []


@pytest.mark.parametrize("raw", [b"not json", b"", b"{" * 10, b"x" * 100000])
async def test_garbage_and_oversized_worker_bodies_are_ignored(worker, raw):
    worker.raw = raw
    client, probed, _ = make_client(worker.url, MemUrlCache(), {T1: "ok"}, validator=validate_tunnel_url)
    assert await resolve(client) is None and probed == []


async def test_oversized_but_valid_json_is_ignored(worker):
    worker.body = {"v": 1, "url": T1, "pad": "x" * 10000}
    client, probed, _ = make_client(worker.url, MemUrlCache(), {T1: "ok"}, validator=validate_tunnel_url)
    assert await resolve(client) is None and probed == []


async def test_worker_redirect_is_not_followed(worker):
    worker.status, worker.headers = 302, {"location": "https://evil.example.com/v1/gateway"}
    client, probed, _ = make_client(worker.url, MemUrlCache(), {}, validator=validate_tunnel_url)
    assert await resolve(client) is None and probed == []


async def test_poisoned_cache_is_ignored_and_never_probed(worker):
    worker.status = 503
    for poison in ("https://evil.example.com", "http://127.0.0.1:1", "garbage", "https://x.trycloudflare.com/a"):
        client, probed, _ = make_client(worker.url, MemUrlCache(poison), {}, validator=validate_tunnel_url)
        assert await resolve(client) is None and probed == [], poison


async def test_wrong_server_behind_worker_url_is_rejected_and_not_cached(worker):
    """The Worker is a hint, not an identity proof: a URL whose server fails the pinned handshake is never used or remembered."""
    cache = MemUrlCache()
    worker.body = {"v": 1, "url": T1}
    client, probed, statuses = make_client(worker.url, cache, {T1: "mismatch"})
    assert await resolve(client) is None and cache.url is None and statuses[-1] == "server_offline"


async def test_dead_fresh_url_then_good_cached_url(worker):
    cache = MemUrlCache(T2)
    worker.body = {"v": 1, "url": T1}                        # Worker still lists a stale URL (KV propagation lag)
    client, probed, _ = make_client(worker.url, cache, {T1: "unreachable", T2: "ok"})
    assert (await resolve(client))[0] == f"{T2}/s/{SID}" and probed == [T1, T2]


async def test_tunnel_down_530_is_reported_as_server_offline_not_gateway_error(worker):
    worker.body = {"v": 1, "url": T1}
    client, _, statuses = make_client(worker.url, MemUrlCache(), {T1: "offline"})
    assert await resolve(client) is None and statuses[-1] == "server_offline"


async def test_no_duplicate_probe_when_cache_equals_worker_answer(worker):
    worker.body = {"v": 1, "url": T1}
    client, probed, _ = make_client(worker.url, MemUrlCache(T1), {T1: "unreachable"})
    assert await resolve(client) is None and probed == [T1]


async def test_lan_still_wins_and_the_worker_is_not_even_asked(worker):
    client, probed, _ = make_client(worker.url, MemUrlCache(), {})
    client._discover = lambda: [{"ip": "10.0.0.5", "port": 8765, "server_id": SID}]

    async def probe(_c, base, _sid):
        probed.append(base)
        return "ok"
    client._probe = probe
    assert await resolve(client) == ("http://10.0.0.5:8765", "lan")
    assert worker.hits == 0


async def test_without_bootstrap_behaviour_is_unchanged():
    prov = _validate({"server_id": SID, "server_cert_pem": PEM, "enroll_key": "k", "gateway_url": "https://gateway.airalertanalyzer.example"})
    client = ServerClient(FakeLogger(), MemCredStore(), MemPairStore(SID), discover=lambda: [], provisioning=prov)
    seen = []
    client.set_status_listener(lambda c, m: seen.append(c))
    assert await resolve(client) is None and seen[-1] == "gateway_unconfigured"
    prov2 = _validate({"server_id": SID, "server_cert_pem": PEM, "enroll_key": "k", "gateway_url": "https://gw.example.org"})
    client2 = ServerClient(FakeLogger(), MemCredStore(), MemPairStore(SID), discover=lambda: [], provisioning=prov2)
    probed = []

    async def probe(_c, base, _sid):
        probed.append(base)
        return "ok"
    client2._probe = probe
    assert (await resolve(client2))[0] == f"https://gw.example.org/s/{SID}" and probed == [f"https://gw.example.org/s/{SID}"]


async def test_static_gateway_is_a_last_resort_next_to_bootstrap(worker):
    worker.status = 503
    prov = _validate({"server_id": SID, "server_cert_pem": PEM, "enroll_key": "k", "gateway_url": "https://gw.example.org",
                      "bootstrap_url": "https://boot.example.org"})
    boot = GatewayBootstrap("https://boot.example.org", MemUrlCache())
    client = ServerClient(FakeLogger(), MemCredStore(), MemPairStore(SID), discover=lambda: [], provisioning=prov, bootstrap=boot)
    boot._url = worker.url
    probed = []

    async def probe(_c, base, _sid):
        probed.append(base)
        return "ok"
    client._probe = probe
    assert (await resolve(client))[0] == f"https://gw.example.org/s/{SID}"


# ------------------------------------------------------------------ persistence with the REAL store class

async def test_gateway_url_store_round_trip_and_corruption():
    storage = FakeClientStorage()
    store = GatewayUrlStore(FakePage(storage))
    assert await store.load() is None
    await store.save(T1)
    assert await GatewayUrlStore(FakePage(storage)).load() == T1        # survives "app restart"
    storage.data["air_alert.gateway_cache"] = json.dumps("not a dict")
    assert await store.load() is None
    storage.data["air_alert.gateway_cache"] = json.dumps({"url": 5})
    assert await store.load() is None
    await store.clear()
    assert await store.load() is None


async def test_remember_writes_only_when_changed():
    cache = MemUrlCache(T1)
    boot = GatewayBootstrap("https://boot.example.org", cache)
    await boot.remember(T1)
    assert cache.saves == 0
    await boot.remember(T2)
    assert cache.saves == 1 and cache.url == T2


async def test_a_new_phone_with_only_a_bootstrap_url_builds_a_working_client():
    """Production wiring path: provisioning + GatewayUrlStore, no injected bootstrap."""
    prov = _validate(GOOD)
    client = ServerClient(FakeLogger(), MemCredStore(), MemPairStore(SID), discover=lambda: [], provisioning=prov,
                          gateway_cache=GatewayUrlStore(FakePage(FakeClientStorage())))
    assert client._bootstrap is not None and client._bootstrap.configured
