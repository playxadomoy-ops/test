"""Data model describing persisted user/app settings."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import DEFAULTS


@dataclass(slots=True)
class AppSettings:
    """All user-configurable settings, persisted locally."""

    api_id: str = ""
    api_hash: str = ""
    alerts_api_token: str = ""
    update_interval_seconds: int = DEFAULTS.UPDATE_INTERVAL_SECONDS
    auto_start_monitoring: bool = DEFAULTS.AUTO_START_MONITORING
    notifications_enabled: bool = DEFAULTS.NOTIFICATIONS_ENABLED
    #: Air Alert Analyzer SERVER connection (the centralized backend --
    #: see app/services/server_client.py). Entirely separate from the
    #: Telegram api_id/api_hash above: those are this device's OWN
    #: Telegram API credentials for local monitoring, while these are
    #: just this device's login for the already-running server, which
    #: never receives or exposes Telegram credentials of any kind.
    server_url: str = ""
    server_device_id: str = ""
    server_token: str = ""
    server_enabled: bool = False
    #: Region.value strings the user has selected as their "regions of
    #: interest" -- editable both as chips on the "Рух загроз" map (which
    #: filters/zooms to just this selection) and as checkboxes in
    #: Налаштування's "Сповіщення за областями" section. The same list
    #: also tells AlertService which regions should trigger a sound
    #: notification when they enter alert (see
    #: AlertService.set_watched_regions / main.py's
    #: handle_region_alert_triggered). Empty means no map filter and no
    #: region-triggered notifications.
    watched_regions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialize to a plain dict for JSON storage."""
        return {
            "api_id": self.api_id,
            "api_hash": self.api_hash,
            "alerts_api_token": self.alerts_api_token,
            "update_interval_seconds": self.update_interval_seconds,
            "auto_start_monitoring": self.auto_start_monitoring,
            "notifications_enabled": self.notifications_enabled,
            "watched_regions": list(self.watched_regions),
            "server_url": self.server_url,
            "server_device_id": self.server_device_id,
            "server_token": self.server_token,
            "server_enabled": self.server_enabled,
        }


    @staticmethod
    def from_dict(data: dict) -> "AppSettings":
        """Deserialize from a plain dict loaded from JSON storage."""
        return AppSettings(
            api_id=str(data.get("api_id", "")),
            api_hash=str(data.get("api_hash", "")),
            alerts_api_token=str(data.get("alerts_api_token", "")),
            update_interval_seconds=int(
                data.get("update_interval_seconds", DEFAULTS.UPDATE_INTERVAL_SECONDS)
            ),
            auto_start_monitoring=bool(
                data.get("auto_start_monitoring", DEFAULTS.AUTO_START_MONITORING)
            ),
            notifications_enabled=bool(
                data.get("notifications_enabled", DEFAULTS.NOTIFICATIONS_ENABLED)
            ),
            watched_regions=[str(r) for r in data.get("watched_regions", []) or []],
            server_url=str(data.get("server_url", "")),
            server_device_id=str(data.get("server_device_id", "")),
            server_token=str(data.get("server_token", "")),
            server_enabled=bool(data.get("server_enabled", False)),
        )

    @staticmethod
    def default() -> "AppSettings":
        """Return a fresh settings instance with default values."""
        return AppSettings()
