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
