"""Official MCP transport adapter (stdio) over the master's tool layer.

Until this lands, agents talk to master through ``POST /mcp/call`` JSON-
over-HTTP. That works but isn't the standard MCP protocol an
off-the-shelf agent (Claude Desktop, an MCP-aware IDE plugin, etc.)
expects to dial.

This module wraps the master's existing pure-Python tool handlers in
the official ``mcp`` Python SDK's stdio transport, so the same handler
code answers either form of caller. The wire format is whatever the
SDK speaks; the business logic is unchanged.

Run via the CLI:

    vaultmcp-master mcp-stdio

stdin/stdout are wired to the MCP transport — typically an LLM client
spawns the command as a subprocess and pipes the JSON-RPC traffic
through the pipes.

Limitations on this first pass:
- Only ``wiki.*`` tools are exposed (read/write/list/append_log/search/
  audit). ``ext.*`` are admin-only and gated server-side already.
- No auth: stdio is a local pipe; the parent process is the trust
  boundary. Network MCP (streamable-HTTP) is a follow-up.
- Tool input is validated against the Pydantic model's JSON schema
  by the SDK; our handlers run their own validation downstream too.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp import types as mcp_types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .. import __version__
from ..shared.types import (
    AuditInput,
    ListInput,
    LogEntryInput,
    ReadInput,
    SearchInput,
    WriteInput,
)
from .config import MasterConfig
from .db import Database
from .errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ToolError,
    ValidationFailed,
)
from .tools import call_handler_by_name

# ---------------------------------------------------------------
# Tool catalog
# ---------------------------------------------------------------
#
# Order: read-side first (cheap, frequently-invoked by LLMs), then
# write-side, then audit/search. Each entry's inputSchema is the
# Pydantic JSON schema for the corresponding *Input model — the SDK
# uses it to validate before our handler runs.

_TOOL_SPECS: tuple[tuple[str, str, type], ...] = (
    (
        "wiki.read",
        "Read a wiki page by its dotted/slashed path. Returns content + version.",
        ReadInput,
    ),
    (
        "wiki.list",
        "List wiki pages, optionally filtered by prefix / type / updated_since.",
        ListInput,
    ),
    (
        "wiki.search",
        "Search the wiki. mode='lexical' (default) | 'semantic' | 'hybrid'.",
        SearchInput,
    ),
    (
        "wiki.audit",
        "Read the audit log; filter by path / server / app / outcome.",
        AuditInput,
    ),
    (
        "wiki.write",
        "Create or update a wiki page. Optimistic concurrency via base_version.",
        WriteInput,
    ),
    (
        "wiki.append_log",
        "Append a row to the session log (the message-bus role).",
        LogEntryInput,
    ),
)


def _tool_list() -> list[mcp_types.Tool]:
    """Build the ``Tool`` objects the MCP SDK exposes via list_tools."""
    out: list[mcp_types.Tool] = []
    for name, description, model in _TOOL_SPECS:
        schema = model.model_json_schema()
        out.append(
            mcp_types.Tool(
                name=name, description=description, inputSchema=schema
            )
        )
    return out


# ---------------------------------------------------------------
# Server construction
# ---------------------------------------------------------------


def build_mcp_server(*, db: Database, wiki_dir: Path) -> Server:
    """Construct an MCP ``Server`` whose tools dispatch to our handlers.

    ``db`` and ``wiki_dir`` are captured by closure so the per-request
    handler stays free of constructor parameters — the SDK doesn't
    pass arbitrary state through call_tool().
    """
    server: Server = Server(
        "vaultmcp",
        version=__version__,
        instructions=(
            "VaultMCP master. Read/write the team wiki, search by "
            "lexical or semantic similarity, and append session-log "
            "entries. Writes use optimistic concurrency: pass the "
            "version you most recently read as base_version, otherwise "
            "the server returns a conflict with the current state."
        ),
    )

    @server.list_tools()
    async def _list_tools() -> list[mcp_types.Tool]:
        return _tool_list()

    @server.call_tool()
    async def _call_tool(
        name: str, arguments: dict[str, Any] | None
    ) -> Sequence[mcp_types.ContentBlock]:
        try:
            result = await call_handler_by_name(
                name,
                arguments or {},
                db=db,
                wiki_dir=wiki_dir,
                # No auth on stdio: the calling process is the trust
                # boundary. ownership / max_bytes use their defaults.
            )
        except ConflictError as exc:
            raise ToolError(
                "conflict: " + json.dumps(exc.conflict.model_dump(mode="json"))
            ) from exc
        except (
            ForbiddenError,
            NotFoundError,
            ValidationFailed,
            ToolError,
        ):
            # The SDK wraps the exception in a CallToolResult with
            # isError=True; the message goes back to the LLM client
            # as an error block.
            raise

        # `result` is a JSON-serialisable dict. Hand it back as
        # a single text block; the SDK will also include it as
        # ``structuredContent`` because we pass a dict-shaped value.
        return [
            mcp_types.TextContent(type="text", text=json.dumps(result, default=str))
        ]

    return server


# ---------------------------------------------------------------
# CLI entry — used by `vaultmcp-master mcp-stdio`.
# ---------------------------------------------------------------


@asynccontextmanager
async def _master_db(config: MasterConfig):
    """Bring up just the DB + schema. No HTTP, no embedding worker, no
    SSE broadcaster — stdio MCP doesn't need any of that."""
    db = await Database.connect(config.database_url)
    try:
        await db.apply_schema(config.schema_sql_path)
        config.wiki_dir.mkdir(parents=True, exist_ok=True)
        yield db
    finally:
        await db.close()


async def serve_stdio() -> None:
    """Boot the DB, build the MCP server, run it on stdin/stdout."""
    config = MasterConfig.from_env()
    async with _master_db(config) as db:
        server = build_mcp_server(db=db, wiki_dir=config.wiki_dir)
        init_options = server.create_initialization_options()
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, init_options)
