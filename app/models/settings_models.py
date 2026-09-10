"""Data model describing persisted user/app settings.

Note: this model no longer carries any Telegram credentials (API ID,
API HASH, session string, phone) -- that integration lives entirely in
the separate server application. It also no longer carries any manual
server-connection fields (address, Device ID, token, an "enabled"
flag): the server connection is now fully automatic (LAN discovery +
self-registration, see app/services/server_client.py) and this
device's credentials, once obtained, are stored separately in
app/storage/device_store.py -- deliberately NOT here, since nothing in
the UI ever displays or edits them anymore.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import DEFAULTS


@dataclass(slots=True)
class AppSettings:
    """All user-configurable settings, persisted locally."""

    alerts_api_token: str = ""
    update_interval_seconds: int = DEFAULTS.UPDATE_INTERVAL_SECONDS
    auto_start_monitoring: bool = DEFAULTS.AUTO_START_MONITORING
    notifications_enabled: bool = DEFAULTS.NOTIFICATIONS_ENABLED
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
            "alerts_api_token": self.alerts_api_token,
            "update_interval_seconds": self.update_interval_seconds,
            "auto_start_monitoring": self.auto_start_monitoring,
            "notifications_enabled": self.notifications_enabled,
            "watched_regions": list(self.watched_regions),
        }

    @staticmethod
    def from_dict(data: dict) -> "AppSettings":
        """Deserialize from a plain dict loaded from JSON storage.

        Silently ignores now-obsolete keys (``api_id``/``api_hash`` from
        the old direct-Telegram build, ``server_url``/``server_device_id``/
        ``server_token``/``server_enabled`` from the old manual-connection
        build) that may still be present in a settings blob persisted by
        an older app version -- so upgrading never crashes on a leftover
        field, it's just ignored.
        """
        return AppSettings(
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
