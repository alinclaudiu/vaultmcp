"""End-to-end test for the dashboard router.

Boots the master in-process, hits ``GET /``, and checks that the
rendered HTML reflects whatever events live in the DB.
"""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn

pytestmark = pytest.mark.e2e


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@asynccontextmanager
async def _master(database_url: str, tmp_path: Path) -> AsyncIterator[str]:
    from vaultmcp.master.config import MasterConfig
    from vaultmcp.master.server import build_app

    port = _free_port()
    wiki_dir = tmp_path / "master_wiki"
    wiki_dir.mkdir()
    config = MasterConfig(database_url=database_url, wiki_dir=wiki_dir)
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
async def test_dashboard_renders_empty_activity(
    clean_database: str, tmp_path: Path
) -> None:
    async with _master(clean_database, tmp_path) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as client:
            resp = await client.get("/")
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/html")
            body = resp.text
            assert "VaultMCP" in body
            assert "Live activity" in body
            assert "No events yet" in body


@pytest.mark.asyncio
async def test_dashboard_renders_activity_after_writes(
    clean_database: str, tmp_path: Path
) -> None:
    async with _master(clean_database, tmp_path) as base_url:
        page = (
            "---\n"
            "title: Test\n"
            "type: app\n"
            "owners: [testapp]\n"
            "updated: 2026-05-10\n"
            "---\n\n"
            "# Hi.\n"
        )
        session = {
            "server_id": "server-test",
            "app": "testapp",
            "agent_model": "pytest",
            "session_id": "",
        }
        async with httpx.AsyncClient(base_url=base_url) as client:
            await client.post(
                "/mcp/call",
                json={
                    "tool": "wiki.write",
                    "args": {
                        "path": "apps/testapp/dash.md",
                        "content": page,
                        "base_version": None,
                        "session": session,
                    },
                },
            )

            resp = await client.get("/")
            assert resp.status_code == 200
            body = resp.text
            assert "PageChanged" in body
            assert "apps/testapp/dash.md" in body
            assert "No events yet" not in body

            # The HTMX swap target also returns a fragment with the same row.
            resp = await client.get("/dashboard/activity-rows")
            assert resp.status_code == 200
            assert "apps/testapp/dash.md" in resp.text
            assert "<html" not in resp.text  # fragment, not a full page
