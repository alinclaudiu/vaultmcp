"""Pytest configuration shared across tests."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio


def pytest_configure(config) -> None:  # type: ignore[no-untyped-def]
    config.addinivalue_line(
        "markers", "e2e: end-to-end test requiring a running Postgres"
    )


@pytest.fixture
def database_url() -> str:
    """Return the test DB URL or skip if it isn't configured."""
    url = os.environ.get("VAULTMCP_TEST_DATABASE_URL")
    if not url:
        pytest.skip("VAULTMCP_TEST_DATABASE_URL not set; skipping e2e")
    return url


@pytest_asyncio.fixture
async def clean_database(database_url: str) -> AsyncIterator[str]:
    """Drop the master's tables/sequences so each e2e test starts from zero.

    Tests that assert version numbers (``assert version == 1``) or count
    rows depend on a clean slate; without this fixture, a second run
    would see leftover data from the first.

    We can't ``DROP SCHEMA public CASCADE`` because that would also
    remove the ``vector`` extension (installed by a superuser as a
    one-shot deploy step), and recreating it requires superuser
    privileges the test role doesn't have. Dropping the master's tables
    explicitly leaves the extension intact.
    """
    core_tables = (
        "embedding_jobs",
        "embeddings",
        "audit",
        "events",
        "subscriptions",
        "log_entries",
        "pages",
        "servers",
        "extensions",
    )
    conn = await asyncpg.connect(database_url)
    try:
        # Extension tables (ext_<name>_*) are created at runtime by
        # ext.declare_table. Discover and drop them before the core
        # tables so subsequent runs see a truly empty schema.
        ext_tables = [
            r["tablename"]
            for r in await conn.fetch(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename LIKE 'ext_%'"
            )
        ]
        for tbl in ext_tables:
            await conn.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")
        for tbl in core_tables:
            await conn.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")
        await conn.execute("DROP SEQUENCE IF EXISTS global_version_seq CASCADE")
    finally:
        await conn.close()
    yield database_url
