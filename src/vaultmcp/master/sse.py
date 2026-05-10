"""Server-Sent Events broadcaster for the wiki.subscribe endpoint.

The master process opens one dedicated Postgres LISTEN connection on
``events_channel`` and fans out each notification to every connected
subscriber via an in-memory pub/sub. Subscribers are async queues; each
SSE handler (an HTTP request) consumes from its queue and emits SSE
chunks to its client.

Catch-up: when a subscriber connects with ``since_global_version=N``,
the broadcaster first replays all events from the DB with
``global_version > N`` before starting the live stream. This prevents
race conditions where an event happens between the replay query and the
LISTEN attaching.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

import asyncpg

from .db import Database

LOG = logging.getLogger(__name__)


@dataclass
class _Subscriber:
    queue: asyncio.Queue[dict]
    prefix: str | None


class EventBroadcaster:
    """Single-process fanout from Postgres NOTIFY to N HTTP subscribers."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._subscribers: set[_Subscriber] = set()
        self._lock = asyncio.Lock()
        self._listen_task: asyncio.Task | None = None
        self._listen_conn: asyncpg.Connection | None = None
        self._stopping = False

    async def start(self) -> None:
        """Open the LISTEN connection and start the fanout loop."""
        self._stopping = False
        self._listen_task = asyncio.create_task(self._listen_loop())
        # Heartbeat: every 30s emit a heartbeat to all subscribers.
        asyncio.create_task(self._heartbeat_loop())

    async def stop(self) -> None:
        self._stopping = True
        if self._listen_task is not None:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass

    async def _listen_loop(self) -> None:
        """Hold one Postgres connection open, LISTENing forever."""
        try:
            async with self.db.listen("events_channel") as conn:
                self._listen_conn = conn
                # asyncpg >= 0.30 made add_listener a coroutine; await it
                # so the callback is actually registered (a forgotten await
                # silently disables the entire fanout).
                await conn.add_listener("events_channel", self._on_notify)  # type: ignore[arg-type]
                while not self._stopping:
                    await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("LISTEN loop crashed; will be restarted")
            raise

    def _on_notify(
        self,
        connection: asyncpg.Connection,
        pid: int,
        channel: str,
        payload: str,
    ) -> None:
        """Synchronous callback from asyncpg; schedule async fanout."""
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            LOG.warning("Bad NOTIFY payload: %r", payload)
            return
        asyncio.create_task(self._fanout(data))

    async def _fanout(self, payload: dict) -> None:
        """Push one notification to every subscriber whose prefix matches."""
        path = payload.get("path") or ""
        async with self._lock:
            subs = list(self._subscribers)
        for sub in subs:
            if sub.prefix and not path.startswith(sub.prefix):
                continue
            try:
                sub.queue.put_nowait(payload)
            except asyncio.QueueFull:
                LOG.warning("Dropping event for slow subscriber")

    async def _heartbeat_loop(self) -> None:
        """Emit a heartbeat to all subscribers every 30 seconds."""
        while not self._stopping:
            await asyncio.sleep(30)
            heartbeat = {"event_type": "Heartbeat"}
            async with self._lock:
                subs = list(self._subscribers)
            for sub in subs:
                try:
                    sub.queue.put_nowait(heartbeat)
                except asyncio.QueueFull:
                    pass

    # ---------- subscriber API ----------

    async def subscribe(
        self,
        *,
        since_global_version: int | None,
        prefix: str | None,
    ) -> AsyncIterator[dict]:
        """Yield events to one HTTP subscriber.

        First, replays any events with ``global_version > since`` from the DB.
        Then yields live events from the in-memory queue.
        """
        # Catch-up phase.
        if since_global_version is not None:
            for record in await self.db.events_since(since_global_version):
                payload = {
                    "id": record["id"],
                    "event_type": record["event_type"],
                    "path": record["path"],
                    "global_version": record["global_version"],
                    "data": record["payload"],
                }
                if prefix and (payload.get("path") or "").startswith(prefix) is False:
                    continue
                yield payload

        # Register for live updates.
        sub = _Subscriber(queue=asyncio.Queue(maxsize=256), prefix=prefix)
        async with self._lock:
            self._subscribers.add(sub)
        try:
            while True:
                event = await sub.queue.get()
                yield event
        finally:
            async with self._lock:
                self._subscribers.discard(sub)


def format_sse(payload: dict) -> str:
    """Format one event as an SSE frame.

    Standard format: ``event: <type>\\ndata: <json>\\n\\n``.
    """
    event_type = payload.get("event_type", "message")
    data = json.dumps(payload, default=str)
    return f"event: {event_type}\ndata: {data}\n\n"
