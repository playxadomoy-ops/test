"""
Global application configuration and constants.

This module holds values that are shared across the whole application
(default intervals, storage keys, limits). Keeping them in one place
avoids "magic numbers" scattered through the codebase.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StorageKeys:
    """Keys used for persisted data in Flet's client storage."""

    SETTINGS: str = "air_alert.settings"
    CHANNELS: str = "air_alert.channels"
    REGION_STATE: str = "air_alert.region_state"
    LOG_ENTRIES: str = "air_alert.log_entries"
    SESSION_STRING: str = "air_alert.session_string"
    VOCABULARY_CACHE: str = "air_alert.vocabulary_cache"


@dataclass(frozen=True)
class Defaults:
    """Default values used when no persisted settings exist yet."""

    UPDATE_INTERVAL_SECONDS: int = 30
    MIN_UPDATE_INTERVAL_SECONDS: int = 10
    MAX_UPDATE_INTERVAL_SECONDS: int = 300
    MAX_LOG_ENTRIES: int = 500
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
