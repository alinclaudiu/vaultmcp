"""Master server bootstrap.

Combines the MCP tool surface (synchronous read/write/list/append_log) with
a FastAPI sidecar that hosts:

- ``/healthz`` — liveness probe
- ``/metrics`` — Prometheus text-format gauges
- ``/mcp/subscribe`` — SSE endpoint for real-time event stream
- ``/mcp/call`` — JSON-over-HTTP dispatch for the MCP tools (the v0.2-era
  shim; now lives alongside ``/mcp/streamable``)
- ``/mcp/streamable`` — official MCP-over-HTTP transport via the
  ``mcp`` Python SDK; what off-the-shelf MCP-aware clients should dial
- ``/`` and friends — the dashboard router

Stdio MCP transport (the SDK's other supported wire format) lives in
``vaultmcp.master.mcp_adapter``; bring it up via the
``vaultmcp-master mcp-stdio`` CLI.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, Header, HTTPException
from fastapi.responses import Response, StreamingResponse
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import BaseModel, ConfigDict, ValidationError

from .auth import AuthenticatedServer, hash_token, parse_bearer
from .config import MasterConfig
from .dashboard import build_router as build_dashboard_router
from .db import Database
from .embeddings import EmbeddingWorker, build_provider
from .mcp_adapter import build_mcp_server
from .metrics import render_metrics
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

        embedding_worker: EmbeddingWorker | None = None
        embedding_provider = None
        if config.embedding_provider:
            try:
                embedding_provider = build_provider(
                    name=config.embedding_provider,
                    base_url=config.embedding_base_url,
                    model=config.embedding_model,
                    api_key=config.embedding_api_key,
                    dim=config.embedding_dim,
                )
            except (KeyError, ValueError) as exc:
                LOG.error("Embedding provider misconfigured: %s; worker disabled", exc)
            else:
                embedding_worker = EmbeddingWorker(db=db, provider=embedding_provider)
                await embedding_worker.start()
                LOG.info(
                    "Embedding worker started (provider=%s)", embedding_provider.name
                )

        state["db"] = db
        state["broadcaster"] = broadcaster
        state["wiki_dir"] = config.wiki_dir
        state["max_bytes"] = config.max_file_size_bytes
        state["ownership"] = ownership
        state["embedder"] = embedding_provider

        # Network MCP transport (streamable-HTTP). Mounted at
        # /mcp/streamable; the JSON-over-HTTP /mcp/call shim stays in
        # place for callers from before the SDK adapter landed.
        # Stateless=True keeps each POST self-contained — no session
        # affinity to manage. The session manager owns its own task
        # group via the run() context, so we keep it open for the
        # duration of the FastAPI lifespan.
        mcp_server = build_mcp_server(db=db, wiki_dir=config.wiki_dir)
        mcp_session_manager = StreamableHTTPSessionManager(
            app=mcp_server, stateless=True, json_response=True
        )
        state["mcp_session_manager"] = mcp_session_manager

        try:
            # ``mcp_session_manager.run()`` owns its own task group and
            # MUST stay open for the duration of /mcp/streamable. Nest
            # it inside our existing teardown so both shut down cleanly
            # on lifespan exit.
            async with mcp_session_manager.run():
                yield
        finally:
            if embedding_worker is not None:
                await embedding_worker.stop()
            if embedding_provider is not None:
                aclose = getattr(embedding_provider, "aclose", None)
                if aclose is not None:
                    try:
                        await aclose()
                    except Exception:
                        pass
            await broadcaster.stop()
            await db.close()

    app = FastAPI(
        title="VaultMCP master",
        version="0.6.1",
        lifespan=lifespan,
    )

    # Dashboard router (read-only HTML views over Postgres state).
    # Mounted before the JSON routes so /healthz etc. still take
    # precedence by being declared later on the same app.
    def _embedder_name() -> str | None:
        emb = state.get("embedder")
        return getattr(emb, "name", None) if emb is not None else None

    app.include_router(
        build_dashboard_router(
            config=config,
            get_db=lambda: _get_db(state),
            get_broadcaster=lambda: _get_broadcaster(state),
            get_embedder_name=_embedder_name,
        )
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

    # ---------- /metrics ----------

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        """Prometheus text-format metrics. No auth — gate at the proxy
        layer or via firewall rules if you expose this beyond the
        master's localhost bind."""
        body = await render_metrics(_get_db(state))
        return Response(content=body, media_type="text/plain; version=0.0.4")

    # ---------- /mcp/streamable (network MCP transport) ----------
    #
    # The official MCP-over-HTTP endpoint. Lives alongside (not in
    # place of) /mcp/call so old JSON-over-HTTP callers keep working
    # while new MCP-aware clients dial /mcp/streamable. Mounted as a
    # raw ASGI route because the SDK's session manager already speaks
    # ASGI (POST + SSE on the same path, per MCP spec).
    async def _mcp_streamable_asgi(scope: dict, receive: Any, send: Any) -> None:
        sm = state.get("mcp_session_manager")
        if not isinstance(sm, StreamableHTTPSessionManager):
            # Lifespan hasn't completed yet — shouldn't happen because
            # uvicorn waits for startup, but bail safely if it does.
            await Response(
                "MCP transport not initialised", status_code=503
            )(scope, receive, send)
            return
        await sm.handle_request(scope, receive, send)

    from starlette.routing import Mount as _Mount

    app.routes.append(_Mount("/mcp/streamable", app=_mcp_streamable_asgi))

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
        embedder = state.get("embedder")  # may be None
        try:
            return await call_handler_by_name(
                req.tool,
                req.args,
                db=db,
                wiki_dir=wiki_dir,
                authed=authed,
                max_bytes=max_bytes,
                ownership=ownership,
                embedder=embedder,  # type: ignore[arg-type]
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
