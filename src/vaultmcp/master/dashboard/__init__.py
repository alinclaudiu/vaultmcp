"""Dashboard module — server-rendered HTML over the master's Postgres.

Tech stack from `docs/04-dashboard.md`:

- FastAPI router mounted on the master process
- Jinja2 templates in ``./templates/``
- PicoCSS + HTMX loaded from CDN (no JS build pipeline)

v0.4 scope is observability-only (read-only views: activity, servers,
audit, search). Editing wiki pages stays in the agent's flow.
"""

from .router import build_router

__all__ = ["build_router"]
