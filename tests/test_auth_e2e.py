"""End-to-end tests for bearer-token auth + per-server authorization.

Boots the master HTTP server in-process and drives it through
:class:`MasterClient`. The master starts in unauthenticated dev mode
(empty ``servers`` table); adding a server flips auth on for every
subsequent request.
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


async def _wait_started(server: uvicorn.Server, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not getattr(server, "started", False):
        if time.monotonic() > deadline:
            raise AssertionError("master never reached started=True")
        await asyncio.sleep(0.05)


@asynccontextmanager
async def _master(database_url: str, tmp_path: Path) -> AsyncIterator[str]:
    """Boot master in-process, yield its base URL, tear it down on exit."""
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
        await _wait_started(server)
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


def _page_payload(server_id: str, app: str) -> dict:
    return {
        "tool": "wiki.write",
        "args": {
            "path": "apps/testapp/notes.md",
            "content": (
                "---\n"
                "title: Notes\n"
                "type: app\n"
                "owners: [testapp]\n"
                "updated: 2026-05-10\n"
                "---\n\n"
                "# Hi.\n"
            ),
            "base_version": None,
            "session": {
                "server_id": server_id,
                "app": app,
                "agent_model": "pytest",
                "session_id": "",
            },
        },
    }


@pytest.mark.asyncio
async def test_dev_mode_bypasses_auth_when_no_servers(
    clean_database: str, tmp_path: Path
) -> None:
    """With zero registered servers the master accepts unauth'd writes."""
    async with _master(clean_database, tmp_path) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as client:
            resp = await client.post(
                "/mcp/call", json=_page_payload("server-anything", "testapp")
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["version"] == 1


@pytest.mark.asyncio
async def test_auth_required_once_a_server_is_registered(
    clean_database: str, tmp_path: Path
) -> None:
    from vaultmcp.master.auth import generate_token, hash_token
    from vaultmcp.master.db import Database

    db = await Database.connect(clean_database)
    try:
        await db.apply_schema(
            Path(__file__).resolve().parent.parent
            / "src" / "vaultmcp" / "master" / "schema.sql"
        )
        valid_token = generate_token()
        await db.add_server(
            server_id="server-a",
            apps=["testapp"],
            token_hash=hash_token(valid_token),
        )
    finally:
        await db.close()

    async with _master(clean_database, tmp_path) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as client:
            # Missing token -> 401
            resp = await client.post(
                "/mcp/call", json=_page_payload("server-a", "testapp")
            )
            assert resp.status_code == 401
            assert resp.headers.get("www-authenticate", "").startswith("Bearer")

            # Wrong token -> 401
            resp = await client.post(
                "/mcp/call",
                json=_page_payload("server-a", "testapp"),
                headers={"Authorization": "Bearer not-the-real-token"},
            )
            assert resp.status_code == 401

            # Right token but session claims another server -> 403
            resp = await client.post(
                "/mcp/call",
                json=_page_payload("server-impostor", "testapp"),
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert resp.status_code == 403, resp.text

            # Right token but app not in allowed list -> 403
            resp = await client.post(
                "/mcp/call",
                json=_page_payload("server-a", "another-app"),
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert resp.status_code == 403, resp.text

            # Right token + matching session -> 200
            resp = await client.post(
                "/mcp/call",
                json=_page_payload("server-a", "testapp"),
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["version"] == 1


@pytest.mark.asyncio
async def test_subscribe_requires_token_when_servers_exist(
    clean_database: str, tmp_path: Path
) -> None:
    from vaultmcp.master.auth import generate_token, hash_token
    from vaultmcp.master.db import Database

    db = await Database.connect(clean_database)
    try:
        await db.apply_schema(
            Path(__file__).resolve().parent.parent
            / "src" / "vaultmcp" / "master" / "schema.sql"
        )
        await db.add_server(
            server_id="server-a",
            apps=["testapp"],
            token_hash=hash_token(generate_token()),
        )
    finally:
        await db.close()

    async with _master(clean_database, tmp_path) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as client:
            resp = await client.get("/mcp/subscribe")
            assert resp.status_code == 401
