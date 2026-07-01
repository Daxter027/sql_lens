"""
progress.py
-----------
Thread-safe, in-memory progress registry keyed by session token.

Long-running executions (e.g. a page-moving DBCC SHRINKFILE) publish live
telemetry here from a background monitor thread; the frontend polls a read-only
endpoint to drive a progress bar. Ephemeral by design — nothing is persisted.
"""

import threading
from typing import Any, Optional


class ProgressStore:
    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def set(self, token: str, data: dict[str, Any]) -> None:
        with self._lock:
            self._store[token] = dict(data)

    def get(self, token: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._store.get(token) or {})

    def clear(self, token: Optional[str]) -> None:
        if not token:
            return
        with self._lock:
            self._store.pop(token, None)


# Module-level singleton.
store = ProgressStore()
