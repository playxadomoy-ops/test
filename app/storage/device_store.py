"""
Persists this device's server login credentials (device_id + token).

Kept as its own tiny storage class -- deliberately NOT part of
AppSettings -- for two reasons:
  1. These are never user-editable (no UI ever shows or lets someone
     type a device_id/token anymore, per the fully-automatic connection
     requirement), so they don't belong in the same model as fields a
     person fills out in a form.
  2. It keeps "clear this device's server identity" (e.g. if it's ever
     needed for a manual reset/support flow) a one-line, single-purpose
     call that can't accidentally touch unrelated settings.

Security note: like the rest of this project's persistence (see
LocalStorage), this uses Flet's ``page.client_storage``, which on
Android is backed by SharedPreferences -- not hardware-keystore-backed
encryption. That's an existing, accepted limitation of this app's
storage layer, not something introduced here; it's appropriate for a
LAN-only home/small-team tool, not a sensitive credential store.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import flet as ft

from app.config import STORAGE


@dataclass(slots=True)
class DeviceCredentials:
    device_id: str
    token: str


class DeviceCredentialStore:
    def __init__(self, page: ft.Page) -> None:
        self._page = page

    async def load(self) -> Optional[DeviceCredentials]:
        try:
            if not await self._page.client_storage.contains_key_async(STORAGE.DEVICE_CREDENTIALS):
                return None
            raw = await self._page.client_storage.get_async(STORAGE.DEVICE_CREDENTIALS)
        except Exception:
            return None
        if not isinstance(raw, dict):
            return None
        device_id = raw.get("device_id")
        token = raw.get("token")
        if not device_id or not token:
            return None
        return DeviceCredentials(device_id=device_id, token=token)

    async def save(self, credentials: DeviceCredentials) -> None:
        await self._page.client_storage.set_async(
            STORAGE.DEVICE_CREDENTIALS,
            {"device_id": credentials.device_id, "token": credentials.token},
        )

    async def clear(self) -> None:
        try:
            await self._page.client_storage.remove_async(STORAGE.DEVICE_CREDENTIALS)
        except Exception:
            pass
