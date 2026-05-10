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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import asyncpg


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
    def from_record(cls, r: asyncpg.Record) -> PageRow:
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
    async def connect(cls, dsn: str, *, min_size: int = 1, max_size: int = 10) -> Database:
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

    async def iter_all_pages(
        self, *, prefix: str | None = None, batch: int = 200
    ) -> AsyncIterator[PageRow]:
        """Stream every page row, in path order. Used by ``render-all``.

        Pulls in batches of ``batch`` rows so a wiki with hundreds of
        thousands of pages doesn't materialise the whole table in
        memory. Caller can pass ``prefix`` to scope the walk.
        """
        cursor: str | None = None
        async with self.pool.acquire() as conn:
            while True:
                clauses = ["TRUE"]
                params: list[Any] = []
                if prefix:
                    params.append(prefix + "%")
                    clauses.append(f"path LIKE ${len(params)}")
                if cursor is not None:
                    params.append(cursor)
                    clauses.append(f"path > ${len(params)}")
                params.append(batch)
                rows = await conn.fetch(
                    "SELECT * FROM pages WHERE "
                    + " AND ".join(clauses)
                    + f" ORDER BY path ASC LIMIT ${len(params)}",
                    *params,
                )
                if not rows:
                    return
                for r in rows:
                    yield PageRow.from_record(r)
                if len(rows) < batch:
                    return
                cursor = rows[-1]["path"]

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

                # Queue an embedding job for this page version. Idempotent
                # via the (path, version) unique key — if the worker is
                # behind and the same page is written twice in quick
                # succession, only one job per (path, version) survives.
                await conn.execute(
                    """
                    INSERT INTO embedding_jobs (path, version)
                    VALUES ($1, $2)
                    ON CONFLICT (path, version) DO NOTHING
                    """,
                    path,
                    new_version,
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
                    applied_at=datetime.now(tz=UTC),
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

    # ---------- embeddings (worker side) ----------

    async def pop_pending_embedding_job(
        self, *, max_attempts: int = 5
    ) -> tuple[int, str, int, str] | None:
        """Atomically claim the oldest pending embedding job.

        Bumps the row's ``attempts`` by 1 (so a failure to embed
        eventually retires the job after ``max_attempts``) and joins
        in the page's current content. Returns ``None`` when no jobs
        are pending. Uses ``FOR UPDATE SKIP LOCKED`` so multiple
        workers (a future scale-out) wouldn't fight over the same row.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    WITH next_job AS (
                        SELECT id FROM embedding_jobs
                        WHERE attempts < $1
                        ORDER BY enqueued_at ASC, id ASC
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE embedding_jobs ej
                    SET attempts = ej.attempts + 1
                    FROM next_job
                    WHERE ej.id = next_job.id
                    RETURNING ej.id, ej.path, ej.version
                    """,
                    max_attempts,
                )
                if row is None:
                    return None
                page = await conn.fetchrow(
                    "SELECT content FROM pages WHERE path = $1", row["path"]
                )
                content = page["content"] if page is not None else ""
                return row["id"], row["path"], row["version"], content

    async def complete_embedding_job(
        self,
        *,
        job_id: int,
        path: str,
        version: int,
        model: str,
        dim: int,
        vector: list[float],
    ) -> None:
        """Upsert the embedding and remove the job in one transaction."""
        # pgvector accepts a string literal cast to vector — saves us
        # adding the pgvector Python package as a runtime dep just to
        # register the asyncpg codec.
        vec_literal = "[" + ",".join(f"{x:.7f}" for x in vector) + "]"
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO embeddings (path, version, model, dim, embedding, computed_at)
                    VALUES ($1, $2, $3, $4, $5::vector, NOW())
                    ON CONFLICT (path) DO UPDATE SET
                        version = EXCLUDED.version,
                        model = EXCLUDED.model,
                        dim = EXCLUDED.dim,
                        embedding = EXCLUDED.embedding,
                        computed_at = EXCLUDED.computed_at
                    """,
                    path,
                    version,
                    model,
                    dim,
                    vec_literal,
                )
                await conn.execute("DELETE FROM embedding_jobs WHERE id = $1", job_id)

    async def embedding_queue_stats(self) -> dict[str, Any]:
        """Aggregate counts for the dashboard's worker section.

        ``pending`` = jobs whose ``attempts`` is still below the worker's
        retry cap (5). ``failed`` = jobs that exhausted retries and are
        sitting in the queue with a last_error attached.
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) FILTER (WHERE attempts < 5) AS pending,
                    COUNT(*) FILTER (WHERE attempts >= 5) AS failed
                FROM embedding_jobs
                """
            )
            embedded = await conn.fetchval("SELECT COUNT(*) FROM embeddings")
            last_err = await conn.fetchrow(
                "SELECT path, version, last_error FROM embedding_jobs "
                "WHERE last_error IS NOT NULL "
                "ORDER BY enqueued_at DESC LIMIT 1"
            )
        return {
            "pending": int(row["pending"] or 0),
            "failed": int(row["failed"] or 0),
            "embedded": int(embedded or 0),
            "last_error": last_err["last_error"] if last_err else None,
            "last_error_path": last_err["path"] if last_err else None,
        }

    async def fail_embedding_job(self, *, job_id: int, error: str) -> None:
        """Record an error on a job so the dashboard surfaces it; attempts already bumped."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE embedding_jobs SET last_error = $1 WHERE id = $2",
                error,
                job_id,
            )

    # ---------- search ----------

    async def neighbors_of(
        self, *, path: str, limit: int = 10
    ) -> list[asyncpg.Record]:
        """Return the closest pages (by cosine) to ``path``'s embedding.

        Excludes ``path`` itself. Returns ``[]`` when ``path`` has no
        embedding yet (e.g. the worker hasn't drained the queue).
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT p.path, p.type, p.updated, p.version,
                       1 - (e.embedding <=> base.embedding) AS similarity
                FROM embeddings e
                JOIN pages p ON p.path = e.path
                JOIN embeddings base ON base.path = $1
                WHERE e.path <> $1
                ORDER BY e.embedding <=> base.embedding ASC
                LIMIT $2
                """,
                path,
                limit,
            )

    async def search_pages_semantic(
        self,
        *,
        query_vector: list[float],
        prefix: str | None = None,
        type_filter: str | None = None,
        limit: int = 20,
    ) -> list[asyncpg.Record]:
        """Cosine-distance search against the ``embeddings`` table.

        Returns rows with ``score`` = ``1 - distance`` (i.e. cosine
        similarity in [-1, 1]; for unit-norm vectors, in [0, 1]).
        """
        vec_literal = "[" + ",".join(f"{x:.7f}" for x in query_vector) + "]"
        params: list[Any] = [vec_literal]
        clauses = ["TRUE"]

        if prefix:
            params.append(prefix + "%")
            clauses.append(f"p.path LIKE ${len(params)}")
        if type_filter:
            params.append(type_filter)
            clauses.append(f"p.type = ${len(params)}")

        params.append(limit)
        sql = (
            "SELECT p.path, p.type, p.owners, p.updated, p.version, "
            "1 - (e.embedding <=> $1::vector) AS score, "
            "'' AS snippet "
            "FROM embeddings e JOIN pages p ON p.path = e.path "
            "WHERE " + " AND ".join(clauses)
            + f" ORDER BY e.embedding <=> $1::vector ASC LIMIT ${len(params)}"
        )

        async with self.pool.acquire() as conn:
            return await conn.fetch(sql, *params)

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

    # ---------- extensions ----------

    async def ext_register(
        self,
        *,
        name: str,
        schema_prefix: str,
        role_name: str,
        owners: list[str],
        policy: dict[str, Any],
        grants: list[str],
    ) -> dict[str, Any] | None:
        """Insert a new extension and provision its Postgres role.

        All of: extensions row insert, ``CREATE ROLE``, and the policy
        ``GRANT``s run inside one transaction so a partial failure
        doesn't leave a half-registered extension.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    INSERT INTO extensions
                        (name, schema_prefix, role_name, owners, policy)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (name) DO NOTHING
                    RETURNING name, schema_prefix, role_name, owners, policy,
                              schema_version, created_at
                    """,
                    name,
                    schema_prefix,
                    role_name,
                    owners,
                    policy,
                )
                if row is None:
                    return None
                # CREATE ROLE inside the same transaction. NOLOGIN so
                # nobody can connect AS the extension; only SET ROLE
                # under master's connection grants its privileges.
                # IF NOT EXISTS landed in PG 16; we use a portable form.
                exists = await conn.fetchval(
                    "SELECT 1 FROM pg_roles WHERE rolname = $1", role_name
                )
                if not exists:
                    await conn.execute(f"CREATE ROLE {role_name} NOLOGIN")
                # Master needs membership of the new role to issue
                # ``SET ROLE`` from ext.query / ext.exec. Granting to
                # CURRENT_USER scopes it to whichever DB user the
                # master is connecting as (vaultmcp in production).
                await conn.execute(f"GRANT {role_name} TO CURRENT_USER")
                for grant_sql in grants:
                    await conn.execute(grant_sql)
        return {
            "name": row["name"],
            "schema_prefix": row["schema_prefix"],
            "role_name": row["role_name"],
            "owners": list(row["owners"] or []),
            "policy": row["policy"] or {},
            "schema_version": row["schema_version"],
            "created_at": row["created_at"],
        }

    async def ext_get(self, name: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT name, schema_prefix, role_name, owners, policy, "
                "schema_version, created_at FROM extensions WHERE name = $1",
                name,
            )
        if row is None:
            return None
        return {
            "name": row["name"],
            "schema_prefix": row["schema_prefix"],
            "role_name": row["role_name"],
            "owners": list(row["owners"] or []),
            "policy": row["policy"] or {},
            "schema_version": row["schema_version"],
            "created_at": row["created_at"],
        }

    async def ext_list(self) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT name, schema_prefix, role_name, owners, policy, "
                "schema_version, created_at FROM extensions ORDER BY name"
            )
        return [
            {
                "name": r["name"],
                "schema_prefix": r["schema_prefix"],
                "role_name": r["role_name"],
                "owners": list(r["owners"] or []),
                "policy": r["policy"] or {},
                "schema_version": r["schema_version"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    async def ext_deregister(
        self, *, name: str, schema_prefix: str, role_name: str
    ) -> dict[str, Any]:
        """Tear down an extension. Reverse of ext_register.

        Order matters:
        1. Re-grant the role to CURRENT_USER (defensive — the original
           ``GRANT … TO CURRENT_USER`` from registration may have been
           revoked, and DROP OWNED needs membership privileges).
        2. DELETE pages where path matches the ``ext/<name>/`` namespace
           — cascades to ``embeddings`` + ``embedding_jobs`` via FK.
        3. DROP every ``<schema_prefix>*`` table.
        4. DROP OWNED + DROP ROLE.
        5. DELETE extensions row.

        Returns a small report so the operator sees what got cleaned.
        """
        report = {
            "name": name,
            "tables_dropped": 0,
            "pages_dropped": 0,
            "role_dropped": False,
        }
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # If the extensions row is already gone, treat as no-op.
                exists = await conn.fetchval(
                    "SELECT 1 FROM extensions WHERE name = $1", name
                )
                if not exists:
                    return report

                # 1. Make sure we can DROP OWNED.
                role_exists = await conn.fetchval(
                    "SELECT 1 FROM pg_roles WHERE rolname = $1", role_name
                )
                if role_exists:
                    try:
                        await conn.execute(f"GRANT {role_name} TO CURRENT_USER")
                    except Exception:
                        pass

                # 2. Drop pages in the ext namespace; FK cascades.
                deleted = await conn.fetchval(
                    "WITH d AS ("
                    "  DELETE FROM pages WHERE path LIKE $1 RETURNING 1"
                    ") SELECT count(*) FROM d",
                    f"ext/{name}/%",
                )
                report["pages_dropped"] = int(deleted or 0)

                # 3. Drop ext_<name>_* tables.
                ext_tables = [
                    r["tablename"]
                    for r in await conn.fetch(
                        "SELECT tablename FROM pg_tables "
                        "WHERE schemaname = 'public' AND tablename LIKE $1",
                        f"{schema_prefix}%",
                    )
                ]
                for tbl in ext_tables:
                    await conn.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")
                report["tables_dropped"] = len(ext_tables)

                # 4. DROP role if present.
                if role_exists:
                    try:
                        await conn.execute(f"DROP OWNED BY {role_name}")
                    except Exception:
                        # Worst case the role keeps a few leftover ACLs;
                        # DROP ROLE will tell us below.
                        pass
                    try:
                        await conn.execute(f"DROP ROLE IF EXISTS {role_name}")
                        report["role_dropped"] = True
                    except Exception:
                        report["role_dropped"] = False

                # 5. Remove the extensions row.
                await conn.execute("DELETE FROM extensions WHERE name = $1", name)
        return report

    async def ext_create_table(
        self, *, sql: str, full_table_name: str, role_name: str
    ) -> None:
        """Run a pre-validated CREATE TABLE and grant the extension role
        full DML on the new table. One transaction so the GRANT can't be
        skipped if a later step fails.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, "
                    f"REFERENCES, TRIGGER ON TABLE {full_table_name} TO {role_name}"
                )

    async def ext_exec_as_role(
        self,
        *,
        sql: str,
        params: list[Any],
        role_name: str,
    ) -> int:
        """Run a mutation under the extension's role.

        Read-write transaction; the role's GRANTs decide what can be
        touched. Returns ``rows_affected`` parsed from the asyncpg
        status string (e.g. ``INSERT 0 3`` → 3).
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(f"SET LOCAL ROLE {role_name}")
                status = await conn.execute(sql, *params)
        # status is "<COMMAND> [<oid>] <count>" for INSERT, "<CMD> <count>"
        # otherwise. The trailing token is always the row count.
        try:
            return int(status.rsplit(" ", 1)[-1])
        except (ValueError, IndexError):
            return 0

    async def ext_emit_event(
        self,
        *,
        event_type: str,
        path: str | None,
        payload: dict[str, Any],
        role_name: str,
    ) -> tuple[int, int]:
        """Insert a row into ``events`` from inside the extension's role.

        Returns ``(event_id, global_version)``. The role must have
        INSERT on ``events`` (granted when policy.can_subscribe_events
        is true); otherwise Postgres raises permission denied.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(f"SET LOCAL ROLE {role_name}")
                gv = await conn.fetchval(
                    "SELECT nextval('global_version_seq')"
                )
                row = await conn.fetchrow(
                    """
                    INSERT INTO events (event_type, path, global_version, payload)
                    VALUES ($1, $2, $3, $4)
                    RETURNING id
                    """,
                    event_type,
                    path,
                    gv,
                    payload,
                )
        return int(row["id"]), int(gv)

    async def ext_enqueue_embedding(
        self, *, path: str, content: str
    ) -> bool:
        """Persist content for an extension path + enqueue an embedding job.

        Reuses the ``embeddings`` table (which is keyed on ``path``
        with an FK to ``pages.path``); to keep the FK satisfied while
        avoiding a wiki entry for ext content, we insert a synthetic
        ``pages`` row in the ``ext/<name>/...`` namespace with type
        ``shared``. The worker then drains the job like any other.
        """
        from datetime import datetime
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Upsert a synthetic page so the FK on embeddings(path)
                # is satisfied. last_writer_* fields are filled with
                # the path itself for traceability.
                gv = await conn.fetchval(
                    "SELECT nextval('global_version_seq')"
                )
                existing = await conn.fetchrow(
                    "SELECT version FROM pages WHERE path = $1", path
                )
                new_version = (existing["version"] + 1) if existing else 1
                await conn.execute(
                    """
                    INSERT INTO pages
                        (path, content, version, global_version, metadata, type,
                         owners, updated, last_writer_server, last_writer_app,
                         last_writer_session)
                    VALUES
                        ($1, $2, $3, $4, '{}'::jsonb, 'shared', ARRAY[]::TEXT[],
                         $5, 'ext', 'ext', '')
                    ON CONFLICT (path) DO UPDATE SET
                        content = EXCLUDED.content,
                        version = EXCLUDED.version,
                        global_version = EXCLUDED.global_version,
                        updated = EXCLUDED.updated
                    """,
                    path,
                    content,
                    new_version,
                    gv,
                    datetime.now(tz=UTC),
                )
                await conn.execute(
                    """
                    INSERT INTO embedding_jobs (path, version)
                    VALUES ($1, $2)
                    ON CONFLICT (path, version) DO NOTHING
                    """,
                    path,
                    new_version,
                )
        return True

    async def ext_query_as_role(
        self,
        *,
        sql: str,
        params: list[Any],
        limit: int,
        role_name: str,
        readonly: bool,
    ) -> tuple[list[str], list[list[Any]], bool]:
        """Run user-supplied SQL under the extension's role.

        ``SET LOCAL ROLE`` is automatically reset at transaction end
        regardless of whether the inner query raised, so even an
        asyncpg crash mid-query cannot leave a connection running as
        the extension role on the next pool checkout.

        For ``ext.query`` the transaction is opened READ ONLY; the role
        grants on top mean a SELECT can only see what the policy
        allowed. For ``ext.exec`` the transaction is read-write but
        the role only has DML on its own ``ext_<name>_*`` tables (plus
        whatever core grants the policy specifies).
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction(readonly=readonly):
                await conn.execute(f"SET LOCAL ROLE {role_name}")
                rows = await conn.fetch(sql, *params)
        truncated = len(rows) > limit
        out_rows = [list(r.values()) for r in rows[:limit]]
        cols: list[str] = list(rows[0].keys()) if rows else []
        return cols, out_rows, truncated

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

    async def servers_with_activity(
        self,
    ) -> list[tuple[str, list[str], datetime | None, datetime | None]]:
        """As :meth:`list_servers` but also joins the most recent audit timestamp.

        ``last_active`` is None when the server has never been seen in audit.
        Used by the dashboard's Servers page.
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT s.id, s.apps, s.rotated_at,
                       (SELECT MAX(ts) FROM audit a WHERE a.server_id = s.id) AS last_active
                FROM servers s
                ORDER BY s.id
                """
            )
        return [
            (r["id"], list(r["apps"] or []), r["rotated_at"], r["last_active"])
            for r in rows
        ]
