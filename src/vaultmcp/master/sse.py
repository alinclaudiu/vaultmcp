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


# eq=False keeps identity-based equality and hashing, which is what we want
# for tracking subscribers in a set (each instance is its own subscription).
# The default @dataclass sets __hash__ = None, making instances unhashable.
@dataclass(eq=False)
class _Subscriber:
    queue: asyncio.Queue[dict]
    prefix: str | None


# Special event pushed by ``EventBroadcaster.stop()`` so subscribers
# parked in ``await queue.get()`` can wake up and exit cleanly. Any
# value would do; using a singleton dict means the receiver doesn't
# have to teach the JSON encoder about a new sentinel type — it
# inspects the dict in-place and never yields it.
_SHUTDOWN_SENTINEL: dict = {"_shutdown_": True}


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
        # Wake every active subscriber. Without this, each subscribe()
        # generator is parked in ``await sub.queue.get()`` and uvicorn's
        # graceful shutdown blocks indefinitely waiting for the SSE
        # connection to close. We push a sentinel; the subscribe loop
        # checks for it and returns.
        async with self._lock:
            subs = list(self._subscribers)
        for sub in subs:
            try:
                sub.queue.put_nowait(_SHUTDOWN_SENTINEL)
            except asyncio.QueueFull:
                # The queue is full of pending events. The subscriber
                # has already crashed or is unreachable — discarding
                # this sentinel is fine; uvicorn's connection-level
                # cancellation will tear it down.
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
                try:
                    while not self._stopping:
                        await asyncio.sleep(3600)
                finally:
                    # Remove the callback before the connection returns
                    # to the pool. asyncpg emits an InterfaceWarning when
                    # a connection with active notification listeners is
                    # released, even when the eventual pool close would
                    # eventually drop it. CancelledError can land here
                    # under shutdown — keep the cleanup synchronous to
                    # finish before re-raising.
                    try:
                        await conn.remove_listener(  # type: ignore[func-returns-value]
                            "events_channel", self._on_notify
                        )
                    except Exception:
                        LOG.debug("remove_listener failed during shutdown")
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
                # Poll with a short timeout so the loop can notice
                # ``_stopping`` even when uvicorn defers the lifespan
                # shutdown until after active requests close. Without
                # this, a long-lived SSE client deadlocks shutdown:
                # uvicorn waits for the request to end, the request
                # waits for an event, the event won't come because
                # broadcaster.stop() runs only on lifespan __aexit__.
                try:
                    event = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
                except TimeoutError:
                    if self._stopping:
                        return
                    continue
                if event is _SHUTDOWN_SENTINEL:
                    return
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
