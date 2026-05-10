"""Pure tool handler functions.

Each function maps to an MCP tool exposed by the master. They take a
:class:`Database` and Pydantic input model, return a Pydantic output (or
raise :class:`ToolError` for protocol-level errors). This separation
means the handlers are testable without an MCP server running — the
server module wires them into the actual MCP transport.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..shared import frontmatter as fm_mod
from ..shared.types import (
    ListEntry,
    ListInput,
    ListOutput,
    LogEntryInput,
    LogEntryOutput,
    PageMetadata,
    ReadInput,
    ReadOutput,
    WriteConflict,
    WriteInput,
    WriteOutput,
)
from .auth import AuthenticatedServer
from .db import ConflictResult, Database, WriteResult
from .errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ToolError,
    ValidationFailed,
)
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
    "handle_list",
    "handle_read",
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
) -> WriteOutput:
    _enforce_session_matches_server(inp.session.server_id, inp.session.app, authed)

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
                db, wiki_dir, WriteInput(**args), authed=authed, max_bytes=max_bytes
            )
        ).model_dump(mode="json")
    if name == "wiki.list":
        return (await handle_list(db, ListInput(**args))).model_dump(mode="json")
    if name == "wiki.append_log":
        return (
            await handle_append_log(db, LogEntryInput(**args), authed=authed)
        ).model_dump(mode="json")

    raise ToolError(f"Unknown tool: {name}", status=400)
