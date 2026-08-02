"""
Application logging service.

Provides a small, dependency-injected logger that:
  * keeps a bounded in-memory journal for the "Лог" tab,
  * persists the journal so it survives restarts,
  * notifies subscribers (the UI) whenever a new entry is added,
  * never raises — logging must never be the cause of a crash.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from app.config import DEFAULTS
from app.models.log_models import LogEntry, LogLevel
from app.storage.local_storage import LocalStorage

# Standard library logger used as a secondary sink (visible in `adb logcat`
# / console output), independent from the in-app journal below.
_stdlib_logger = logging.getLogger("air_alert_analyzer")
if not _stdlib_logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
    )
    _stdlib_logger.addHandler(_handler)
    _stdlib_logger.setLevel(logging.DEBUG)

OnLogAdded = Callable[[LogEntry], None]


class LoggerService:
    """Central logging facility, injected into every other service."""

    def __init__(self, storage: LocalStorage) -> None:
        """Create the logger with its storage dependency."""
        self._storage = storage
        self._entries: list[LogEntry] = []
        self._listener: Optional[OnLogAdded] = None

    def set_listener(self, listener: Optional[OnLogAdded]) -> None:
        """Register a single callback invoked whenever a new entry is added.

        The UI uses this to append a row to the log view live, instead of
        re-rendering the whole journal on every message.
        """
        self._listener = listener

    @property
    def entries(self) -> list[LogEntry]:
        """Return a copy of the current in-memory journal (oldest first)."""
        return list(self._entries)

    async def load_persisted(self) -> None:
        """Load any previously persisted log entries into memory."""
        try:
            self._entries = await self._storage.load_log_entries()
        except Exception:
            # Corrupted log storage must never prevent the app from starting.
            self._entries = []

    async def clear(self) -> None:
        """Clear the journal, in memory and in storage."""
        self._entries = []
        try:
            await self._storage.save_log_entries(self._entries)
        except Exception:
            pass

    def debug(self, message: str) -> None:
        """Log a DEBUG-level message."""
        self._log(LogLevel.DEBUG, message)

    def info(self, message: str) -> None:
        """Log an INFO-level message."""
        self._log(LogLevel.INFO, message)

    def warning(self, message: str) -> None:
        """Log a WARNING-level message."""
        self._log(LogLevel.WARNING, message)

    def error(self, message: str) -> None:
        """Log an ERROR-level message."""
        self._log(LogLevel.ERROR, message)

    def _log(self, level: LogLevel, message: str) -> None:
        """Append an entry, mirror it to stdlib logging, and notify the UI."""
        entry = LogEntry(level=level, message=message)
        self._entries.append(entry)
        if len(self._entries) > DEFAULTS.MAX_LOG_ENTRIES:
            self._entries = self._entries[-DEFAULTS.MAX_LOG_ENTRIES :]

        try:
            _stdlib_logger.log(getattr(logging, level.value), message)
        except Exception:
            pass

        if self._listener is not None:
            try:
                self._listener(entry)
            except Exception:
                pass

        # Persisting on every single line would be wasteful; the caller
        # (AlertService / main) periodically calls `flush()` instead.

    async def flush(self) -> None:
        """Persist the current in-memory journal. Safe to call often."""
        try:
            await self._storage.save_log_entries(self._entries)
        except Exception:
            pass
