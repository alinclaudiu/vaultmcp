"""End-to-end test for the official MCP-over-stdio adapter.

Spawns ``vaultmcp-master mcp-stdio`` as a subprocess and drives it
through the official ``mcp`` SDK client. Confirms that:

- The list_tools handshake returns our six wiki.* tools.
- A ``wiki.write`` followed by ``wiki.read`` round-trips through the
  same DB the rest of the master uses.
- A read of a missing page surfaces an error from the SDK side.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

pytestmark = pytest.mark.e2e


def _stdio_params(database_url: str, wiki_dir: Path) -> StdioServerParameters:
    env = os.environ.copy()
    env["VAULTMCP_DATABASE_URL"] = database_url
    env["VAULTMCP_WIKI_DIR"] = str(wiki_dir)
    # Resolve the CLI through the same Python interpreter pytest is
    # using so the test passes whether or not ``vaultmcp-master`` is
    # on $PATH (inside ``.venv/bin/`` typically isn't unless the venv
    # is activated).
    import shutil
    import sys as _sys

    cli = shutil.which(
        "vaultmcp-master", path=str(Path(_sys.executable).parent)
    ) or shutil.which("vaultmcp-master")
    if cli is None:  # pragma: no cover — install would be broken
        raise RuntimeError("vaultmcp-master CLI not found on PATH or venv bin")
    return StdioServerParameters(command=cli, args=["mcp-stdio"], env=env)


@pytest.mark.asyncio
async def test_mcp_stdio_handshake_and_round_trip(
    clean_database: str, tmp_path: Path
) -> None:
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    params = _stdio_params(clean_database, wiki_dir)

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            # ---- list_tools ----
            tools_resp = await session.list_tools()
            tool_names = {t.name for t in tools_resp.tools}
            for expected in (
                "wiki.read",
                "wiki.write",
                "wiki.list",
                "wiki.search",
                "wiki.audit",
                "wiki.append_log",
            ):
                assert expected in tool_names, f"missing {expected}"

            # ---- wiki.write ----
            page_content = (
                "---\n"
                "title: From MCP\n"
                "type: app\n"
                "owners: [test]\n"
                "updated: 2026-05-10\n"
                "---\n\n# hi\n"
            )
            write_resp = await session.call_tool(
                "wiki.write",
                {
                    "path": "apps/test/from-mcp.md",
                    "content": page_content,
                    "base_version": None,
                    "session": {
                        "server_id": "mcp-stdio",
                        "app": "test",
                        "agent_model": "pytest",
                        "session_id": "",
                    },
                },
            )
            assert write_resp.isError is False, write_resp
            payload = json.loads(write_resp.content[0].text)
            assert payload["version"] == 1
            assert payload["path"] == "apps/test/from-mcp.md"

            # And the file was rendered to disk on the master side.
            assert (wiki_dir / "apps/test/from-mcp.md").exists()

            # ---- wiki.read ----
            read_resp = await session.call_tool(
                "wiki.read", {"path": "apps/test/from-mcp.md"}
            )
            assert read_resp.isError is False
            payload = json.loads(read_resp.content[0].text)
            assert payload["content"] == page_content
            assert payload["version"] == 1

            # ---- wiki.read of missing page returns isError=True ----
            miss = await session.call_tool(
                "wiki.read", {"path": "apps/test/no-such.md"}
            )
            assert miss.isError is True
