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
class ServerConfig:
    """The Air Alert Analyzer server's stable Internet endpoint.

    Architecture note: the app connects to a FIXED, known server
    address over the Internet -- it no longer searches the local
    network for it (see app/services/server_client.py). A normal user
    never sees or edits this value; it just needs to be correct in the
    build you ship.

    PRODUCTION_URL must be a stable hostname/domain you control (with a
    real TLS certificate for HTTPS in production), e.g.
    "https://api.yourdomain.com" -- NOT a home PC's IP address, which
    changes and typically isn't reachable from the public Internet at
    all without port forwarding (see the server README's deployment
    section). A bare "http://" URL still works for local development
    (see DEV override below) or for a self-hosted server you've put
    behind your own TLS-terminating reverse proxy at a domain you
    control; it is not a safe default for a real deployment.
    """

    PRODUCTION_URL: str = "https://api.airalertanalyzer.example"

    #: Name of the environment variable that overrides PRODUCTION_URL,
    #: for local development only -- e.g. running `python main.py` on a
    #: desktop pointed at a server on your LAN or a dev VPS:
    #:   AIR_ALERT_SERVER_URL=http://192.168.1.50:8765 python main.py
    #: This has no effect unless you explicitly set it in your own
    #: shell/build environment -- a normal built APK never has it set,
    #: so it always falls back to PRODUCTION_URL. This is the "safe
    #: developer configuration" the normal user is never exposed to.
    DEV_OVERRIDE_ENV_VAR: str = "AIR_ALERT_SERVER_URL"


SERVER_CONFIG = ServerConfig()


def get_server_base_url() -> str:
    """Return the server URL this app should connect to.

    Checks the developer-only environment override first, falls back
    to the production constant -- see ServerConfig's docstring.
    """
    override = os.environ.get(SERVER_CONFIG.DEV_OVERRIDE_ENV_VAR, "").strip()
    return override or SERVER_CONFIG.PRODUCTION_URL
