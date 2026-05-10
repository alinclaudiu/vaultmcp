"""End-to-end test scaffolding.

Requires a running Postgres (the docker-compose recipe in deploy/ provides
one). Marked ``e2e`` so unit-only runs skip it by default::

    pytest -m "not e2e"        # unit only (CI default)
    pytest -m "e2e"             # e2e only
    pytest                      # both

The actual implementation is intentionally minimal — boots the master in
the same process as the test, drives it via the HTTP API, asserts core
behaviors. A multi-process docker-compose variant lands in a follow-up
once the basic flow is green.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


@pytest.mark.asyncio
async def test_write_then_read(clean_database: str, tmp_path) -> None:
    from vaultmcp.master.config import MasterConfig
    from vaultmcp.master.db import Database
    from vaultmcp.master.tools import handle_read, handle_write
    from vaultmcp.shared.types import ReadInput, Session, WriteInput

    config = MasterConfig(database_url=clean_database, wiki_dir=tmp_path / "wiki")
    config.wiki_dir.mkdir(parents=True, exist_ok=True)

    db = await Database.connect(config.database_url)
    try:
        await db.apply_schema(config.schema_sql_path)

        session = Session(
            server_id="server-test",
            app="testapp",
            agent_model="pytest",
        )
        write_in = WriteInput(
            path="apps/testapp/README.md",
            content=(
                "---\n"
                "title: Test\n"
                "type: app\n"
                "owners: [testapp]\n"
                "updated: 2026-05-09\n"
                "---\n\n"
                "# Hello.\n"
            ),
            base_version=None,
            session=session,
        )
        write_out = await handle_write(db, config.wiki_dir, write_in)
        assert write_out.version == 1

        read_out = await handle_read(db, ReadInput(path="apps/testapp/README.md"))
        assert read_out.version == 1
        assert "Hello." in read_out.content

        # On-disk file must exist.
        target = config.wiki_dir / "apps" / "testapp" / "README.md"
        assert target.exists()
    finally:
        await db.close()
