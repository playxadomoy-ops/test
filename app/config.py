"""
Global application configuration and constants.

This module holds values that are shared across the whole application
(default intervals, storage keys, limits). Keeping them in one place
avoids "magic numbers" scattered through the codebase.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class StorageKeys:
    """Keys used for persisted data in Flet's client storage.

    CHANNELS/SESSION_STRING/VOCABULARY_CACHE were used only for the
    mobile app's former direct Telegram connection and are no longer
    written -- left here (rather than deleted) only so an old value
    left over in a device's storage from a previous app version is
    simply never read again, not so it causes a KeyError.
    """

    SETTINGS: str = "air_alert.settings"
    CHANNELS: str = "air_alert.channels"
    REGION_STATE: str = "air_alert.region_state"
    LOG_ENTRIES: str = "air_alert.log_entries"
    SESSION_STRING: str = "air_alert.session_string"
    VOCABULARY_CACHE: str = "air_alert.vocabulary_cache"
    #: This device's server login credentials (device_id + token), set
    #: once automatic registration is approved -- see
    #: app/storage/device_store.py and app/services/server_client.py.
    #: Deliberately its own key, never mixed into SETTINGS: nothing in
    #: the UI ever displays or edits it.
    DEVICE_CREDENTIALS: str = "air_alert.device_credentials"
    #: Which server installation this phone is paired with (server_id from the pairing code / LAN discovery).
    PAIRED_SERVER: str = "air_alert.paired_server"


@dataclass(frozen=True)
class Defaults:
    """Default values used when no persisted settings exist yet."""

    UPDATE_INTERVAL_SECONDS: int = 30
    MIN_UPDATE_INTERVAL_SECONDS: int = 10
    MAX_UPDATE_INTERVAL_SECONDS: int = 300
    MAX_LOG_ENTRIES: int = 500
    #: Threat lifecycle history (appeared/moved/destroyed/all-clear) is
    #: never pruned the way ordinary diagnostic log lines are -- this is
    #: only a very generous safety cap against unbounded growth over
    #: months of uptime, not a rolling window like MAX_LOG_ENTRIES.
    MAX_HISTORY_ENTRIES: int = 10000
    AUTO_START_MONITORING: bool = True
    NOTIFICATIONS_ENABLED: bool = True
    ALERTS_API_TIMEOUT_SECONDS: int = 10


#: Official alerts.in.ua endpoint for per-oblast air-raid alert status.
#: NOTE: this endpoint requires a personal API token (Bearer auth),
#: obtained via https://alerts.in.ua/api-request — it is not anonymous.
#: The user enters their own token in Налаштування; without a token this
#: source is simply skipped and the app relies on Telegram-derived risk.
ALERTS_STATUS_URL = "https://api.alerts.in.ua/v1/iot/active_air_raid_alerts_by_oblast.json"

#: This endpoint's response is NOT a JSON list of objects -- it is a single
#: JSON string such as "ANNNNNNNNNNNANNNNNNNNNNNNNN", one character per
#: oblast, in this exact documented order (see https://devs.alerts.in.ua/,
#: section "/v1/iot/active_air_raid_alerts_by_oblast.json"). Each entry's
#: text matches a Region enum value exactly. A previous version of this
#: project incorrectly assumed a list-of-dicts shape, which silently
#: failed to parse on every real response and left the map showing stale
#: "all clear" state regardless of actual alerts -- see alert_service.py.
ALERTS_OBLAST_ORDER: tuple[str, ...] = (
    "Автономна Республіка Крим",
    "Волинська область",
    "Вінницька область",
    "Дніпропетровська область",
    "Донецька область",
    "Житомирська область",
    "Закарпатська область",
    "Запорізька область",
    "Івано-Франківська область",
    "м. Київ",
    "Київська область",
    "Кіровоградська область",
    "Луганська область",
    "Львівська область",
    "Миколаївська область",
    "Одеська область",
    "Полтавська область",
    "Рівненська область",
    "м. Севастополь",
    "Сумська область",
    "Тернопільська область",
    "Харківська область",
    "Херсонська область",
    "Хмельницька область",
    "Черкаська область",
    "Чернівецька область",
    "Чернігівська область",
)

STORAGE = StorageKeys()
DEFAULTS = Defaults()


@dataclass(frozen=True)
class GatewayConfig:
    """Where the Air Alert Internet Gateway lives. A PUBLIC hostname -- not a secret, no credentials here.

    The app no longer has ONE fixed server address (that would connect every customer to the same server).
    Instead each phone is paired with its own server_id and reaches it as ``<gateway>/s/<server_id>/...``
    (the gateway relays to that customer's PC over the PC's own outbound tunnel, so NAT/CGNAT/port-forwarding
    are irrelevant), or directly on the LAN when the PC is found there.

    HTTP_URL must be changed to your real gateway domain before building the APK (docs/DEPLOYMENT.md). While it
    still points at the reserved ".example" placeholder the app reports "gateway not configured" instead of
    pretending to be online.
    """

    HTTP_URL: str = "https://gateway.airalertanalyzer.example"
    #: Developer/test override (e.g. http://192.168.1.10:8080), same idea as the old dev override.
    OVERRIDE_ENV_VAR: str = "AIR_ALERT_GATEWAY_HTTP_URL"
    LAN_DISCOVERY_PORT: int = 37020


GATEWAY_CONFIG = GatewayConfig()


def get_gateway_base_url() -> str:
    override = os.environ.get(GATEWAY_CONFIG.OVERRIDE_ENV_VAR, "").strip()
    return (override or GATEWAY_CONFIG.HTTP_URL).rstrip("/")
