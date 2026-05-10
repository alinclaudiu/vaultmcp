"""End-to-end test for the embedding worker.

Boots the master with the null embedding provider, writes a page, and
waits for the worker to drain the queue and populate the embeddings
table.
"""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import httpx
import pytest
import uvicorn

pytestmark = pytest.mark.e2e


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@asynccontextmanager
async def _master_with_worker(database_url: str, tmp_path: Path) -> AsyncIterator[str]:
    from vaultmcp.master.config import MasterConfig
    from vaultmcp.master.server import build_app

    port = _free_port()
    wiki_dir = tmp_path / "master_wiki"
    wiki_dir.mkdir()
    config = MasterConfig(
        database_url=database_url,
        wiki_dir=wiki_dir,
        embedding_provider="null/sha-1024",
    )
    app = build_app(config)
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"
        )
    )
    task = asyncio.create_task(server.serve())
    try:
        deadline = time.monotonic() + 10
        while not getattr(server, "started", False):
            if time.monotonic() > deadline:
                raise AssertionError("master never started")
            await asyncio.sleep(0.05)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=5)
        except TimeoutError:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


@pytest.mark.asyncio
async def test_worker_embeds_pages_after_write(
    clean_database: str, tmp_path: Path
) -> None:
    page = (
        "---\n"
        "title: Notes\n"
        "type: app\n"
        "owners: [testapp]\n"
        "updated: 2026-05-10\n"
        "---\n\n"
        "# Hello.\n"
    )
    session = {
        "server_id": "server-test",
        "app": "testapp",
        "agent_model": "pytest",
        "session_id": "",
    }

    async with _master_with_worker(clean_database, tmp_path) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as client:
            r = await client.post(
                "/mcp/call",
                json={
                    "tool": "wiki.write",
                    "args": {
                        "path": "apps/testapp/note.md",
                        "content": page,
                        "base_version": None,
                        "session": session,
                    },
                },
            )
            assert r.status_code == 200, r.text

        # Poll the embeddings table until the row appears.
        conn = await asyncpg.connect(clean_database)
        try:
            deadline = time.monotonic() + 10
            row = None
            while time.monotonic() < deadline:
                row = await conn.fetchrow(
                    "SELECT path, version, model, dim FROM embeddings WHERE path = $1",
                    "apps/testapp/note.md",
                )
                if row is not None:
                    break
                await asyncio.sleep(0.1)
            assert row is not None, "embedding never landed"
            assert row["version"] == 1
            assert row["model"] == "null/sha-1024"
            assert row["dim"] == 1024

            remaining = await conn.fetchval(
                "SELECT count(*) FROM embedding_jobs WHERE path = $1",
                "apps/testapp/note.md",
            )
            assert remaining == 0, "job should have been deleted on success"
        finally:
            await conn.close()


@pytest.mark.asyncio
async def test_worker_off_when_provider_unconfigured(
    clean_database: str, tmp_path: Path
) -> None:
    """Without VAULTMCP_EMBEDDING_PROVIDER, jobs accumulate but no embedding lands."""
    from vaultmcp.master.config import MasterConfig
    from vaultmcp.master.server import build_app

    port = _free_port()
    wiki_dir = tmp_path / "master_wiki"
    wiki_dir.mkdir()
    config = MasterConfig(database_url=clean_database, wiki_dir=wiki_dir)
    app = build_app(config)
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"
        )
    )
    task = asyncio.create_task(server.serve())
    try:
        deadline = time.monotonic() + 10
        while not getattr(server, "started", False):
            if time.monotonic() > deadline:
                raise AssertionError("master never started")
            await asyncio.sleep(0.05)

        page = (
            "---\n"
            "title: Notes\n"
            "type: app\n"
            "owners: [testapp]\n"
            "updated: 2026-05-10\n"
            "---\n\n"
            "# Hi.\n"
        )
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            await client.post(
                "/mcp/call",
                json={
                    "tool": "wiki.write",
                    "args": {
                        "path": "apps/testapp/x.md",
                        "content": page,
                        "base_version": None,
                        "session": {
                            "server_id": "server-test",
                            "app": "testapp",
                            "agent_model": "pytest",
                            "session_id": "",
                        },
                    },
                },
            )

        # Give the (non-existent) worker a chance to do nothing.
        await asyncio.sleep(0.5)

        conn = await asyncpg.connect(clean_database)
        try:
            queued = await conn.fetchval(
                "SELECT count(*) FROM embedding_jobs WHERE path = $1",
                "apps/testapp/x.md",
            )
            embedded = await conn.fetchval(
                "SELECT count(*) FROM embeddings WHERE path = $1",
                "apps/testapp/x.md",
            )
            assert queued == 1
            assert embedded == 0
        finally:
            await conn.close()
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=5)
        except TimeoutError:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
