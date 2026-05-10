"""Sync-suppression coordination between the agent's sync engine and watcher.

Without this, the agent loops: an SSE event causes sync.py to write the
file to disk → watchdog fires → watcher pushes the same content back to
master → master returns 409 (version moved) → watcher replaces local with
master content → watchdog fires again → loop.

The suppressor records "I just wrote this path because of an incoming
sync event" with a short expiry. The watcher consults it on every
filesystem event and skips paths that are still suppressed.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path


class SyncSuppressor:
    """Marks paths as recently-applied-by-sync so the watcher skips them.

    The window must be larger than the watcher's debounce delay so that
    even atomic-rename editors (multiple inotify events for one save)
    don't slip through. 2 seconds is comfortable for our 0.5s debounce.
    """

    SUPPRESSION_WINDOW: float = 2.0

    def __init__(self) -> None:
        self._suppressed: dict[Path, float] = {}
        # The watcher callback runs from the watchdog thread; the sync
        # engine runs in the asyncio event loop. Use a lock so we don't
        # race on the dict.
        self._lock = threading.Lock()

    def mark(self, path: Path) -> None:
        """Suppress events for `path` for the next ``SUPPRESSION_WINDOW`` seconds."""
        with self._lock:
            self._suppressed[path.resolve()] = (
                time.monotonic() + self.SUPPRESSION_WINDOW
            )

    def is_suppressed(self, path: Path) -> bool:
        """Return True if `path` was just written by sync."""
        resolved = path.resolve()
        now = time.monotonic()
        with self._lock:
            expiry = self._suppressed.get(resolved)
            if expiry is None:
                return False
            if expiry < now:
                # Expired; clean up and let the event through.
                del self._suppressed[resolved]
                return False
            return True
