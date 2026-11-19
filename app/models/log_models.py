"""Data models for the in-app log journal."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class LogLevel(str, Enum):
    """Severity of a log entry, mirrors standard logging levels."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    #: A threat lifecycle event (appeared / moved / destroyed / all-clear)
    #: -- distinct from ordinary diagnostic log levels above in that it
    #: is exempt from the rolling-window prune (see LoggerService._log),
    #: so it remains permanently visible per the "never delete threat
    #: history" requirement, instead of aging out alongside routine
    #: DEBUG/INFO noise.
    HISTORY = "HISTORY"


@dataclass(slots=True)
class LogEntry:
    """A single line in the application's log journal."""

    level: LogLevel
    message: str
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict:
        """Serialize to a plain dict for JSON storage."""
        return {
            "level": self.level.value,
            "message": self.message,
            "timestamp": self.timestamp.isoformat(),
        }

    @staticmethod
    def from_dict(data: dict) -> "LogEntry":
        """Deserialize from a plain dict loaded from JSON storage."""
        return LogEntry(
            level=LogLevel(data.get("level", LogLevel.INFO.value)),
            message=data.get("message", ""),
            timestamp=datetime.fromisoformat(data["timestamp"])
            if data.get("timestamp")
            else datetime.now(),
        )

    def formatted(self) -> str:
        """Return a one-line human readable representation."""
        return f"[{self.timestamp:%H:%M:%S}] {self.level.value:<7} {self.message}"
