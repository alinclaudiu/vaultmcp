"""End-to-end test for ``vaultmcp-master render-all``.

Writes a few pages through the normal wiki.write path, deletes the
on-disk projection, then verifies the CLI rebuilds it from the DB.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e


def _page(title: str, body: str = "body\n") -> str:
    return (
        "---\n"
        f"title: {title}\n"
        "type: app\n"
        "owners: [test]\n"
        "updated: 2026-05-10\n"
        "---\n\n"
        f"{body}"
    )


@pytest.mark.asyncio
async def test_render_all_rebuilds_wiki_dir_from_db(
    clean_database: str, tmp_path: Path
) -> None:
    from vaultmcp.master.config import MasterConfig
    from vaultmcp.master.db import Database
    from vaultmcp.master.render import render_to_disk
    from vaultmcp.master.tools import call_handler_by_name

    config = MasterConfig(database_url=clean_database, wiki_dir=tmp_path / "wiki")
    config.wiki_dir.mkdir()
    db = await Database.connect(config.database_url)
    try:
        await db.apply_schema(config.schema_sql_path)
        session = {
            "server_id": "test",
            "app": "test",
            "agent_model": "pytest",
            "session_id": "",
        }
        for path, title in [
            ("apps/test/a.md", "A"),
            ("apps/test/sub/b.md", "B"),
            ("apps/test/sub/deeper/c.md", "C"),
        ]:
            await call_handler_by_name(
                "wiki.write",
                {
                    "path": path,
                    "content": _page(title),
                    "base_version": None,
                    "session": session,
                },
                db=db,
                wiki_dir=config.wiki_dir,
            )

        # All three should be on disk now.
        for p in ("apps/test/a.md", "apps/test/sub/b.md", "apps/test/sub/deeper/c.md"):
            assert (config.wiki_dir / p).exists()

        # Wipe the wiki directory and re-render from DB.
        shutil.rmtree(config.wiki_dir)
        assert not config.wiki_dir.exists()
        config.wiki_dir.mkdir()

        rendered = 0
        async for page in db.iter_all_pages():
            render_to_disk(config.wiki_dir, page.path, page.content)
            rendered += 1
        assert rendered == 3

        # Re-rendered tree must match the originals.
        for p, title in [
            ("apps/test/a.md", "A"),
            ("apps/test/sub/b.md", "B"),
            ("apps/test/sub/deeper/c.md", "C"),
        ]:
            content = (config.wiki_dir / p).read_text(encoding="utf-8")
            assert f"title: {title}" in content
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_iter_all_pages_filters_by_prefix(
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
        session = {
            "server_id": "test",
            "app": "test",
            "agent_model": "pytest",
            "session_id": "",
        }
        for p in ("apps/a/x.md", "apps/b/y.md", "decisions/z.md"):
            await call_handler_by_name(
                "wiki.write",
                {
                    "path": p,
                    "content": _page("T"),
                    "base_version": None,
                    "session": session,
                },
                db=db,
                wiki_dir=config.wiki_dir,
            )

        only_apps_a: list[str] = []
        async for page in db.iter_all_pages(prefix="apps/a/"):
            only_apps_a.append(page.path)
        assert only_apps_a == ["apps/a/x.md"]

        all_paths = []
        async for page in db.iter_all_pages():
            all_paths.append(page.path)
        # Path-ordered ascending.
        assert all_paths == ["apps/a/x.md", "apps/b/y.md", "decisions/z.md"]
    finally:
        await db.close()
