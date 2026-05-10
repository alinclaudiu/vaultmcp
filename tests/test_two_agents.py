"""Multi-agent end-to-end test exercising the v0.2 sync flow.

Boots the master HTTP server in-process plus two agents (each with its
own vault dir + state dir), writes a markdown file under agent A's vault,
and asserts it propagates to agent B within a few seconds without
triggering an echo loop.

The full path covered:

- watchdog inotify on agent A
- wiki.write -> master via /mcp/call
- INSERT trigger on `pages` -> NOTIFY events_channel
- master EventBroadcaster fanout to SSE subscribers
- agent B's SSE subscribe -> wiki.read -> local mirror write
- SyncSuppressor preventing watcher echoes on both agents (regression
  guard for commit 918f334)
"""

from __future__ import annotations

import asyncio
import socket
import time
from pathlib import Path

import pytest
import uvicorn

pytestmark = pytest.mark.e2e


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _wait_for(predicate, timeout: float, interval: float = 0.1, what: str = "condition") -> None:
    """Poll ``predicate`` (sync or async) until it's truthy or time runs out."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"Timed out waiting for {what} after {timeout:.1f}s")


@pytest.mark.asyncio
async def test_two_agents_sync(clean_database: str, tmp_path: Path) -> None:
    from vaultmcp.agent.client import MasterClient
    from vaultmcp.agent.config import AgentConfig
    from vaultmcp.agent.queue import WriteQueue
    from vaultmcp.agent.suppressor import SyncSuppressor
    from vaultmcp.agent.sync import SyncEngine
    from vaultmcp.agent.version_cache import VersionCache
    from vaultmcp.agent.watcher import FileWatcher
    from vaultmcp.master.config import MasterConfig
    from vaultmcp.master.server import build_app

    # ---------------- master ----------------
    port = _free_port()
    master_url = f"http://127.0.0.1:{port}"
    master_wiki = tmp_path / "master_wiki"
    master_wiki.mkdir()

    master_config = MasterConfig(database_url=clean_database, wiki_dir=master_wiki)
    app = build_app(master_config)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            lifespan="on",
        )
    )
    server_task = asyncio.create_task(server.serve())
    await _wait_for(lambda: getattr(server, "started", False), timeout=10, what="master startup")

    # ---------------- agents ----------------
    def _spawn(name: str) -> tuple[AgentConfig, FileWatcher, SyncEngine, MasterClient]:
        vault = tmp_path / f"vault_{name}"
        state = tmp_path / f"state_{name}"
        vault.mkdir()
        state.mkdir()
        cfg = AgentConfig(
            master_url=master_url,
            vault_dir=vault,
            state_dir=state,
            server_id=f"server-{name}",
            app="testapp",
        )
        cache = VersionCache(state / "version-cache.db")
        queue = WriteQueue(state / "queue")
        client = MasterClient(master_url)
        suppressor = SyncSuppressor()
        watcher = FileWatcher(cfg, client, cache, queue, suppressor=suppressor)
        sync = SyncEngine(client=client, cache=cache, vault_dir=vault, suppressor=suppressor)
        return cfg, watcher, sync, client

    cfg_a, watcher_a, sync_a, client_a = _spawn("a")
    cfg_b, watcher_b, sync_b, client_b = _spawn("b")

    a_task = asyncio.gather(watcher_a.run(), sync_a.run())
    b_task = asyncio.gather(watcher_b.run(), sync_b.run())

    try:
        # Give both agents' SSE subscribers time to attach before we
        # generate the event we want them to receive.
        await asyncio.sleep(0.5)

        rel_path = "apps/testapp/notes.md"
        content = (
            "---\n"
            "title: Notes\n"
            "type: app\n"
            "owners: [testapp]\n"
            "updated: 2026-05-10\n"
            "---\n\n"
            "# Hello from A.\n"
        )
        target_a = cfg_a.vault_dir / rel_path
        target_b = cfg_b.vault_dir / rel_path
        target_a.parent.mkdir(parents=True, exist_ok=True)
        target_a.write_text(content, encoding="utf-8")

        await _wait_for(
            lambda: target_b.exists() and target_b.read_text(encoding="utf-8") == content,
            timeout=10,
            what=f"file to propagate from agent A to agent B ({target_b})",
        )

        # Regression guard for the SyncSuppressor fix (commit 918f334):
        # if either agent's watcher echoed master's own broadcast back
        # as a fresh write, master would be at v2+ instead of v1.
        page = await client_a.read(rel_path)
        assert page["version"] == 1, (
            f"Echo loop suspected: master is at v{page['version']} after one write"
        )
        # And on-disk content on agent A is unchanged from what we wrote.
        assert target_a.read_text(encoding="utf-8") == content

    finally:
        await watcher_a.stop()
        await watcher_b.stop()
        await sync_a.stop()
        await sync_b.stop()
        for t in (a_task, b_task):
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        await client_a.close()
        await client_b.close()

        server.should_exit = True
        try:
            await asyncio.wait_for(server_task, timeout=5)
        except TimeoutError:
            server_task.cancel()
            try:
                await server_task
            except (asyncio.CancelledError, Exception):
                pass
