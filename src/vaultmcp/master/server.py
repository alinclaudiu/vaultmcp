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

from fastapi import Body, Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from .auth import AuthenticatedServer, hash_token, parse_bearer
from .config import MasterConfig
from .db import Database
from .ownership import OwnershipRules, load_rules
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

        ownership = load_rules(config.ownership_config_path)
        if len(ownership) > 0:
            LOG.info("Loaded %d ownership rule(s)", len(ownership))

        state["db"] = db
        state["broadcaster"] = broadcaster
        state["wiki_dir"] = config.wiki_dir
        state["max_bytes"] = config.max_file_size_bytes
        state["ownership"] = ownership

        try:
            yield
        finally:
            await broadcaster.stop()
            await db.close()

    app = FastAPI(
        title="VaultMCP master",
        version="0.2.1",
        lifespan=lifespan,
    )

    # ---------- auth dependency ----------

    async def authenticate(
        authorization: str | None = Header(default=None),
    ) -> AuthenticatedServer | None:
        """Validate a bearer token against the ``servers`` table.

        Returns ``None`` when no servers are registered yet (dev mode);
        callers that need an identity fall back to other constraints.
        Raises 401 when servers exist and the token is missing or wrong.
        """
        db = _get_db(state)
        if await db.count_servers() == 0:
            return None  # dev mode: no servers configured, no auth enforced
        token = parse_bearer(authorization)
        if token is None:
            raise HTTPException(
                status_code=401,
                detail="Bearer token required",
                headers={"WWW-Authenticate": 'Bearer realm="vaultmcp"'},
            )
        match = await db.find_server_by_token_hash(hash_token(token))
        if match is None:
            raise HTTPException(status_code=401, detail="Invalid token")
        server_id, apps = match
        return AuthenticatedServer(id=server_id, apps=tuple(apps))

    # ---------- /healthz ----------

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # ---------- /mcp/call ----------

    @app.post("/mcp/call")
    async def call_tool(
        req: CallRequest = Body(...),
        authed: AuthenticatedServer | None = Depends(authenticate),
    ) -> dict[str, Any]:
        db = _get_db(state)
        wiki_dir = _get_wiki_dir(state)
        max_bytes = _get_max_bytes(state)
        ownership = _get_ownership(state)
        try:
            return await call_handler_by_name(
                req.tool,
                req.args,
                db=db,
                wiki_dir=wiki_dir,
                authed=authed,
                max_bytes=max_bytes,
                ownership=ownership,
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
        _authed: AuthenticatedServer | None = Depends(authenticate),
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


def _get_max_bytes(state: dict[str, object]) -> int:
    n = state.get("max_bytes")
    if not isinstance(n, int):
        raise RuntimeError("max_bytes not initialized")
    return n


def _get_ownership(state: dict[str, object]) -> OwnershipRules:
    o = state.get("ownership")
    if not isinstance(o, OwnershipRules):
        raise RuntimeError("ownership not initialized")
    return o


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
