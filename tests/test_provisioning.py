from __future__ import annotations

import json

import pytest

from app.provisioning import ENV_OVERRIDE, _validate, load_provisioning
from app.services.server_client import ServerClient
from app.storage.device_store import DeviceCredentials

from .fakes import FakeLogger, MemCredStore, MemPairStore

PEM = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
GOOD = {"server_id": "abcd-efgh-jkmn", "server_cert_pem": PEM, "enroll_key": "k" * 40, "gateway_url": "https://gw.example.org/"}


def test_valid_bundle_is_normalized():
    p = _validate(GOOD)
    assert p.server_id == "ABCDEFGHJKMN" and p.gateway_url == "https://gw.example.org" and p.enroll_key == "k" * 40


@pytest.mark.parametrize("patch", [{"server_id": "short"}, {"server_cert_pem": "not a cert"}, 
                                   {"gateway_url": "ftp://x"}, {"gateway_url": ""}, {"enroll_key": 5}])
def test_invalid_bundles_are_rejected(patch):  # note: an EMPTY key is VALID -- that is a public (Google Play) build
    assert _validate({**GOOD, **patch}) is None
    assert _validate("nope") is None and _validate(None) is None


def test_env_override_and_absent_bundle(tmp_path, monkeypatch):
    f = tmp_path / "b.json"
    f.write_text(json.dumps(GOOD))
    monkeypatch.setenv(ENV_OVERRIDE, str(f))
    assert load_provisioning().server_id == "ABCDEFGHJKMN"
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "missing.json"))
    assert load_provisioning() is None
    monkeypatch.delenv(ENV_OVERRIDE)
    assert load_provisioning() is None            # no generated module in the source tree => unprovisioned build


async def test_provisioned_build_never_asks_for_pairing_and_pins_the_server():
    cred, pair = MemCredStore(), MemPairStore(None)
    c = ServerClient(FakeLogger(), cred, pair, discover=lambda: [], provisioning=_validate(GOOD))
    assert await c._ensure_paired() == "ABCDEFGHJKMN"      # no dialog, no typing, no LAN needed
    assert pair.server_id == "ABCDEFGHJKMN"


async def test_credentials_from_a_different_server_are_never_reused():
    cred = MemCredStore()
    cred.creds = DeviceCredentials("a" * 16, "s" * 43, approved=True)
    pair = MemPairStore("ZZZZZZZZZZZZ")                    # phone used to belong to ANOTHER installation
    c = ServerClient(FakeLogger(), cred, pair, discover=lambda: [], provisioning=_validate(GOOD))
    await c._ensure_paired()
    assert cred.creds is None and pair.server_id == "ABCDEFGHJKMN"


async def test_same_server_keeps_existing_identity_so_upgrade_does_not_reenroll():
    cred = MemCredStore()
    cred.creds = DeviceCredentials("a" * 16, "s" * 43, approved=True)
    c = ServerClient(FakeLogger(), cred, MemPairStore("ABCDEFGHJKMN"), discover=lambda: [], provisioning=_validate(GOOD))
    await c._ensure_paired()
    assert cred.creds is not None


def test_status_row_knows_every_required_state():
    from app.ui.components.server_status_row import _STATUS_STYLE as style
    for code in ("initializing", "registering", "connecting", "connected", "reconnecting", "server_offline",
                 "gateway_unavailable", "rejected"):
        assert code in style, code


def test_public_bundle_carries_no_secret():
    p = _validate({**GOOD, "enroll_key": ""})
    assert p is not None and p.is_public and p.enroll_key == ""
    assert _validate({k: v for k, v in GOOD.items() if k != "enroll_key"}).is_public
    assert not _validate(GOOD).is_public
