"""Async Postgres access for the master.

Wraps :mod:`asyncpg` with a small set of typed helpers that map directly
to the master's MCP tool surface. Every write is wrapped in a transaction
that:

1. Validates the version etag (optimistic concurrency).
2. INSERTs / UPDATEs the page row, bumping ``version`` and pulling a fresh
   ``global_version`` from the sequence.
3. INSERTs an event row (which fires the LISTEN/NOTIFY trigger).
4. INSERTs an audit row.

The renderer is called outside the DB transaction by the caller (a write
to disk that fails after a successful DB commit is logged but does not
roll back — the DB is the source of truth).
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator


def _json_default(obj: Any) -> Any:
    """JSON encoder fallback for non-builtin types we store in JSONB.

    YAML frontmatter often contains date/datetime values (e.g. ``updated: 2026-05-10``);
    Pydantic models we serialize for events and audit may contain them too.
    Without this fallback, ``json.dumps`` raises TypeError on datetime.
    """
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, default=_json_default)

import asyncpg


# =============================================================
# Page row + result types
# =============================================================


@dataclass
class PageRow:
    path: str
    content: str
    version: int
    global_version: int
    metadata: dict[str, Any]
    type: str
    owners: list[str]
    updated: datetime
    last_writer_server: str
    last_writer_app: str
    last_writer_session: str

    @classmethod
    def from_record(cls, r: asyncpg.Record) -> "PageRow":
        return cls(
            path=r["path"],
            content=r["content"],
            version=r["version"],
            global_version=r["global_version"],
            metadata=r["metadata"] or {},
            type=r["type"],
            owners=list(r["owners"] or []),
            updated=r["updated"],
            last_writer_server=r["last_writer_server"],
            last_writer_app=r["last_writer_app"],
            last_writer_session=r["last_writer_session"],
        )


@dataclass
class WriteResult:
    """Returned by :meth:`Database.write_page` on success."""

    path: str
    version: int
    global_version: int
    applied_at: datetime


@dataclass
class ConflictResult:
    """Returned when the supplied ``base_version`` doesn't match current."""

    current_version: int
    current_content: str
    last_writer: str  # server_id of the winner


# =============================================================
# Database wrapper
# =============================================================


class Database:
    """Thin wrapper over an :class:`asyncpg.Pool`."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    @classmethod
    async def connect(cls, dsn: str, *, min_size: int = 1, max_size: int = 10) -> "Database":
        pool = await asyncpg.create_pool(
            dsn=dsn,
            min_size=min_size,
            max_size=max_size,
            init=cls._init_connection,
        )
        if pool is None:
            raise RuntimeError("asyncpg.create_pool returned None")
        return cls(pool=pool)

    @staticmethod
    async def _init_connection(conn: asyncpg.Connection) -> None:
        # JSONB columns come back as Python dicts/lists; encoder handles
        # datetime/date (which YAML frontmatter often produces).
        await conn.set_type_codec(
            "jsonb",
            encoder=_json_dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )

    async def close(self) -> None:
        await self.pool.close()

    async def apply_schema(self, schema_path: Path) -> None:
        sql = schema_path.read_text(encoding="utf-8")
        async with self.pool.acquire() as conn:
            await conn.execute(sql)

    # ---------- pages ----------

    async def get_page(self, path: str) -> PageRow | None:
        """Fetch a single page; ``None`` if it doesn't exist."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM pages WHERE path = $1",
                path,
            )
        return PageRow.from_record(row) if row else None

    async def list_pages(
        self,
        *,
        prefix: str | None = None,
        type_filter: str | None = None,
        updated_since: datetime | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> tuple[list[PageRow], str | None]:
        """Paginated list. Cursor is the last seen ``global_version`` (string)."""
        clauses = ["TRUE"]
        params: list[Any] = []

        if prefix:
            params.append(prefix + "%")
            clauses.append(f"path LIKE ${len(params)}")
        if type_filter:
            params.append(type_filter)
            clauses.append(f"type = ${len(params)}")
        if updated_since:
            params.append(updated_since)
            clauses.append(f"updated >= ${len(params)}")
        if cursor:
            params.append(int(cursor))
            clauses.append(f"global_version < ${len(params)}")

        params.append(limit)
        sql = (
            "SELECT * FROM pages WHERE "
            + " AND ".join(clauses)
            + f" ORDER BY global_version DESC LIMIT ${len(params)}"
        )

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        pages = [PageRow.from_record(r) for r in rows]
        next_cursor = (
            str(pages[-1].global_version) if len(pages) == limit else None
        )
        return pages, next_cursor

    async def write_page(
        self,
        *,
        path: str,
        content: str,
        base_version: int | None,
        metadata: dict[str, Any],
        type_: str,
        owners: list[str],
        updated: datetime,
        last_writer_server: str,
        last_writer_app: str,
        last_writer_session: str,
        agent_model: str | None = None,
        prompt_hash: str | None = None,
        client_ip: str | None = None,
    ) -> WriteResult | ConflictResult:
        """Insert or update a page atomically; returns conflict if version mismatched.

        Atomicity guarantees (one transaction):
        - Bumps ``version`` and ``global_version``.
        - Inserts an event row (NOTIFY fires after COMMIT — correct semantic).
        - Inserts an audit row.

        If ``base_version`` differs from the current row's version, the
        transaction is rolled back and a :class:`ConflictResult` is returned.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                existing = await conn.fetchrow(
                    "SELECT version, content, last_writer_server FROM pages "
                    "WHERE path = $1 FOR UPDATE",
                    path,
                )

                if existing is None:
                    # New page — base_version must be None.
                    if base_version is not None:
                        await self._insert_audit(
                            conn,
                            operation="write",
                            path=path,
                            server_id=last_writer_server,
                            app=last_writer_app,
                            agent_model=agent_model,
                            prompt_hash=prompt_hash,
                            version_before=None,
                            version_after=None,
                            outcome="conflict",
                            error_code="not_found_but_base_version_supplied",
                            client_ip=client_ip,
                        )
                        return ConflictResult(
                            current_version=0,
                            current_content="",
                            last_writer="",
                        )
                    new_version = 1
                else:
                    if base_version != existing["version"]:
                        await self._insert_audit(
                            conn,
                            operation="write",
                            path=path,
                            server_id=last_writer_server,
                            app=last_writer_app,
                            agent_model=agent_model,
                            prompt_hash=prompt_hash,
                            version_before=existing["version"],
                            version_after=None,
                            outcome="conflict",
                            client_ip=client_ip,
                        )
                        return ConflictResult(
                            current_version=existing["version"],
                            current_content=existing["content"],
                            last_writer=existing["last_writer_server"],
                        )
                    new_version = existing["version"] + 1

                gv = await conn.fetchval("SELECT nextval('global_version_seq')")

                await conn.execute(
                    """
                    INSERT INTO pages
                        (path, content, version, global_version, metadata, type,
                         owners, updated, last_writer_server, last_writer_app,
                         last_writer_session)
                    VALUES
                        ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                    ON CONFLICT (path) DO UPDATE SET
                        content = EXCLUDED.content,
                        version = EXCLUDED.version,
                        global_version = EXCLUDED.global_version,
                        metadata = EXCLUDED.metadata,
                        type = EXCLUDED.type,
                        owners = EXCLUDED.owners,
                        updated = EXCLUDED.updated,
                        last_writer_server = EXCLUDED.last_writer_server,
                        last_writer_app = EXCLUDED.last_writer_app,
                        last_writer_session = EXCLUDED.last_writer_session
                    """,
                    path,
                    content,
                    new_version,
                    gv,
                    metadata,
                    type_,
                    owners,
                    updated,
                    last_writer_server,
                    last_writer_app,
                    last_writer_session,
                )

                await conn.execute(
                    """
                    INSERT INTO events (event_type, path, global_version, payload)
                    VALUES ($1, $2, $3, $4)
                    """,
                    "PageChanged",
                    path,
                    gv,
                    {"version": new_version},
                )

                await self._insert_audit(
                    conn,
                    operation="write",
                    path=path,
                    server_id=last_writer_server,
                    app=last_writer_app,
                    agent_model=agent_model,
                    prompt_hash=prompt_hash,
                    version_before=existing["version"] if existing else None,
                    version_after=new_version,
                    outcome="ok",
                    client_ip=client_ip,
                )

                return WriteResult(
                    path=path,
                    version=new_version,
                    global_version=gv,
                    applied_at=datetime.now(tz=timezone.utc),
                )

    # ---------- log ----------

    async def append_log(
        self,
        *,
        timestamp: datetime,
        server_id: str,
        app: str,
        modules_touched: list[str],
        integrations_updated: list[str],
        notable: list[str],
    ) -> tuple[int, int]:
        """Append to the session log. Returns ``(id, global_version)``."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                gv = await conn.fetchval("SELECT nextval('global_version_seq')")
                row = await conn.fetchrow(
                    """
                    INSERT INTO log_entries
                        (ts, server_id, app, modules_touched,
                         integrations_updated, notable, global_version)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    RETURNING id
                    """,
                    timestamp,
                    server_id,
                    app,
                    modules_touched,
                    integrations_updated,
                    notable,
                    gv,
                )
                await conn.execute(
                    """
                    INSERT INTO events (event_type, path, global_version, payload)
                    VALUES ($1, $2, $3, $4)
                    """,
                    "LogAppended",
                    None,
                    gv,
                    {"id": row["id"], "server_id": server_id, "app": app},
                )
                await self._insert_audit(
                    conn,
                    operation="append_log",
                    path=None,
                    server_id=server_id,
                    app=app,
                    agent_model=None,
                    prompt_hash=None,
                    version_before=None,
                    version_after=None,
                    outcome="ok",
                    client_ip=None,
                )
                return row["id"], gv

    # ---------- events / streaming ----------

    async def events_since(
        self, since_global_version: int, *, limit: int = 1000
    ) -> list[asyncpg.Record]:
        """Return events with ``global_version > since`` for catch-up."""
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT id, ts, event_type, path, global_version, payload
                FROM events
                WHERE global_version > $1
                ORDER BY global_version ASC
                LIMIT $2
                """,
                since_global_version,
                limit,
            )

    async def recent_events(self, *, limit: int = 50) -> list[asyncpg.Record]:
        """Return the N most recent events, newest first. Used by the dashboard."""
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT id, ts, event_type, path, global_version
                FROM events
                ORDER BY ts DESC, id DESC
                LIMIT $1
                """,
                limit,
            )

    @asynccontextmanager
    async def listen(self, channel: str) -> AsyncIterator[asyncpg.Connection]:
        """Acquire a dedicated connection and LISTEN on a channel.

        Caller installs ``add_listener`` callbacks on the yielded connection.
        """
        conn = await self.pool.acquire()
        try:
            await conn.execute(f"LISTEN {channel}")
            yield conn
        finally:
            await conn.execute(f"UNLISTEN {channel}")
            await self.pool.release(conn)

    # ---------- audit ----------

    async def _insert_audit(
        self,
        conn: asyncpg.Connection,
        *,
        operation: str,
        path: str | None,
        server_id: str,
        app: str,
        agent_model: str | None,
        prompt_hash: str | None,
        version_before: int | None,
        version_after: int | None,
        outcome: str,
        error_code: str | None = None,
        client_ip: str | None = None,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO audit
                (operation, path, server_id, app, agent_model, prompt_hash,
                 version_before, version_after, outcome, error_code, client_ip)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            """,
            operation,
            path,
            server_id,
            app,
            agent_model,
            prompt_hash,
            version_before,
            version_after,
            outcome,
            error_code,
            client_ip,
        )

    # ---------- search ----------

    async def search_pages(
        self,
        *,
        query: str,
        prefix: str | None = None,
        type_filter: str | None = None,
        limit: int = 20,
    ) -> list[asyncpg.Record]:
        """Full-text search across pages, ranked by ts_rank_cd.

        Uses ``websearch_to_tsquery`` so the query string can include
        unquoted phrases, ``-exclusions``, and ``OR`` — i.e. the
        Google-ish syntax users already know. Empty result set is
        returned when no rows match (no error).
        """
        params: list[Any] = [query]
        clauses = ["content_tsv @@ websearch_to_tsquery('english', $1)"]

        if prefix:
            params.append(prefix + "%")
            clauses.append(f"path LIKE ${len(params)}")
        if type_filter:
            params.append(type_filter)
            clauses.append(f"type = ${len(params)}")

        params.append(limit)
        sql = (
            "SELECT path, type, owners, updated, version, "
            "ts_rank_cd(content_tsv, websearch_to_tsquery('english', $1)) AS score, "
            "ts_headline("
            "  'english', content, websearch_to_tsquery('english', $1), "
            "  'StartSel=<<,StopSel=>>,MaxFragments=1,MaxWords=20,MinWords=5'"
            ") AS snippet "
            "FROM pages WHERE "
            + " AND ".join(clauses)
            + f" ORDER BY score DESC, updated DESC LIMIT ${len(params)}"
        )

        async with self.pool.acquire() as conn:
            return await conn.fetch(sql, *params)

    # ---------- audit (read) ----------

    async def list_audit(
        self,
        *,
        path: str | None = None,
        since: datetime | None = None,
        server_id: str | None = None,
        app: str | None = None,
        operation: str | None = None,
        limit: int = 100,
    ) -> list[asyncpg.Record]:
        """Return audit rows matching the given filters, newest first."""
        clauses = ["TRUE"]
        params: list[Any] = []

        if path is not None:
            params.append(path)
            clauses.append(f"path = ${len(params)}")
        if since is not None:
            params.append(since)
            clauses.append(f"ts >= ${len(params)}")
        if server_id is not None:
            params.append(server_id)
            clauses.append(f"server_id = ${len(params)}")
        if app is not None:
            params.append(app)
            clauses.append(f"app = ${len(params)}")
        if operation is not None:
            params.append(operation)
            clauses.append(f"operation = ${len(params)}")

        params.append(limit)
        sql = (
            "SELECT id, ts, operation, path, server_id, app, agent_model, "
            "prompt_hash, version_before, version_after, outcome, error_code, "
            "client_ip "
            "FROM audit WHERE "
            + " AND ".join(clauses)
            + f" ORDER BY ts DESC, id DESC LIMIT ${len(params)}"
        )

        async with self.pool.acquire() as conn:
            return await conn.fetch(sql, *params)

    # ---------- servers (auth) ----------

    async def add_server(
        self, *, server_id: str, apps: list[str], token_hash: str
    ) -> bool:
        """Insert a new server row. Returns False if the id already exists."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO servers (id, apps, token_hash, rotated_at)
                VALUES ($1, $2, $3, NOW())
                ON CONFLICT (id) DO NOTHING
                RETURNING id
                """,
                server_id,
                apps,
                token_hash,
            )
        return row is not None

    async def update_server_token(self, *, server_id: str, token_hash: str) -> bool:
        """Rotate the token of an existing server. Returns False if missing."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE servers SET token_hash = $1, rotated_at = NOW() WHERE id = $2",
                token_hash,
                server_id,
            )
        return result.endswith(" 1")

    async def remove_server(self, *, server_id: str) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute("DELETE FROM servers WHERE id = $1", server_id)
        return result.endswith(" 1")

    async def find_server_by_token_hash(
        self, token_hash: str
    ) -> tuple[str, list[str]] | None:
        """Look up a server by its hashed token. Returns ``(id, apps)`` or None."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, apps FROM servers WHERE token_hash = $1",
                token_hash,
            )
        if row is None:
            return None
        return row["id"], list(row["apps"] or [])

    async def count_servers(self) -> int:
        """Total registered servers. Auth is bypassed when this is zero."""
        async with self.pool.acquire() as conn:
            n = await conn.fetchval("SELECT COUNT(*) FROM servers")
        return int(n)

    async def list_servers(self) -> list[tuple[str, list[str], datetime | None]]:
        """List ``(id, apps, rotated_at)`` for every server. Tokens never returned."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, apps, rotated_at FROM servers ORDER BY id"
            )
        return [(r["id"], list(r["apps"] or []), r["rotated_at"]) for r in rows]
