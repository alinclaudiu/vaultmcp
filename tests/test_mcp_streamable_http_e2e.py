"""End-to-end test for the network MCP transport (streamable-HTTP)."""

from __future__ import annotations

import asyncio
import json
import socket
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

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
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    app = build_app(MasterConfig(database_url=database_url, wiki_dir=wiki_dir))
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
async def test_mcp_streamable_http_round_trip(
    clean_database: str, tmp_path: Path
) -> None:
    async with _master(clean_database, tmp_path) as base_url:
        url = f"{base_url}/mcp/streamable/"
        async with streamable_http_client(url) as (read_stream, write_stream, _get_session_id):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                assert {"wiki.read", "wiki.write", "wiki.search"} <= names

                page = (
                    "---\n"
                    "title: Streamable HTTP\n"
                    "type: app\n"
                    "owners: [t]\n"
                    "updated: 2026-05-10\n"
                    "---\n\nbody\n"
                )
                w = await session.call_tool(
                    "wiki.write",
                    {
                        "path": "apps/t/streamable.md",
                        "content": page,
                        "base_version": None,
                        "session": {
                            "server_id": "t",
                            "app": "t",
                            "agent_model": "pytest",
                            "session_id": "",
                        },
                    },
                )
                assert w.isError is False, w
                assert json.loads(w.content[0].text)["version"] == 1

                r = await session.call_tool(
                    "wiki.read", {"path": "apps/t/streamable.md"}
                )
                assert r.isError is False
                payload = json.loads(r.content[0].text)
                assert payload["content"] == page
