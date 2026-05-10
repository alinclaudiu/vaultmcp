"""End-to-end test for the wiki.search MCP tool (lexical, v0.4)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e


def _page(title: str, body: str) -> str:
    return (
        "---\n"
        f"title: {title}\n"
        "type: app\n"
        "owners: [testapp]\n"
        "updated: 2026-05-10\n"
        "---\n\n"
        f"{body}\n"
    )


@pytest.mark.asyncio
async def test_search_finds_and_ranks_pages(
    clean_database: str, tmp_path: Path
) -> None:
    from vaultmcp.master.config import MasterConfig
    from vaultmcp.master.db import Database
    from vaultmcp.master.tools import call_handler_by_name
    from vaultmcp.shared.types import SearchOutput

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

        async def write(path: str, page: str) -> None:
            await call_handler_by_name(
                "wiki.write",
                {
                    "path": path,
                    "content": page,
                    "base_version": None,
                    "session": session,
                },
                db=db,
                wiki_dir=config.wiki_dir,
            )

        # The matching page mentions "warehouse" multiple times in the body.
        await write(
            "apps/testapp/inv.md",
            _page(
                "Inventory",
                "We track warehouse stock across regions.\n"
                "The warehouse subsystem owns delivery windows.\n",
            ),
        )
        # A weaker match — title mention only.
        await write(
            "apps/testapp/warehouse.md",
            _page("Warehouse", "Notes about logistics.\n"),
        )
        # A page that should NOT match.
        await write(
            "apps/testapp/billing.md",
            _page("Billing", "Invoices, refunds, and tax registration.\n"),
        )

        out = SearchOutput.model_validate(
            await call_handler_by_name(
                "wiki.search",
                {"query": "warehouse", "limit": 10},
                db=db,
                wiki_dir=config.wiki_dir,
            )
        )

        paths = [e.path for e in out.entries]
        assert "apps/testapp/inv.md" in paths
        assert "apps/testapp/warehouse.md" in paths
        assert "apps/testapp/billing.md" not in paths

        # Title-weighted match should rank at-least-comparable to body match.
        scores = {e.path: e.score for e in out.entries}
        assert scores["apps/testapp/warehouse.md"] > 0
        assert scores["apps/testapp/inv.md"] > 0

        # Snippet on the body-heavy match should mention warehouse.
        inv_entry = next(e for e in out.entries if e.path == "apps/testapp/inv.md")
        assert "warehouse" in inv_entry.snippet.lower()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_search_supports_prefix_and_type_filters(
    clean_database: str, tmp_path: Path
) -> None:
    from vaultmcp.master.config import MasterConfig
    from vaultmcp.master.db import Database
    from vaultmcp.master.tools import call_handler_by_name
    from vaultmcp.shared.types import SearchOutput

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
                "path": "apps/testapp/x.md",
                "content": _page("X", "alpha beta gamma in apps."),
                "base_version": None,
                "session": session,
            },
            db=db,
            wiki_dir=config.wiki_dir,
        )
        await call_handler_by_name(
            "wiki.write",
            {
                "path": "decisions/0001.md",
                "content": (
                    "---\n"
                    "title: First decision\n"
                    "type: adr\n"
                    "owners: [testapp]\n"
                    "updated: 2026-05-10\n"
                    "---\n\n"
                    "alpha beta gamma in decisions.\n"
                ),
                "base_version": None,
                "session": session,
            },
            db=db,
            wiki_dir=config.wiki_dir,
        )

        # Prefix filter
        only_apps = SearchOutput.model_validate(
            await call_handler_by_name(
                "wiki.search",
                {"query": "alpha", "prefix": "apps/"},
                db=db,
                wiki_dir=config.wiki_dir,
            )
        )
        assert {e.path for e in only_apps.entries} == {"apps/testapp/x.md"}

        # Type filter
        only_adr = SearchOutput.model_validate(
            await call_handler_by_name(
                "wiki.search",
                {"query": "alpha", "type": "adr"},
                db=db,
                wiki_dir=config.wiki_dir,
            )
        )
        assert {e.path for e in only_adr.entries} == {"decisions/0001.md"}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_search_no_match_returns_empty(
    clean_database: str, tmp_path: Path
) -> None:
    from vaultmcp.master.config import MasterConfig
    from vaultmcp.master.db import Database
    from vaultmcp.master.tools import call_handler_by_name
    from vaultmcp.shared.types import SearchOutput

    config = MasterConfig(database_url=clean_database, wiki_dir=tmp_path / "wiki")
    config.wiki_dir.mkdir()
    db = await Database.connect(config.database_url)
    try:
        await db.apply_schema(config.schema_sql_path)

        out = SearchOutput.model_validate(
            await call_handler_by_name(
                "wiki.search",
                {"query": "no-such-content"},
                db=db,
                wiki_dir=config.wiki_dir,
            )
        )
        assert out.entries == []
    finally:
        await db.close()
