"""Pure tool handler functions.

Each function maps to an MCP tool exposed by the master. They take a
:class:`Database` and Pydantic input model, return a Pydantic output (or
raise :class:`ToolError` for protocol-level errors). This separation
means the handlers are testable without an MCP server running — the
server module wires them into the actual MCP transport.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..shared import frontmatter as fm_mod
from ..shared.types import (
    AuditEntry,
    AuditInput,
    AuditOutput,
    ListEntry,
    ListInput,
    ListOutput,
    LogEntryInput,
    LogEntryOutput,
    PageMetadata,
    ReadInput,
    ReadOutput,
    SearchInput,
    SearchOutput,
    SearchResult,
    WriteConflict,
    WriteInput,
    WriteOutput,
)
from .auth import AuthenticatedServer
from .db import ConflictResult, Database, WriteResult
from .embeddings import EmbeddingProvider
from .errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ToolError,
    ValidationFailed,
)
from .ownership import OwnershipRules
from .render import render_to_disk
from .validation import DEFAULT_MAX_FILE_SIZE_BYTES, validate_write_content

__all__ = [
    "ConflictError",
    "ForbiddenError",
    "NotFoundError",
    "ToolError",
    "ValidationFailed",
    "call_handler_by_name",
    "handle_append_log",
    "handle_audit",
    "handle_list",
    "handle_read",
    "handle_search",
    "handle_write",
]


def _enforce_session_matches_server(
    session_server_id: str,
    session_app: str,
    authed: AuthenticatedServer | None,
) -> None:
    """Reject calls where the session claims an identity the token doesn't grant.

    When ``authed`` is None the master runs in unauthenticated dev mode
    (no servers registered yet) and the check is skipped.
    """
    if authed is None:
        return
    if session_server_id != authed.id:
        raise ForbiddenError(
            f"Session claims server_id={session_server_id!r} but token belongs to {authed.id!r}"
        )
    if session_app not in authed.apps:
        raise ForbiddenError(
            f"Server {authed.id!r} is not registered for app {session_app!r}"
        )


# =============================================================
# Handlers
# =============================================================


async def handle_read(db: Database, inp: ReadInput) -> ReadOutput:
    page = await db.get_page(inp.path)
    if page is None:
        raise NotFoundError(f"Page not found: {inp.path}")

    return ReadOutput(
        path=page.path,
        content=page.content,
        version=page.version,
        global_version=page.global_version,
        metadata=PageMetadata(
            type=page.type,  # type: ignore[arg-type]
            owners=page.owners,
            updated=page.updated,
            last_writer_server=page.last_writer_server,
            last_writer_app=page.last_writer_app,
            last_writer_session=page.last_writer_session,
        ),
    )


async def handle_write(
    db: Database,
    wiki_dir: Path,
    inp: WriteInput,
    *,
    authed: AuthenticatedServer | None = None,
    max_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES,
    ownership: OwnershipRules | None = None,
) -> WriteOutput:
    _enforce_session_matches_server(inp.session.server_id, inp.session.app, authed)

    # Path-level ownership rules from /srv/vaultmcp/config.yaml. No-op
    # when the file is missing or no rule matches the path.
    if ownership is not None:
        ownership.check_write(path=inp.path, app=inp.session.app)

    # Encoding / size / frontmatter / secret checks. Raises ValidationFailed
    # (422) or SizeLimitExceeded (413).
    validate_write_content(inp.content, max_bytes=max_bytes)

    # Validation already parsed the frontmatter; do it once more here so
    # the rest of the function keeps the same shape it had pre-validation.
    metadata, _body = fm_mod.parse(inp.content)

    type_ = fm_mod.extract_type(metadata)
    owners = fm_mod.extract_owners(metadata)
    updated = fm_mod.extract_updated(metadata) or datetime.now(tz=timezone.utc)

    result = await db.write_page(
        path=inp.path,
        content=inp.content,
        base_version=inp.base_version,
        metadata=metadata,
        type_=type_,
        owners=owners,
        updated=updated,
        last_writer_server=inp.session.server_id,
        last_writer_app=inp.session.app,
        last_writer_session=inp.session.session_id or "",
        agent_model=inp.session.agent_model,
        prompt_hash=inp.session.prompt_hash,
    )

    if isinstance(result, ConflictResult):
        raise ConflictError(
            WriteConflict(
                current_version=result.current_version,
                current_content=result.current_content,
                last_writer=result.last_writer,
            )
        )

    assert isinstance(result, WriteResult)
    # Render to disk AFTER successful DB commit. If this fails, log and
    # surface a 500 — the DB is the source of truth, the on-disk file is
    # a projection that will be re-rendered on the next write or via
    # `vaultmcp-master render-all`.
    try:
        render_to_disk(wiki_dir, inp.path, inp.content)
    except (OSError, ValueError) as exc:
        # Non-fatal for correctness; surface it but the DB write has succeeded.
        raise ToolError(
            f"Wrote to DB but failed to render to disk: {exc}",
            status=500,
        ) from exc

    return WriteOutput(
        path=result.path,
        version=result.version,
        global_version=result.global_version,
        applied_at=result.applied_at,
    )


async def handle_list(db: Database, inp: ListInput) -> ListOutput:
    pages, next_cursor = await db.list_pages(
        prefix=inp.prefix,
        type_filter=inp.type,
        updated_since=inp.updated_since,
        limit=inp.limit,
        cursor=inp.cursor,
    )
    return ListOutput(
        entries=[
            ListEntry(
                path=p.path,
                type=p.type,  # type: ignore[arg-type]
                owners=p.owners,
                updated=p.updated,
                version=p.version,
                global_version=p.global_version,
            )
            for p in pages
        ],
        next_cursor=next_cursor,
    )


async def handle_search(
    db: Database,
    inp: SearchInput,
    *,
    embedder: EmbeddingProvider | None = None,
) -> SearchOutput:
    if inp.mode in ("semantic", "hybrid") and embedder is None:
        raise ValidationFailed(
            f"Search mode {inp.mode!r} requires an embedding provider; "
            "set VAULTMCP_EMBEDDING_PROVIDER on the master"
        )

    if inp.mode == "lexical":
        rows = await db.search_pages(
            query=inp.query,
            prefix=inp.prefix,
            type_filter=inp.type,
            limit=inp.limit,
        )
        entries = [_search_row_to_result(r) for r in rows]
        return SearchOutput(entries=entries)

    assert embedder is not None  # narrowed by the guard above
    query_vector = await embedder.embed(inp.query)

    if inp.mode == "semantic":
        rows = await db.search_pages_semantic(
            query_vector=query_vector,
            prefix=inp.prefix,
            type_filter=inp.type,
            limit=inp.limit,
        )
        entries = [_search_row_to_result(r) for r in rows]
        return SearchOutput(entries=entries)

    # ---- hybrid ----
    # Pull a deeper window from each ranker so RRF has room to mix.
    window = max(inp.limit * 4, 50)
    lex_rows, sem_rows = await asyncio.gather(
        db.search_pages(
            query=inp.query,
            prefix=inp.prefix,
            type_filter=inp.type,
            limit=window,
        ),
        db.search_pages_semantic(
            query_vector=query_vector,
            prefix=inp.prefix,
            type_filter=inp.type,
            limit=window,
        ),
    )
    fused = _reciprocal_rank_fusion(lex_rows, sem_rows, k=60, limit=inp.limit)
    return SearchOutput(entries=fused)


def _search_row_to_result(r: dict[str, Any]) -> SearchResult:
    return SearchResult(
        path=r["path"],
        type=r["type"],
        owners=list(r["owners"] or []),
        updated=r["updated"],
        version=r["version"],
        score=float(r["score"] or 0.0),
        snippet=r["snippet"] or "",
    )


def _reciprocal_rank_fusion(
    lex_rows: list[dict[str, Any]],
    sem_rows: list[dict[str, Any]],
    *,
    k: int,
    limit: int,
) -> list[SearchResult]:
    """Combine two ranked lists by RRF: score = Σ 1 / (k + rank_i).

    Snippets and metadata are taken from whichever list saw the doc
    first (lexical preferred, since it has ts_headline output).
    """
    fused: dict[str, dict[str, Any]] = {}
    for rank, r in enumerate(lex_rows, start=1):
        fused[r["path"]] = {"row": r, "score": 1.0 / (k + rank)}
    for rank, r in enumerate(sem_rows, start=1):
        existing = fused.get(r["path"])
        if existing is None:
            fused[r["path"]] = {"row": r, "score": 1.0 / (k + rank)}
        else:
            existing["score"] += 1.0 / (k + rank)

    ordered = sorted(fused.values(), key=lambda x: x["score"], reverse=True)[:limit]
    out: list[SearchResult] = []
    for entry in ordered:
        r = entry["row"]
        out.append(
            SearchResult(
                path=r["path"],
                type=r["type"],
                owners=list(r["owners"] or []),
                updated=r["updated"],
                version=r["version"],
                score=float(entry["score"]),
                snippet=r["snippet"] or "",
            )
        )
    return out


async def handle_audit(db: Database, inp: AuditInput) -> AuditOutput:
    rows = await db.list_audit(
        path=inp.path,
        since=inp.since,
        server_id=inp.server_id,
        app=inp.app,
        operation=inp.operation,
        limit=inp.limit,
    )
    entries = [
        AuditEntry(
            id=r["id"],
            ts=r["ts"],
            operation=r["operation"],
            path=r["path"],
            server_id=r["server_id"],
            app=r["app"],
            agent_model=r["agent_model"],
            prompt_hash=r["prompt_hash"],
            version_before=r["version_before"],
            version_after=r["version_after"],
            outcome=r["outcome"],
            error_code=r["error_code"],
            client_ip=str(r["client_ip"]) if r["client_ip"] is not None else None,
        )
        for r in rows
    ]
    return AuditOutput(entries=entries)


async def handle_append_log(
    db: Database,
    inp: LogEntryInput,
    *,
    authed: AuthenticatedServer | None = None,
) -> LogEntryOutput:
    _enforce_session_matches_server(inp.server_id, inp.app, authed)

    log_id, gv = await db.append_log(
        timestamp=inp.timestamp,
        server_id=inp.server_id,
        app=inp.app,
        modules_touched=inp.modules_touched,
        integrations_updated=inp.integrations_updated,
        notable=inp.notable,
    )
    return LogEntryOutput(id=log_id, global_version=gv)


# =============================================================
# JSON adapters (so the MCP transport layer can speak dict↔Pydantic without coupling)
# =============================================================


async def call_handler_by_name(
    name: str,
    args: dict[str, Any],
    *,
    db: Database,
    wiki_dir: Path,
    authed: AuthenticatedServer | None = None,
    max_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES,
    ownership: OwnershipRules | None = None,
    embedder: EmbeddingProvider | None = None,
) -> dict[str, Any]:
    """Dispatch a tool call by name. Returns a JSON-serializable dict.

    Raises :class:`ToolError` on protocol-level errors.

    ``authed`` carries the result of the FastAPI auth dependency: an
    :class:`AuthenticatedServer` when bearer-token auth is enforced, or
    ``None`` when the master runs in unauthenticated dev mode (no
    registered servers).
    """
    if name == "wiki.read":
        return (await handle_read(db, ReadInput(**args))).model_dump(mode="json")
    if name == "wiki.write":
        return (
            await handle_write(
                db,
                wiki_dir,
                WriteInput(**args),
                authed=authed,
                max_bytes=max_bytes,
                ownership=ownership,
            )
        ).model_dump(mode="json")
    if name == "wiki.list":
        return (await handle_list(db, ListInput(**args))).model_dump(mode="json")
    if name == "wiki.append_log":
        return (
            await handle_append_log(db, LogEntryInput(**args), authed=authed)
        ).model_dump(mode="json")
    if name == "wiki.audit":
        return (await handle_audit(db, AuditInput(**args))).model_dump(mode="json")
    if name == "wiki.search":
        return (
            await handle_search(db, SearchInput(**args), embedder=embedder)
        ).model_dump(mode="json")

    raise ToolError(f"Unknown tool: {name}", status=400)
