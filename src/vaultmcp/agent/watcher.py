"""Local file watcher.

Uses ``watchdog`` to detect edits in the vault directory and translate them
into ``wiki.write`` calls (or queue entries when offline).

Edits are debounced: many editors save through atomic renames or multiple
fsyncs that produce a flurry of events. We batch events per-path with a
small delay so we don't issue redundant writes.

Conflicts: when master returns 409, we replace the local file with master's
content and notify (via log) — the LLM session running locally will see
its content was overwritten on next read. This is the intended behavior;
the agent never tries to merge.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .client import ConflictError, MasterClient, MasterClientError
from .config import AgentConfig
from .queue import WriteQueue
from .suppressor import SyncSuppressor
from .version_cache import VersionCache

LOG = logging.getLogger("vaultmcp.agent.watcher")

DEBOUNCE_SECONDS = 0.5


class _Handler(FileSystemEventHandler):
    """Translates watchdog events into 'this path changed' notifications."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        notify: Callable[[Path], None],
    ) -> None:
        super().__init__()
        self.loop = loop
        self.notify = notify

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self.loop.call_soon_threadsafe(self.notify, Path(event.src_path))

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self.loop.call_soon_threadsafe(self.notify, Path(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        # Treat the destination as a new write.
        dst = getattr(event, "dest_path", None)
        if dst:
            self.loop.call_soon_threadsafe(self.notify, Path(str(dst)))


class FileWatcher:
    """Watches the vault for edits and pushes them to master."""

    def __init__(
        self,
        config: AgentConfig,
        client: MasterClient,
        cache: VersionCache,
        queue: WriteQueue,
        suppressor: SyncSuppressor | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.cache = cache
        self.queue = queue
        self.suppressor = suppressor
        self._pending: dict[Path, asyncio.TimerHandle] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._observer: Observer | None = None
        self._stopping = False

    async def run(self) -> None:
        """Start watching and replay any queued offline writes."""
        self._loop = asyncio.get_running_loop()
        self.config.vault_dir.mkdir(parents=True, exist_ok=True)

        # Start replaying any queued writes on startup.
        asyncio.create_task(self._replay_queue())

        # Start watchdog observer.
        handler = _Handler(self._loop, self._on_change)
        self._observer = Observer()
        self._observer.schedule(handler, str(self.config.vault_dir), recursive=True)
        self._observer.start()
        LOG.info("Watching %s", self.config.vault_dir)

        try:
            while not self._stopping:
                await asyncio.sleep(1)
        finally:
            if self._observer is not None:
                self._observer.stop()
                self._observer.join(timeout=5)

    async def stop(self) -> None:
        self._stopping = True

    # ---------- core ----------

    def _on_change(self, abs_path: Path) -> None:
        """Schedule a debounced write."""
        # Skip non-markdown, hidden, non-existent paths.
        if not abs_path.suffix == ".md":
            return
        if any(part.startswith(".") for part in abs_path.parts):
            return

        # Skip events caused by the sync engine writing master's content
        # to disk. Without this guard, every incoming PageChanged event
        # triggers a watcher event, which posts wiki.write back to master,
        # which 409s, which makes sync re-apply, which… (echo loop).
        if self.suppressor is not None and self.suppressor.is_suppressed(abs_path):
            return

        # Cancel pending timer for this path.
        old = self._pending.pop(abs_path, None)
        if old:
            old.cancel()

        loop = self._loop
        if loop is None:
            return

        timer = loop.call_later(
            DEBOUNCE_SECONDS,
            lambda: asyncio.create_task(self._flush(abs_path)),
        )
        self._pending[abs_path] = timer

    async def _flush(self, abs_path: Path) -> None:
        self._pending.pop(abs_path, None)
        if not abs_path.exists():
            return
        try:
            rel = abs_path.relative_to(self.config.vault_dir)
        except ValueError:
            return
        rel_str = rel.as_posix()
        try:
            content = abs_path.read_text(encoding="utf-8")
        except OSError as exc:
            LOG.warning("Could not read %s: %s", abs_path, exc)
            return

        await self._send_or_queue(rel_str, content)

    async def _send_or_queue(self, path: str, content: str) -> None:
        base_version = self.cache.get_version(path)
        session = {
            "server_id": self.config.server_id,
            "app": self.config.app,
            "agent_model": self.config.agent_model,
            "session_id": "",
        }
        try:
            result = await self.client.write(
                path=path,
                content=content,
                base_version=base_version,
                session=session,
            )
            self.cache.set_version(path, result["version"], result["global_version"])
            LOG.info("Pushed %s @ v%d", path, result["version"])
        except ConflictError as exc:
            LOG.warning(
                "Conflict on %s (master is at v%d by %s); replacing local copy",
                path, exc.current_version, exc.last_writer,
            )
            self._replace_local(path, exc.current_content)
            self.cache.set_version(path, exc.current_version, 0)  # gv unknown here
        except MasterClientError as exc:
            LOG.warning("Master unreachable for %s; queuing (%s)", path, exc)
            self.queue.enqueue(
                path=path,
                content=content,
                base_version=base_version,
                session=session,
            )

    async def _replay_queue(self) -> None:
        items = self.queue.list_pending()
        if not items:
            return
        LOG.info("Replaying %d queued writes", len(items))
        for item in items:
            try:
                result = await self.client.write(
                    path=item.path,
                    content=item.content,
                    base_version=item.base_version,
                    session=item.session,
                )
                self.cache.set_version(item.path, result["version"], result["global_version"])
                self.queue.remove(item)
                LOG.info("Replayed %s @ v%d", item.path, result["version"])
            except ConflictError as exc:
                LOG.warning(
                    "Conflict on replay of %s; sidelining and replacing local",
                    item.path,
                )
                self._replace_local(item.path, exc.current_content)
                self.queue.mark_failed(item, "conflict")
            except MasterClientError:
                # Still offline; stop replaying and try again later.
                LOG.info("Master still unreachable; pausing queue replay")
                return

    def _replace_local(self, path: str, content: str) -> None:
        target = (self.config.vault_dir / path).resolve()
        try:
            target.relative_to(self.config.vault_dir.resolve())
        except ValueError:
            return

        # Skip the disk write if content already matches.
        if target.exists():
            try:
                if target.read_text(encoding="utf-8") == content:
                    return
            except OSError:
                pass

        # Suppress the inotify echo this write will produce.
        if self.suppressor is not None:
            self.suppressor.mark(target)

        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, target)
