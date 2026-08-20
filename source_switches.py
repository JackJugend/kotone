"""Persistent operator switches for Kotone's optional metadata providers.

The switches are deliberately tiny and independent from SQLite: they must be
available while recovering the database and survive a Railway deployment.  A
corrupt state file fails open to the safe defaults (enabled); operators can
then immediately choose a deliberate state again via ``/dbonly``.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time

from settings import SOURCE_SWITCH_STATE_FILE


OPTIONAL_SOURCES = ("musicbrainz", "lastfm", "discogs")


class SourceSwitches:
    """Atomically persist switches for optional public APIs."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()

    def _load_locked(self) -> dict[str, bool]:
        result = {source: True for source in OPTIONAL_SOURCES}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return result
        if not isinstance(payload, dict):
            return result
        values = payload.get("enabled")
        if not isinstance(values, dict):
            return result
        for source in OPTIONAL_SOURCES:
            if isinstance(values.get(source), bool):
                result[source] = values[source]
        return result

    def enabled(self, source: str) -> bool:
        source = str(source or "").casefold()
        if source not in OPTIONAL_SOURCES:
            return False
        with self._lock:
            return self._load_locked()[source]

    def status(self) -> dict[str, bool]:
        with self._lock:
            return self._load_locked()

    def set_enabled(self, source: str, enabled: bool, *, actor: str = "") -> bool:
        source = str(source or "").casefold()
        if source not in OPTIONAL_SOURCES:
            raise ValueError(f"Nieznane źródło: {source!r}")
        with self._lock:
            values = self._load_locked()
            values[source] = bool(enabled)
            directory = os.path.dirname(self.path) or "."
            os.makedirs(directory, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                prefix=".source-switches-",
                suffix=".json",
                dir=directory,
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump(
                        {
                            "enabled": values,
                            "updated_at": time.time(),
                            "actor": str(actor or ""),
                        },
                        handle,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
            return values[source]


SOURCES = SourceSwitches(SOURCE_SWITCH_STATE_FILE)
