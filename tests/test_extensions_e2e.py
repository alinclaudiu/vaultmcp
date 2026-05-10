"""End-to-end tests for the ext.* MCP tools.

Walks through the worked-example flow from docs/03-extensibility.md:
register a CRM extension, declare a contacts table, insert rows
through the connection used by the test, and run a SELECT through
ext.query that touches both the extension's table and a core table.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import pytest

pytestmark = pytest.mark.e2e


@pytest.mark.asyncio
async def test_extension_lifecycle_register_declare_query(
    clean_database: str, tmp_path: Path
) -> None:
    from vaultmcp.master.config import MasterConfig
    from vaultmcp.master.db import Database
    from vaultmcp.master.tools import call_handler_by_name

    config = MasterConfig(database_url=clean_database, wiki_dir=tmp_path / "wiki")
    config.wiki_dir.mkdir()
    db = await Database.connect(config.database_url)
    try:
        await db.apply_schema(config.schema_sql_path)

        # ---- ext.register ----
        out = await call_handler_by_name(
            "ext.register",
            {
                "name": "crm",
                "owners": ["webstore", "admin"],
                "policy": {"can_read_pages": True, "can_use_embeddings": True},
            },
            db=db,
            wiki_dir=config.wiki_dir,
        )
        assert out["name"] == "crm"
        assert out["schema_prefix"] == "ext_crm_"
        assert out["schema_version"] == 1

        # Re-register fails with 422.
        from vaultmcp.master.errors import ValidationFailed
        with pytest.raises(ValidationFailed, match="already registered"):
            await call_handler_by_name(
                "ext.register",
                {"name": "crm"},
                db=db,
                wiki_dir=config.wiki_dir,
            )

        # ---- ext.list ----
        listed = await call_handler_by_name(
            "ext.list", {}, db=db, wiki_dir=config.wiki_dir
        )
        assert any(e["name"] == "crm" for e in listed["extensions"])

        # ---- ext.declare_table ----
        out = await call_handler_by_name(
            "ext.declare_table",
            {
                "extension": "crm",
                "name": "contacts",
                "columns": [
                    {"name": "id", "type": "uuid", "primary_key": True},
                    {"name": "name", "type": "text", "not_null": True},
                    {"name": "email", "type": "text"},
                    {"name": "linked_page_path", "type": "text"},
                    {
                        "name": "created_at",
                        "type": "timestamptz",
                        "not_null": True,
                        "default": "NOW()",
                    },
                ],
            },
            db=db,
            wiki_dir=config.wiki_dir,
        )
        assert out["full_table_name"] == "ext_crm_contacts"
        assert out["column_count"] == 5

        # Declaring against an unknown extension returns 404 (NotFoundError).
        from vaultmcp.master.errors import NotFoundError
        with pytest.raises(NotFoundError):
            await call_handler_by_name(
                "ext.declare_table",
                {
                    "extension": "no-such",
                    "name": "x",
                    "columns": [{"name": "id", "type": "uuid", "primary_key": True}],
                },
                db=db,
                wiki_dir=config.wiki_dir,
            )

        # ---- write directly into the new table (extension code does this) ----
        conn = await asyncpg.connect(clean_database)
        try:
            await conn.execute(
                "INSERT INTO ext_crm_contacts (id, name, email, linked_page_path) "
                "VALUES (gen_random_uuid(), $1, $2, $3)",
                "Acme Corp",
                "ops@acme.example",
                "apps/pos/README.md",
            )
        finally:
            await conn.close()

        # ---- ext.query: SELECT joining extension table with core table ----
        # First make a wiki page so the LEFT JOIN finds something.
        await call_handler_by_name(
            "wiki.write",
            {
                "path": "apps/pos/README.md",
                "content": (
                    "---\n"
                    "title: POS\n"
                    "type: app\n"
                    "owners: [pos]\n"
                    f"updated: {datetime.now(tz=UTC).date().isoformat()}\n"
                    "---\n\n"
                    "# POS app\n"
                ),
                "base_version": None,
                "session": {
                    "server_id": "server-test",
                    "app": "pos",
                    "agent_model": "pytest",
                    "session_id": "",
                },
            },
            db=db,
            wiki_dir=config.wiki_dir,
        )

        out = await call_handler_by_name(
            "ext.query",
            {
                "extension": "crm",
                "sql": (
                    "SELECT c.name AS contact, p.path AS page "
                    "FROM ext_crm_contacts c "
                    "LEFT JOIN pages p ON p.path = c.linked_page_path "
                    "ORDER BY c.name"
                ),
            },
            db=db,
            wiki_dir=config.wiki_dir,
        )
        assert out["columns"] == ["contact", "page"]
        assert out["row_count"] == 1
        assert out["rows"][0] == ["Acme Corp", "apps/pos/README.md"]
        assert out["truncated"] is False

        # ---- ext.query refuses mutations ----
        with pytest.raises(ValidationFailed, match="read-only"):
            await call_handler_by_name(
                "ext.query",
                {
                    "extension": "crm",
                    "sql": "DELETE FROM ext_crm_contacts",
                },
                db=db,
                wiki_dir=config.wiki_dir,
            )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_extension_role_isolation_enforces_policy(
    clean_database: str, tmp_path: Path
) -> None:
    """``can_read_audit=false`` means even a hand-crafted SELECT against
    ``audit`` from inside ``ext.query`` fails at the DB layer. Same
    holds for ``can_read_pages=false``."""
    from vaultmcp.master.config import MasterConfig
    from vaultmcp.master.db import Database
    from vaultmcp.master.tools import call_handler_by_name

    config = MasterConfig(database_url=clean_database, wiki_dir=tmp_path / "wiki")
    config.wiki_dir.mkdir()
    db = await Database.connect(config.database_url)
    try:
        await db.apply_schema(config.schema_sql_path)

        # Register an extension with NO read permissions on audit, NO
        # read on pages, only its own tables.
        await call_handler_by_name(
            "ext.register",
            {"name": "minimal", "policy": {}},
            db=db,
            wiki_dir=config.wiki_dir,
        )
        await call_handler_by_name(
            "ext.declare_table",
            {
                "extension": "minimal",
                "name": "items",
                "columns": [
                    {"name": "id", "type": "uuid", "primary_key": True},
                    {"name": "label", "type": "text"},
                ],
            },
            db=db,
            wiki_dir=config.wiki_dir,
        )

        # Querying its own table works.
        out = await call_handler_by_name(
            "ext.query",
            {
                "extension": "minimal",
                "sql": "SELECT count(*) FROM ext_minimal_items",
            },
            db=db,
            wiki_dir=config.wiki_dir,
        )
        assert out["row_count"] == 1

        # Querying audit (not granted) -> Postgres permission error.
        # The exact exception bubbles up from asyncpg; we just want
        # something that's NOT a successful response.
        with pytest.raises(Exception, match="permission denied"):
            await call_handler_by_name(
                "ext.query",
                {
                    "extension": "minimal",
                    "sql": "SELECT count(*) FROM audit",
                },
                db=db,
                wiki_dir=config.wiki_dir,
            )

        # Same for pages.
        with pytest.raises(Exception, match="permission denied"):
            await call_handler_by_name(
                "ext.query",
                {
                    "extension": "minimal",
                    "sql": "SELECT count(*) FROM pages",
                },
                db=db,
                wiki_dir=config.wiki_dir,
            )

        # Now register a second extension with can_read_pages=true and
        # confirm it CAN read pages but still cannot read audit.
        await call_handler_by_name(
            "ext.register",
            {
                "name": "reader",
                "policy": {"can_read_pages": True},
            },
            db=db,
            wiki_dir=config.wiki_dir,
        )
        out = await call_handler_by_name(
            "ext.query",
            {"extension": "reader", "sql": "SELECT count(*) FROM pages"},
            db=db,
            wiki_dir=config.wiki_dir,
        )
        assert out["row_count"] == 1
        with pytest.raises(Exception, match="permission denied"):
            await call_handler_by_name(
                "ext.query",
                {"extension": "reader", "sql": "SELECT count(*) FROM audit"},
                db=db,
                wiki_dir=config.wiki_dir,
            )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_ext_exec_mutates_only_within_role_grants(
    clean_database: str, tmp_path: Path
) -> None:
    """ext.exec runs INSERT/UPDATE/DELETE under the extension's role.
    The role only has DML on its own ext_*_* tables (and whatever the
    policy grants), so a hand-crafted UPDATE on pages still fails."""
    from vaultmcp.master.config import MasterConfig
    from vaultmcp.master.db import Database
    from vaultmcp.master.tools import call_handler_by_name

    config = MasterConfig(database_url=clean_database, wiki_dir=tmp_path / "wiki")
    config.wiki_dir.mkdir()
    db = await Database.connect(config.database_url)
    try:
        await db.apply_schema(config.schema_sql_path)
        await call_handler_by_name(
            "ext.register", {"name": "crm"}, db=db, wiki_dir=config.wiki_dir
        )
        await call_handler_by_name(
            "ext.declare_table",
            {
                "extension": "crm",
                "name": "contacts",
                "columns": [
                    {"name": "id", "type": "uuid", "primary_key": True},
                    {"name": "name", "type": "text", "not_null": True},
                ],
            },
            db=db,
            wiki_dir=config.wiki_dir,
        )

        # Mutation on its own table works.
        out = await call_handler_by_name(
            "ext.exec",
            {
                "extension": "crm",
                "sql": (
                    "INSERT INTO ext_crm_contacts (id, name) "
                    "VALUES (gen_random_uuid(), $1), (gen_random_uuid(), $2)"
                ),
                "params": ["Alice", "Bob"],
            },
            db=db,
            wiki_dir=config.wiki_dir,
        )
        assert out["rows_affected"] == 2

        # Same shape but targeting pages -> Postgres rejects.
        with pytest.raises(Exception, match="permission denied"):
            await call_handler_by_name(
                "ext.exec",
                {
                    "extension": "crm",
                    "sql": "UPDATE pages SET content = '' WHERE path = $1",
                    "params": ["x"],
                },
                db=db,
                wiki_dir=config.wiki_dir,
            )

        # And the SELECT through ext.query sees the rows we inserted.
        rows = await call_handler_by_name(
            "ext.query",
            {
                "extension": "crm",
                "sql": "SELECT name FROM ext_crm_contacts ORDER BY name",
            },
            db=db,
            wiki_dir=config.wiki_dir,
        )
        assert [r[0] for r in rows["rows"]] == ["Alice", "Bob"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_ext_embed_routes_through_the_worker(
    clean_database: str, tmp_path: Path
) -> None:
    """ext.embed inserts a synthetic page in the ``ext/<name>/`` namespace
    and enqueues an embedding job. With a configured embedder, the worker
    drains the job and a row appears in the embeddings table."""
    import asyncio as _asyncio
    import socket as _socket
    import time as _time

    import asyncpg as _ap
    import httpx as _httpx
    import uvicorn as _uvicorn

    from vaultmcp.master.config import MasterConfig
    from vaultmcp.master.server import build_app

    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    config = MasterConfig(
        database_url=clean_database,
        wiki_dir=tmp_path / "master_wiki",
        embedding_provider="null/sha-1024",
    )
    config.wiki_dir.mkdir()
    app = build_app(config)
    server = _uvicorn.Server(
        _uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"
        )
    )
    task = _asyncio.create_task(server.serve())
    try:
        deadline = _time.monotonic() + 10
        while not getattr(server, "started", False):
            if _time.monotonic() > deadline:
                raise AssertionError("master never started")
            await _asyncio.sleep(0.05)

        async with _httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            await client.post(
                "/mcp/call",
                json={
                    "tool": "ext.register",
                    "args": {
                        "name": "crm",
                        "policy": {"can_use_embeddings": True},
                    },
                },
            )
            r = await client.post(
                "/mcp/call",
                json={
                    "tool": "ext.embed",
                    "args": {
                        "extension": "crm",
                        "rel_path": "contacts/123",
                        "content": "Met at conference; interested in pos integration",
                    },
                },
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["path"] == "ext/crm/contacts/123"
            assert body["queued"] is True

        # Wait for the worker to embed.
        deadline = _time.monotonic() + 10
        conn = await _ap.connect(clean_database)
        try:
            row = None
            while _time.monotonic() < deadline:
                row = await conn.fetchrow(
                    "SELECT path, model, dim FROM embeddings WHERE path = $1",
                    "ext/crm/contacts/123",
                )
                if row is not None:
                    break
                await _asyncio.sleep(0.1)
            assert row is not None, "ext.embed never landed in embeddings"
            assert row["dim"] == 1024
        finally:
            await conn.close()
    finally:
        server.should_exit = True
        try:
            await _asyncio.wait_for(task, timeout=5)
        except TimeoutError:
            task.cancel()
            try:
                await task
            except (_asyncio.CancelledError, Exception):
                pass


@pytest.mark.asyncio
async def test_ext_emit_event_appears_in_events_feed(
    clean_database: str, tmp_path: Path
) -> None:
    """An emitted event lands in the events table with a namespaced
    type, and shows up in the dashboard's recent_events read path."""
    from vaultmcp.master.config import MasterConfig
    from vaultmcp.master.db import Database
    from vaultmcp.master.tools import call_handler_by_name

    config = MasterConfig(database_url=clean_database, wiki_dir=tmp_path / "wiki")
    config.wiki_dir.mkdir()
    db = await Database.connect(config.database_url)
    try:
        await db.apply_schema(config.schema_sql_path)
        await call_handler_by_name(
            "ext.register",
            {"name": "crm", "policy": {"can_subscribe_events": True}},
            db=db,
            wiki_dir=config.wiki_dir,
        )
        out = await call_handler_by_name(
            "ext.emit_event",
            {
                "extension": "crm",
                "event_type": "ContactCreated",
                "path": "ext/crm/contacts/123",
                "payload": {"contact_id": "123", "linked_page": "apps/pos/README.md"},
            },
            db=db,
            wiki_dir=config.wiki_dir,
        )
        assert out["namespaced_event_type"] == "ext_crm_ContactCreated"
        assert out["id"] > 0

        events = await db.recent_events(limit=10)
        kinds = {e["event_type"] for e in events}
        assert "ext_crm_ContactCreated" in kinds

        # And an extension WITHOUT can_subscribe_events cannot emit.
        await call_handler_by_name(
            "ext.register", {"name": "voiceless"}, db=db, wiki_dir=config.wiki_dir
        )
        with pytest.raises(Exception, match="permission denied"):
            await call_handler_by_name(
                "ext.emit_event",
                {"extension": "voiceless", "event_type": "x"},
                db=db,
                wiki_dir=config.wiki_dir,
            )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_ext_deregister_drops_tables_role_and_pages(
    clean_database: str, tmp_path: Path
) -> None:
    """Round-trip: register → declare table → embed → deregister →
    nothing left. Re-register under the same name then succeeds."""
    import asyncpg as _ap

    from vaultmcp.master.config import MasterConfig
    from vaultmcp.master.db import Database
    from vaultmcp.master.tools import call_handler_by_name

    config = MasterConfig(database_url=clean_database, wiki_dir=tmp_path / "wiki")
    config.wiki_dir.mkdir()
    db = await Database.connect(config.database_url)
    try:
        await db.apply_schema(config.schema_sql_path)

        await call_handler_by_name(
            "ext.register",
            {"name": "crm", "policy": {"can_use_embeddings": True}},
            db=db,
            wiki_dir=config.wiki_dir,
        )
        await call_handler_by_name(
            "ext.declare_table",
            {
                "extension": "crm",
                "name": "contacts",
                "columns": [
                    {"name": "id", "type": "uuid", "primary_key": True},
                    {"name": "name", "type": "text"},
                ],
            },
            db=db,
            wiki_dir=config.wiki_dir,
        )
        # Index a piece of ext content (creates a synthetic page +
        # embedding_jobs row).
        await call_handler_by_name(
            "ext.embed",
            {"extension": "crm", "rel_path": "contacts/123", "content": "hello"},
            db=db,
            wiki_dir=config.wiki_dir,
        )

        # Pre-deregister state.
        conn = await _ap.connect(clean_database)
        try:
            assert await conn.fetchval(
                "SELECT 1 FROM extensions WHERE name='crm'"
            )
            assert await conn.fetchval(
                "SELECT 1 FROM pg_tables WHERE tablename='ext_crm_contacts'"
            )
            assert await conn.fetchval(
                "SELECT 1 FROM pg_roles WHERE rolname='vaultmcp_ext_crm'"
            )
            page_count = await conn.fetchval(
                "SELECT count(*) FROM pages WHERE path LIKE 'ext/crm/%'"
            )
            assert page_count == 1
        finally:
            await conn.close()

        # Deregister.
        out = await call_handler_by_name(
            "ext.deregister", {"name": "crm"}, db=db, wiki_dir=config.wiki_dir
        )
        assert out["tables_dropped"] == 1
        assert out["pages_dropped"] == 1
        assert out["role_dropped"] is True

        # Post-deregister: nothing left in catalog.
        conn = await _ap.connect(clean_database)
        try:
            assert not await conn.fetchval(
                "SELECT 1 FROM extensions WHERE name='crm'"
            )
            assert not await conn.fetchval(
                "SELECT 1 FROM pg_tables WHERE tablename='ext_crm_contacts'"
            )
            assert not await conn.fetchval(
                "SELECT 1 FROM pg_roles WHERE rolname='vaultmcp_ext_crm'"
            )
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM pages WHERE path LIKE 'ext/crm/%'"
                )
                == 0
            )
        finally:
            await conn.close()

        # Idempotent.
        out2 = await call_handler_by_name(
            "ext.deregister", {"name": "crm"}, db=db, wiki_dir=config.wiki_dir
        )
        assert out2["tables_dropped"] == 0
        assert out2["pages_dropped"] == 0

        # Re-registering the same name now works (clean slate).
        await call_handler_by_name(
            "ext.register", {"name": "crm"}, db=db, wiki_dir=config.wiki_dir
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_extension_register_rejects_bad_prefix(
    clean_database: str, tmp_path: Path
) -> None:
    from vaultmcp.master.config import MasterConfig
    from vaultmcp.master.db import Database
    from vaultmcp.master.errors import ValidationFailed
    from vaultmcp.master.tools import call_handler_by_name

    config = MasterConfig(database_url=clean_database, wiki_dir=tmp_path / "wiki")
    config.wiki_dir.mkdir()
    db = await Database.connect(config.database_url)
    try:
        await db.apply_schema(config.schema_sql_path)

        # Prefix that doesn't start with ext_ → reject.
        with pytest.raises(ValidationFailed, match="ext_"):
            await call_handler_by_name(
                "ext.register",
                {"name": "crm", "schema_prefix": "crm_"},
                db=db,
                wiki_dir=config.wiki_dir,
            )

        # Prefix without trailing underscore → reject.
        with pytest.raises(ValidationFailed, match="end with"):
            await call_handler_by_name(
                "ext.register",
                {"name": "crm", "schema_prefix": "ext_crm"},
                db=db,
                wiki_dir=config.wiki_dir,
            )
    finally:
        await db.close()
