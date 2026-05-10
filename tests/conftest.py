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
    """Drop+recreate the `public` schema so each e2e test starts from zero.

    Tests that assert version numbers (`assert version == 1`) or count
    rows depend on a clean slate; without this fixture, a second run
    would see leftover data from the first.
    """
    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        await conn.execute("CREATE SCHEMA public")
    finally:
        await conn.close()
    yield database_url
