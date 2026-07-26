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
    #: Region.value strings the user wants highlighted/filterable on the
    #: "Рух загроз" map (e.g. only Київська + Черкаська). Empty means no
    #: filter is configured -- the map keeps showing the whole country.
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
        )

    @staticmethod
    def default() -> "AppSettings":
        """Return a fresh settings instance with default values."""
        return AppSettings()
