"""Data model describing a processed threat message from the server.

Note: this module previously also defined ``TelegramChannel`` (a raw
Telegram channel the app directly monitored). That's gone now that the
mobile app no longer connects to Telegram directly -- channel
management lives entirely on the server side (see the separate server
application's own Channels admin screen).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class ChannelMessage:
    """A single parsed message coming from a monitored channel."""

    channel_username: str
    text: str
    received_at: datetime = field(default_factory=datetime.now)
    risk_contribution: float = 0.0
    matched_keywords: list[str] = field(default_factory=list)
