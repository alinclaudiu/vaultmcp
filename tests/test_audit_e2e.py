"""End-to-end test for the wiki.audit MCP tool.

Exercises the audit row that ``handle_write`` already inserts and
verifies the new tool surfaces it back through ``call_handler_by_name``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e


def _valid_page(title: str = "Notes") -> str:
    return (
        "---\n"
        f"title: {title}\n"
        "type: app\n"
        "owners: [testapp]\n"
        "updated: 2026-05-10\n"
        "---\n\n"
        "# Body.\n"
    )


@pytest.mark.asyncio
async def test_audit_returns_recent_writes(
    clean_database: str, tmp_path: Path
) -> None:
    from vaultmcp.master.config import MasterConfig
    from vaultmcp.master.db import Database
    from vaultmcp.master.tools import call_handler_by_name
    from vaultmcp.shared.types import AuditOutput

    config = MasterConfig(database_url=clean_database, wiki_dir=tmp_path / "wiki")
    config.wiki_dir.mkdir()
    db = await Database.connect(config.database_url)
    try:
        await db.apply_schema(config.schema_sql_path)

        session = {
            "server_id": "server-test",
            "app": "testapp",
            "agent_model": "pytest",
            "session_id": "",
        }
        await call_handler_by_name(
            "wiki.write",
            {
                "path": "apps/testapp/a.md",
                "content": _valid_page("A"),
                "base_version": None,
                "session": session,
            },
            db=db,
            wiki_dir=config.wiki_dir,
        )
        await call_handler_by_name(
            "wiki.write",
            {
                "path": "apps/testapp/b.md",
                "content": _valid_page("B"),
                "base_version": None,
                "session": session,
            },
            db=db,
            wiki_dir=config.wiki_dir,
        )

        # All audit rows
        out = AuditOutput.model_validate(
            await call_handler_by_name(
                "wiki.audit", {}, db=db, wiki_dir=config.wiki_dir
            )
        )
        ops_paths = {(e.operation, e.path) for e in out.entries}
        assert ("write", "apps/testapp/a.md") in ops_paths
        assert ("write", "apps/testapp/b.md") in ops_paths
        # Newest first.
        assert out.entries[0].ts >= out.entries[-1].ts

        # Filter by path
        only_a = AuditOutput.model_validate(
            await call_handler_by_name(
                "wiki.audit",
                {"path": "apps/testapp/a.md"},
                db=db,
                wiki_dir=config.wiki_dir,
            )
        )
        assert {e.path for e in only_a.entries} == {"apps/testapp/a.md"}

        # Filter by server_id (the writes used server-test)
        only_test = AuditOutput.model_validate(
            await call_handler_by_name(
                "wiki.audit",
                {"server_id": "server-test"},
                db=db,
                wiki_dir=config.wiki_dir,
            )
        )
        assert all(e.server_id == "server-test" for e in only_test.entries)

        # since-filter that excludes everything (future timestamp)
        future = (datetime.now(tz=UTC) + timedelta(hours=1)).isoformat()
        none_yet = AuditOutput.model_validate(
            await call_handler_by_name(
                "wiki.audit",
                {"since": future},
                db=db,
                wiki_dir=config.wiki_dir,
            )
        )
        assert none_yet.entries == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_audit_records_failed_writes(
    clean_database: str, tmp_path: Path
) -> None:
    """Conflict outcomes should also land in audit, not just successes."""
    from vaultmcp.master.config import MasterConfig
    from vaultmcp.master.db import Database
    from vaultmcp.master.errors import ConflictError
    from vaultmcp.master.tools import call_handler_by_name
    from vaultmcp.shared.types import AuditOutput

    config = MasterConfig(database_url=clean_database, wiki_dir=tmp_path / "wiki")
    config.wiki_dir.mkdir()
    db = await Database.connect(config.database_url)
    try:
        await db.apply_schema(config.schema_sql_path)

        session = {
            "server_id": "server-test",
            "app": "testapp",
            "agent_model": "pytest",
            "session_id": "",
        }
        await call_handler_by_name(
            "wiki.write",
            {
                "path": "apps/testapp/c.md",
                "content": _valid_page("C v1"),
                "base_version": None,
                "session": session,
            },
            db=db,
            wiki_dir=config.wiki_dir,
        )
        # Now write with a stale base_version -> conflict.
        with pytest.raises(ConflictError):
            await call_handler_by_name(
                "wiki.write",
                {
                    "path": "apps/testapp/c.md",
                    "content": _valid_page("C v2 stale"),
                    "base_version": 99,
                    "session": session,
                },
                db=db,
                wiki_dir=config.wiki_dir,
            )

        out = AuditOutput.model_validate(
            await call_handler_by_name(
                "wiki.audit",
                {"path": "apps/testapp/c.md"},
                db=db,
                wiki_dir=config.wiki_dir,
            )
        )
        outcomes = [e.outcome for e in out.entries]
        # Newest-first: the conflict is at index 0, the ok at index 1.
        assert outcomes[0] == "conflict"
        assert "ok" in outcomes
    finally:
        await db.close()
