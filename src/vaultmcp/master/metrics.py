"""Prometheus text-format ``/metrics`` endpoint.

Exposes a small, well-defined set of gauges and counters that operators
can scrape without bringing in ``prometheus_client`` as a dependency
(the text format is trivial to render by hand).

What's exposed:

- ``vaultmcp_pages_total`` — current row count of ``pages``.
- ``vaultmcp_embeddings_total`` — rows in ``embeddings``.
- ``vaultmcp_embedding_jobs{state}`` — pending / failed counts.
- ``vaultmcp_audit_total{outcome}`` — running totals broken down by
  outcome (ok / conflict / forbidden / validation_failed).
- ``vaultmcp_servers_registered`` / ``vaultmcp_extensions_registered``.
- ``vaultmcp_build_info{version=...}`` — single-sample gauge with
  the package version as a label, value 1 (Prometheus convention).

Counters (audit) are implemented as gauges over the audit table
because we read the actual rows; if the table is truncated, the
metric drops accordingly. Operators expecting strict monotonicity
should layer a recording rule on top.
"""

from __future__ import annotations

from .. import __version__ as _pkg_version
from .db import Database

_HEADER = (
    "# HELP vaultmcp_pages_total Number of pages in the wiki.\n"
    "# TYPE vaultmcp_pages_total gauge\n"
    "# HELP vaultmcp_embeddings_total Pages with a current embedding.\n"
    "# TYPE vaultmcp_embeddings_total gauge\n"
    "# HELP vaultmcp_embedding_jobs Embedding worker queue depth.\n"
    "# TYPE vaultmcp_embedding_jobs gauge\n"
    "# HELP vaultmcp_audit_total Audit rows by outcome.\n"
    "# TYPE vaultmcp_audit_total gauge\n"
    "# HELP vaultmcp_servers_registered Bearer-token authenticated servers.\n"
    "# TYPE vaultmcp_servers_registered gauge\n"
    "# HELP vaultmcp_extensions_registered Registered ext.* extensions.\n"
    "# TYPE vaultmcp_extensions_registered gauge\n"
    "# HELP vaultmcp_build_info Package build information.\n"
    "# TYPE vaultmcp_build_info gauge\n"
)


async def render_metrics(db: Database) -> str:
    """Pull every counter from Postgres and return a Prometheus text doc."""
    async with db.pool.acquire() as conn:
        pages = await conn.fetchval("SELECT count(*) FROM pages")
        embeddings = await conn.fetchval("SELECT count(*) FROM embeddings")
        ej_pending = await conn.fetchval(
            "SELECT count(*) FROM embedding_jobs WHERE attempts < 5"
        )
        ej_failed = await conn.fetchval(
            "SELECT count(*) FROM embedding_jobs WHERE attempts >= 5"
        )
        audit_rows = await conn.fetch(
            "SELECT outcome, count(*) AS n FROM audit GROUP BY outcome"
        )
        servers = await conn.fetchval("SELECT count(*) FROM servers")
        # ``extensions`` table is created lazily on first migrate; query
        # defensively so a fresh DB doesn't 500 the metrics endpoint.
        extensions = await conn.fetchval(
            "SELECT count(*) FROM extensions"
        ) if await conn.fetchval(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name='extensions'"
        ) else 0

    lines: list[str] = [_HEADER]
    lines.append(f"vaultmcp_pages_total {int(pages or 0)}")
    lines.append(f"vaultmcp_embeddings_total {int(embeddings or 0)}")
    lines.append(f'vaultmcp_embedding_jobs{{state="pending"}} {int(ej_pending or 0)}')
    lines.append(f'vaultmcp_embedding_jobs{{state="failed"}} {int(ej_failed or 0)}')
    # Always emit a row for every known outcome so dashboards don't
    # silently drop a series when the count happens to be zero.
    seen: dict[str, int] = {r["outcome"]: int(r["n"]) for r in audit_rows}
    for outcome in ("ok", "conflict", "forbidden", "validation_failed"):
        lines.append(
            f'vaultmcp_audit_total{{outcome="{outcome}"}} {seen.get(outcome, 0)}'
        )
    lines.append(f"vaultmcp_servers_registered {int(servers or 0)}")
    lines.append(f"vaultmcp_extensions_registered {int(extensions or 0)}")
    lines.append(f'vaultmcp_build_info{{version="{_pkg_version}"}} 1')
    lines.append("")  # trailing newline
    return "\n".join(lines)
