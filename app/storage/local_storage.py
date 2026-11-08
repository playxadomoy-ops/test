"""
Local persistence layer built on top of Flet's ``page.client_storage``.

``client_storage`` is backed by ``shared_preferences`` on Android and by a
local file on desktop, so data written here survives app restarts on both
platforms without any extra platform-specific code. Values are JSON
serializable automatically by Flet, so this module works with plain
``dict``/``list`` payloads and leaves typed (de)serialization to the model
classes themselves.

Note: this module previously also persisted a Telegram session string,
a locally-managed channel list, and the Vocabulary Builder's cache. All
three were specific to the mobile app connecting to Telegram directly,
which it no longer does -- that integration, and everything it needed
to persist, now lives entirely in the separate server application.
"""

from __future__ import annotations

from typing import Any, Optional

import flet as ft

from app.config import STORAGE
from app.models.log_models import LogEntry
from app.models.settings_models import AppSettings


class LocalStorage:
    """Wraps ``page.client_storage`` with typed load/save helpers.

    This is the only class in the project allowed to talk to
    ``page.client_storage`` directly; every other module goes through it,
    which keeps the storage backend swappable and testable.
    """

    def __init__(self, page: ft.Page) -> None:
        """Store a reference to the page whose client_storage will be used."""
        self._page = page

    async def load_settings(self) -> AppSettings:
        """Load persisted settings, or return defaults if none exist."""
        raw = await self._safe_get(STORAGE.SETTINGS)
        if not raw:
            return AppSettings.default()
        return AppSettings.from_dict(raw)

    async def save_settings(self, settings: AppSettings) -> None:
        """Persist the given settings."""
        await self._page.client_storage.set_async(STORAGE.SETTINGS, settings.to_dict())

    async def load_log_entries(self) -> list[LogEntry]:
        """Load persisted log journal entries (oldest first)."""
        raw = await self._safe_get(STORAGE.LOG_ENTRIES)
        if not raw:
            return []
        return [LogEntry.from_dict(item) for item in raw]

    async def save_log_entries(self, entries: list[LogEntry]) -> None:
        """Persist the full log journal."""
        payload = [entry.to_dict() for entry in entries]
        await self._page.client_storage.set_async(STORAGE.LOG_ENTRIES, payload)

    async def _safe_get(self, key: str) -> Optional[Any]:
        """Return the stored value for ``key``, or ``None`` on any failure.

        Never raises: a corrupted or missing value must not crash startup.
        """
        try:
            if not await self._page.client_storage.contains_key_async(key):
                return None
            return await self._page.client_storage.get_async(key)
        except Exception:
            return None
