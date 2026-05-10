"""FastAPI router for the dashboard.

Mounted at the application root. Renders Jinja templates against the
master's Postgres state. Read-only by design.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ... import __version__ as _pkg_version
from ..config import MasterConfig
from ..db import Database

TEMPLATES_DIR = Path(__file__).parent / "templates"


# A small adapter so templates can use attribute access on event records.
class _Event:
    __slots__ = ("event_type", "global_version", "id", "path", "ts")

    def __init__(
        self, *, id: int, ts, event_type: str, path: str | None, global_version: int
    ) -> None:
        self.id = id
        self.ts = ts
        self.event_type = event_type
        self.path = path
        self.global_version = global_version


def build_router(
    *,
    config: MasterConfig,
    get_db: Callable[[], Awaitable[Database] | Database],
) -> APIRouter:
    """Construct the dashboard router.

    ``get_db`` returns the live :class:`Database` instance for each
    request — the master holds the pool in its app state and exposes it
    through this closure so the dashboard reads from the same pool the
    rest of the app does.
    """
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    router = APIRouter()

    base_ctx = {
        "version": _pkg_version,
        "master_url": f"http://{config.http_host}:{config.http_port}",
    }

    async def _resolve_db() -> Database:
        result = get_db()
        if hasattr(result, "__await__"):
            return await result  # type: ignore[return-value, no-any-return]
        return result  # type: ignore[return-value]

    async def _recent_events() -> list[_Event]:
        db = await _resolve_db()
        rows = await db.recent_events(limit=50)
        return [
            _Event(
                id=r["id"],
                ts=r["ts"],
                event_type=r["event_type"],
                path=r["path"],
                global_version=r["global_version"],
            )
            for r in rows
        ]

    @router.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def activity(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "activity.html",
            {**base_ctx, "active": "activity", "events": await _recent_events()},
        )

    @router.get(
        "/dashboard/activity-rows",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def activity_rows(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "_activity_rows.html", {"events": await _recent_events()}
        )

    # ---------- /servers ----------

    @router.get("/servers", response_class=HTMLResponse, include_in_schema=False)
    async def servers(request: Request) -> HTMLResponse:
        db = await _resolve_db()
        rows = await db.servers_with_activity()
        servers_view = [
            type(
                "_S",
                (),
                {"id": sid, "apps": apps, "rotated_at": rot, "last_active": last},
            )()
            for sid, apps, rot, last in rows
        ]
        return templates.TemplateResponse(
            request,
            "servers.html",
            {**base_ctx, "active": "servers", "servers": servers_view},
        )

    # ---------- /audit ----------

    @router.get("/audit", response_class=HTMLResponse, include_in_schema=False)
    async def audit(
        request: Request,
        path: str | None = None,
        server_id: str | None = None,
        app: str | None = None,
        outcome: str | None = None,
    ) -> HTMLResponse:
        db = await _resolve_db()

        # Outcome doesn't have a dedicated DB filter — we filter in Python.
        rows = await db.list_audit(
            path=path or None,
            server_id=server_id or None,
            app=app or None,
            limit=500,
        )
        if outcome:
            rows = [r for r in rows if r["outcome"] == outcome]
        rows = rows[:200]

        entries = [
            type(
                "_A",
                (),
                {
                    "id": r["id"],
                    "ts": r["ts"],
                    "operation": r["operation"],
                    "path": r["path"],
                    "server_id": r["server_id"],
                    "app": r["app"],
                    "outcome": r["outcome"],
                    "version_before": r["version_before"],
                    "version_after": r["version_after"],
                },
            )()
            for r in rows
        ]
        return templates.TemplateResponse(
            request,
            "audit.html",
            {
                **base_ctx,
                "active": "audit",
                "entries": entries,
                "filters": {
                    "path": path,
                    "server_id": server_id,
                    "app": app,
                    "outcome": outcome,
                },
            },
        )

    return router
