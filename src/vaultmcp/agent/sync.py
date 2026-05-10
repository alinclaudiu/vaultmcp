"""Sync engine: subscribe to master, apply incoming changes to local mirror.

Connects to ``GET /mcp/subscribe?since_global_version=<N>`` and processes
each event:

- ``PageChanged`` → fetch the page via ``wiki.read``, write it to local mirror
- ``PageDeleted`` → remove from local mirror (out of scope for v0.2 — logged only)
- ``LogAppended`` → append to local ``log.md``
- ``Heartbeat`` → ignore

When the SSE connection drops, the engine reconnects with the latest
``last_global_version`` from the cache (catch-up).
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from .client import MasterClient, MasterClientError
from .suppressor import SyncSuppressor
from .version_cache import VersionCache

LOG = logging.getLogger("vaultmcp.agent.sync")


class SyncEngine:
    def __init__(
        self,
        *,
        client: MasterClient,
        cache: VersionCache,
        vault_dir: Path,
        suppressor: SyncSuppressor | None = None,
        backoff_initial_seconds: float = 1.0,
        backoff_max_seconds: float = 60.0,
    ) -> None:
        self.client = client
        self.cache = cache
        self.vault_dir = vault_dir
        self.suppressor = suppressor
        self.backoff_initial = backoff_initial_seconds
        self.backoff_max = backoff_max_seconds
        self._stopping = False

    async def run(self) -> None:
        """Subscribe loop with exponential backoff on connection failure."""
        backoff = self.backoff_initial
        while not self._stopping:
            try:
                cursor = self.cache.get_cursor()
                LOG.info("Subscribing since global_version=%d", cursor)
                async for event in self.client.subscribe(
                    since_global_version=cursor
                ):
                    await self._apply_event(event)
                    backoff = self.backoff_initial  # reset on successful event
            except (MasterClientError, OSError) as exc:
                LOG.warning("Subscribe loop dropped (%s); retrying in %.1fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.backoff_max)
            except asyncio.CancelledError:
                raise

    async def stop(self) -> None:
        self._stopping = True

    async def _apply_event(self, event: dict) -> None:
        et = event.get("event_type")
        gv = int(event.get("global_version", 0))

        if et == "PageChanged":
            path = event.get("path")
            if not path:
                return
            try:
                page = await self.client.read(path)
            except MasterClientError as exc:
                LOG.warning("Failed to fetch %s after PageChanged: %s", path, exc)
                return
            self._write_to_mirror(path, page["content"])
            self.cache.set_version(path, page["version"], page["global_version"])
            LOG.info("Applied %s @ v%d", path, page["version"])

        elif et == "PageDeleted":
            path = event.get("path")
            if path:
                LOG.info("PageDeleted received for %s; ignoring (v0.2 — no delete)", path)

        elif et == "LogAppended":
            # The log.md file is normally appended to in master and synced
            # like any other page. We don't try to maintain the local log
            # file from event payloads; the next PageChanged on log.md will
            # bring the canonical content.
            pass

        elif et == "Heartbeat":
            pass

        if gv > self.cache.get_cursor():
            self.cache.set_cursor(gv)

    def _write_to_mirror(self, path: str, content: str) -> None:
        target = (self.vault_dir / path).resolve()
        try:
            target.relative_to(self.vault_dir.resolve())
        except ValueError:
            LOG.warning("Refusing to write outside vault: %s", path)
            return

        # Skip the disk write entirely if the file already has the
        # correct content. Avoids an unnecessary inotify event that
        # would tickle the watcher even with suppression in place.
        if target.exists():
            try:
                if target.read_text(encoding="utf-8") == content:
                    return
            except OSError:
                pass

        # Mark the path BEFORE writing so the watcher's event handler
        # (which may run on a different thread very fast) sees the
        # suppression flag in time.
        if self.suppressor is not None:
            self.suppressor.mark(target)

        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, target)
