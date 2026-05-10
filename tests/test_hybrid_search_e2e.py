"""End-to-end test for semantic + hybrid search modes."""

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
        embedding_provider="null/sha-1536",
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


async def _wait_embeddings(database_url: str, expected: int) -> None:
    deadline = time.monotonic() + 10
    conn = await asyncpg.connect(database_url)
    try:
        while time.monotonic() < deadline:
            n = await conn.fetchval("SELECT count(*) FROM embeddings")
            if n >= expected:
                return
            await asyncio.sleep(0.1)
        raise AssertionError(f"only {n} of {expected} embeddings landed")
    finally:
        await conn.close()


def _payload(path: str, content: str) -> dict:
    return {
        "tool": "wiki.write",
        "args": {
            "path": path,
            "content": content,
            "base_version": None,
            "session": {
                "server_id": "server-test",
                "app": "testapp",
                "agent_model": "pytest",
                "session_id": "",
            },
        },
    }


@pytest.mark.asyncio
async def test_semantic_search_returns_self_similarity_top1(
    clean_database: str, tmp_path: Path
) -> None:
    """With the deterministic Null provider, a page is its own best match
    when the query is its own content."""
    target_content = _page("Inventory", "warehouse stock across regions")
    other_content = _page("Billing", "invoices and refunds")

    async with _master_with_worker(clean_database, tmp_path) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as client:
            await client.post(
                "/mcp/call", json=_payload("apps/testapp/inv.md", target_content)
            )
            await client.post(
                "/mcp/call", json=_payload("apps/testapp/bill.md", other_content)
            )

        await _wait_embeddings(clean_database, expected=2)

        async with httpx.AsyncClient(base_url=base_url) as client:
            r = await client.post(
                "/mcp/call",
                json={
                    "tool": "wiki.search",
                    "args": {
                        "query": target_content,
                        "mode": "semantic",
                        "limit": 5,
                    },
                },
            )
            assert r.status_code == 200, r.text
            entries = r.json()["entries"]
            assert entries, "semantic search returned nothing"
            assert entries[0]["path"] == "apps/testapp/inv.md"
            # Cosine similarity of identical unit-norm vectors is 1.
            assert entries[0]["score"] == pytest.approx(1.0, abs=1e-4)


@pytest.mark.asyncio
async def test_hybrid_search_combines_lexical_and_semantic(
    clean_database: str, tmp_path: Path
) -> None:
    """Hybrid mode finds a page by lexical match even when the Null
    embedder gives no semantic signal — RRF still ranks it via the
    lexical contribution."""
    matching = _page("Inventory", "warehouse stock across regions")
    other = _page("Billing", "invoices and refunds")

    async with _master_with_worker(clean_database, tmp_path) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as client:
            await client.post(
                "/mcp/call", json=_payload("apps/testapp/inv.md", matching)
            )
            await client.post(
                "/mcp/call", json=_payload("apps/testapp/bill.md", other)
            )

        await _wait_embeddings(clean_database, expected=2)

        async with httpx.AsyncClient(base_url=base_url) as client:
            r = await client.post(
                "/mcp/call",
                json={
                    "tool": "wiki.search",
                    "args": {
                        "query": "warehouse",
                        "mode": "hybrid",
                        "limit": 5,
                    },
                },
            )
            assert r.status_code == 200, r.text
            paths = [e["path"] for e in r.json()["entries"]]
            # The lexical hit should be in the result set even though
            # the Null embedder contributes only noise.
            assert "apps/testapp/inv.md" in paths


@pytest.mark.asyncio
async def test_semantic_mode_requires_embedder(
    clean_database: str, tmp_path: Path
) -> None:
    """Without VAULTMCP_EMBEDDING_PROVIDER, mode='semantic' returns 422."""
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

        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            r = await client.post(
                "/mcp/call",
                json={
                    "tool": "wiki.search",
                    "args": {"query": "anything", "mode": "semantic"},
                },
            )
            assert r.status_code == 422
            assert "embedding provider" in r.text.lower()
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
