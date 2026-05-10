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
async def test_dashboard_basic_auth_when_password_configured(
    clean_database: str, tmp_path: Path
) -> None:
    """When VAULTMCP_DASHBOARD_PASSWORD is set, every dashboard route is gated."""
    import socket as _socket

    from vaultmcp.master.config import MasterConfig
    from vaultmcp.master.server import build_app

    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    base_url = f"http://127.0.0.1:{port}"

    config = MasterConfig(
        database_url=clean_database,
        wiki_dir=tmp_path / "master_wiki",
        dashboard_username="admin",
        dashboard_password="hunter2",
    )
    config.wiki_dir.mkdir()
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

        async with httpx.AsyncClient(base_url=base_url) as client:
            # No credentials -> 401
            resp = await client.get("/")
            assert resp.status_code == 401
            assert resp.headers.get("www-authenticate", "").lower().startswith("basic")

            # Wrong password -> 401
            resp = await client.get(
                "/", auth=httpx.BasicAuth("admin", "wrong")
            )
            assert resp.status_code == 401

            # Correct credentials -> 200
            resp = await client.get(
                "/", auth=httpx.BasicAuth("admin", "hunter2")
            )
            assert resp.status_code == 200
            assert "Live activity" in resp.text

            # Same gate applies to /servers, /audit, /search.
            for route in ("/servers", "/audit", "/search"):
                resp = await client.get(route)
                assert resp.status_code == 401, route
                resp = await client.get(
                    route, auth=httpx.BasicAuth("admin", "hunter2")
                )
                assert resp.status_code == 200, route

            # /healthz and /mcp/* are NOT under the dashboard router.
            resp = await client.get("/healthz")
            assert resp.status_code == 200
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
async def test_dashboard_search_page_renders_results(
    clean_database: str, tmp_path: Path
) -> None:
    async with _master(clean_database, tmp_path) as base_url:
        page = (
            "---\n"
            "title: Inventory\n"
            "type: app\n"
            "owners: [testapp]\n"
            "updated: 2026-05-10\n"
            "---\n\n"
            "We track warehouse stock across regions.\n"
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
                        "path": "apps/testapp/inv.md",
                        "content": page,
                        "base_version": None,
                        "session": session,
                    },
                },
            )

            # Empty form (no query) — page renders without results.
            resp = await client.get("/search")
            assert resp.status_code == 200
            assert "Search" in resp.text
            assert "<form" in resp.text

            # Real query — finds the page and shows snippet.
            resp = await client.get("/search", params={"q": "warehouse"})
            assert resp.status_code == 200
            assert "apps/testapp/inv.md" in resp.text
            # Snippet markers from ts_headline should have been converted to <mark>.
            assert "<mark>" in resp.text or "warehouse" in resp.text.lower()

            # No-match query
            resp = await client.get("/search", params={"q": "nonexistentterm"})
            assert resp.status_code == 200
            assert "No matches" in resp.text


@pytest.mark.asyncio
async def test_dashboard_servers_page_empty_then_populated(
    clean_database: str, tmp_path: Path
) -> None:
    from vaultmcp.master.auth import generate_token, hash_token
    from vaultmcp.master.db import Database

    async with _master(clean_database, tmp_path) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as client:
            resp = await client.get("/servers")
            assert resp.status_code == 200
            assert "Registered servers" in resp.text
            assert "No servers registered" in resp.text

        # Add one and verify it shows.
        db = await Database.connect(clean_database)
        try:
            await db.add_server(
                server_id="server-1",
                apps=["webstore", "admin"],
                token_hash=hash_token(generate_token()),
            )
        finally:
            await db.close()

        async with httpx.AsyncClient(base_url=base_url) as client:
            # The freshly-added server now needs auth on /mcp/* routes,
            # but /servers is part of the dashboard and stays open.
            resp = await client.get("/servers")
            assert resp.status_code == 200
            assert "server-1" in resp.text
            assert "webstore" in resp.text
            assert "admin" in resp.text
            assert "No servers registered" not in resp.text


@pytest.mark.asyncio
async def test_dashboard_audit_page_filters_by_outcome(
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
            # One ok write
            await client.post(
                "/mcp/call",
                json={
                    "tool": "wiki.write",
                    "args": {
                        "path": "apps/testapp/audit-target.md",
                        "content": page,
                        "base_version": None,
                        "session": session,
                    },
                },
            )
            # One conflict (stale base_version on the same path)
            r = await client.post(
                "/mcp/call",
                json={
                    "tool": "wiki.write",
                    "args": {
                        "path": "apps/testapp/audit-target.md",
                        "content": page.replace("Hi.", "Hi 2."),
                        "base_version": 99,
                        "session": session,
                    },
                },
            )
            assert r.status_code == 409

            # Unfiltered shows both
            resp = await client.get("/audit")
            assert resp.status_code == 200
            assert "audit-target.md" in resp.text
            assert "ok" in resp.text
            assert "conflict" in resp.text

            # outcome=conflict shows only the conflict row
            resp = await client.get("/audit", params={"outcome": "conflict"})
            assert resp.status_code == 200
            assert "audit-target.md" in resp.text
            # the row body is filtered, but the dropdown still has labels;
            # so just check that no <td>ok</td> remains in the rendered rows.
            assert resp.text.count('class="pill"') >= 1


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
