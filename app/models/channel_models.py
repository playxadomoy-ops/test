"""Data models describing monitored Telegram channels."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass(slots=True)
class TelegramChannel:
    """A single monitored Telegram channel/chat."""

    username: str  # e.g. "@air_alert_ua" or numeric chat id as string
    display_name: str = ""
    enabled: bool = True
    connected: bool = False
    messages_count: int = 0
    last_update: Optional[datetime] = None

    def to_dict(self) -> dict:
        """Serialize to a plain dict for JSON storage."""
        return {
            "username": self.username,
            "display_name": self.display_name,
            "enabled": self.enabled,
            "connected": self.connected,
            "messages_count": self.messages_count,
            "last_update": self.last_update.isoformat() if self.last_update else None,
        }

    @staticmethod
    def from_dict(data: dict) -> "TelegramChannel":
        """Deserialize from a plain dict loaded from JSON storage."""
        last_update_raw = data.get("last_update")
        return TelegramChannel(
            username=data.get("username", ""),
            display_name=data.get("display_name", ""),
            enabled=bool(data.get("enabled", True)),
            connected=bool(data.get("connected", False)),
            messages_count=int(data.get("messages_count", 0)),
            last_update=datetime.fromisoformat(last_update_raw) if last_update_raw else None,
        )


@dataclass(slots=True)
class ChannelMessage:
    """A single parsed message coming from a monitored channel."""

    channel_username: str
    text: str
    received_at: datetime = field(default_factory=datetime.now)
    risk_contribution: float = 0.0
    matched_keywords: list[str] = field(default_factory=list)
