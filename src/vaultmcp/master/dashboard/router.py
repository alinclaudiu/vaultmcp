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

    return router
