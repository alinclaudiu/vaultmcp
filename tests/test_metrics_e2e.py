"""End-to-end test for the Prometheus /metrics endpoint."""

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
async def test_metrics_endpoint_renders_prometheus_format(
    clean_database: str, tmp_path: Path
) -> None:
    async with _master(clean_database, tmp_path) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as client:
            resp = await client.get("/metrics")
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/plain")
            body = resp.text

            # Empty-DB sanity: every series is emitted with zero.
            assert "vaultmcp_pages_total 0" in body
            assert "vaultmcp_embeddings_total 0" in body
            assert 'vaultmcp_embedding_jobs{state="pending"} 0' in body
            assert 'vaultmcp_embedding_jobs{state="failed"} 0' in body
            assert 'vaultmcp_audit_total{outcome="ok"} 0' in body
            assert 'vaultmcp_audit_total{outcome="conflict"} 0' in body
            assert "vaultmcp_servers_registered 0" in body
            assert "vaultmcp_extensions_registered 0" in body
            assert "vaultmcp_build_info" in body
            # Prometheus text-format requires HELP + TYPE for every series.
            for series in (
                "vaultmcp_pages_total",
                "vaultmcp_embeddings_total",
                "vaultmcp_embedding_jobs",
                "vaultmcp_audit_total",
            ):
                assert f"# HELP {series}" in body
                assert f"# TYPE {series}" in body

            # After a write, the gauges move.
            page = (
                "---\n"
                "title: M\n"
                "type: app\n"
                "owners: [t]\n"
                "updated: 2026-05-10\n"
                "---\n\n# x\n"
            )
            r = await client.post(
                "/mcp/call",
                json={
                    "tool": "wiki.write",
                    "args": {
                        "path": "apps/t/m.md",
                        "content": page,
                        "base_version": None,
                        "session": {
                            "server_id": "t",
                            "app": "t",
                            "agent_model": "pytest",
                            "session_id": "",
                        },
                    },
                },
            )
            assert r.status_code == 200

            resp = await client.get("/metrics")
            assert "vaultmcp_pages_total 1" in resp.text
            assert 'vaultmcp_audit_total{outcome="ok"} 1' in resp.text
