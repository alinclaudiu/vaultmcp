"""Master server bootstrap.

Combines the MCP tool surface (synchronous read/write/list/append_log) with
a FastAPI sidecar that hosts:

- ``/healthz`` — liveness probe
- ``/mcp/subscribe`` — SSE endpoint for real-time event stream
- ``/mcp/call`` — JSON-over-HTTP dispatch for the MCP tools (used by tests
  and by clients that don't speak the full MCP protocol yet)

For v0.2 we expose the tools via a simple HTTP/JSON endpoint at ``/mcp/call``.
This keeps the implementation testable and independent of the still-evolving
MCP Python SDK transport details. Adding the official MCP server protocol
(stdio + streamable-HTTP) is a small adapter on top of these handlers and
is tracked in a follow-up issue.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from .config import MasterConfig
from .db import Database
from .sse import EventBroadcaster, format_sse
from .tools import ConflictError, ToolError, call_handler_by_name

LOG = logging.getLogger("vaultmcp.master")


# =============================================================
# Request models (module-scope so FastAPI/Pydantic resolve them cleanly)
# =============================================================


class CallRequest(BaseModel):
    """Body of ``POST /mcp/call`` — the JSON-over-HTTP tool dispatch."""

    model_config = ConfigDict(extra="forbid")

    tool: str
    args: dict[str, Any] = {}


# =============================================================
# App factory
# =============================================================


def build_app(config: MasterConfig) -> FastAPI:
    """Build the FastAPI application bound to a master config."""

    state: dict[str, object] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        LOG.info("Connecting to %s", _redact(config.database_url))
        db = await Database.connect(config.database_url)
        await db.apply_schema(config.schema_sql_path)
        LOG.info("Schema applied; ensuring wiki dir %s exists", config.wiki_dir)
        config.wiki_dir.mkdir(parents=True, exist_ok=True)

        broadcaster = EventBroadcaster(db)
        await broadcaster.start()

        state["db"] = db
        state["broadcaster"] = broadcaster
        state["wiki_dir"] = config.wiki_dir

        try:
            yield
        finally:
            await broadcaster.stop()
            await db.close()

    app = FastAPI(
        title="VaultMCP master",
        version="0.2.0.dev0",
        lifespan=lifespan,
    )

    # ---------- /healthz ----------

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # ---------- /mcp/call ----------

    @app.post("/mcp/call")
    async def call_tool(req: CallRequest = Body(...)) -> dict[str, Any]:
        db = _get_db(state)
        wiki_dir = _get_wiki_dir(state)
        try:
            return await call_handler_by_name(
                req.tool, req.args, db=db, wiki_dir=wiki_dir
            )
        except ConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail=exc.conflict.model_dump(mode="json"),
            ) from exc
        except ToolError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc

    # ---------- /mcp/subscribe (SSE) ----------

    @app.get("/mcp/subscribe")
    async def subscribe(
        since_global_version: int | None = None,
        prefix: str | None = None,
    ) -> StreamingResponse:
        broadcaster = _get_broadcaster(state)

        async def event_stream() -> AsyncIterator[bytes]:
            async for event in broadcaster.subscribe(
                since_global_version=since_global_version,
                prefix=prefix,
            ):
                yield format_sse(event).encode("utf-8")

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return app


# =============================================================
# Helpers
# =============================================================


def _get_db(state: dict[str, object]) -> Database:
    db = state.get("db")
    if not isinstance(db, Database):
        raise RuntimeError("DB not initialized")
    return db


def _get_broadcaster(state: dict[str, object]) -> EventBroadcaster:
    b = state.get("broadcaster")
    if not isinstance(b, EventBroadcaster):
        raise RuntimeError("Broadcaster not initialized")
    return b


def _get_wiki_dir(state: dict[str, object]) -> Path:
    wd = state.get("wiki_dir")
    if not isinstance(wd, Path):
        raise RuntimeError("wiki_dir not initialized")
    return wd


def _redact(dsn: str) -> str:
    """Hide password in DSN for safe logging."""
    if "://" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    if "@" not in rest:
        return dsn
    auth, host = rest.split("@", 1)
    if ":" in auth:
        user = auth.split(":", 1)[0]
        return f"{scheme}://{user}:***@{host}"
    return dsn
