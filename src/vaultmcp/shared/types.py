"""Pydantic models for MCP tool I/O and internal data transfer.

Mirrors the schema in DESIGN.md §4 (`wiki.read`, `wiki.write`, etc.).
Both master (server) and agent (client) import these so the wire format
is one source of truth.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PageType = Literal[
    "app",
    "integration",
    "flow",
    "adr",
    "log",
    "index",
    "module",
    "debugging",
    "runbook",
    "shared",
]

EventType = Literal["PageChanged", "PageDeleted", "LogAppended", "Heartbeat"]

Operation = Literal["read", "write", "delete", "subscribe", "append_log", "search"]

Outcome = Literal["ok", "conflict", "forbidden", "validation_failed"]


# =============================================================
# Session (who's making the call)
# =============================================================


class Session(BaseModel):
    """Identifies the caller for audit + ownership purposes.

    All `wiki.write` and `wiki.append_log` calls must include one.
    Phase 3 will gate access by ``server_id`` ↔ token mapping; for v0.2
    the master accepts any well-formed Session.
    """

    server_id: str = Field(..., min_length=1, max_length=64)
    app: str = Field(..., min_length=1, max_length=64)
    agent_model: str = Field(..., min_length=1, max_length=128)
    prompt_hash: str | None = Field(default=None, max_length=128)
    session_id: str = Field(default="", max_length=128)


# =============================================================
# Page metadata (the bits of frontmatter we surface in API responses)
# =============================================================


class PageMetadata(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: PageType
    owners: list[str] = Field(default_factory=list)
    updated: datetime | None = None
    last_writer_server: str
    last_writer_app: str
    last_writer_session: str


# =============================================================
# wiki.read
# =============================================================


class ReadInput(BaseModel):
    path: str = Field(..., min_length=1)


class ReadOutput(BaseModel):
    path: str
    content: str
    version: int
    global_version: int
    metadata: PageMetadata


# =============================================================
# wiki.write
# =============================================================


class WriteInput(BaseModel):
    path: str = Field(..., min_length=1)
    content: str
    base_version: int | None = Field(default=None, ge=0)
    session: Session


class WriteOutput(BaseModel):
    path: str
    version: int
    global_version: int
    applied_at: datetime


class WriteConflict(BaseModel):
    """Body of a 409 response — informs the agent what to resync to."""

    current_version: int
    current_content: str
    last_writer: str  # server_id of the winner


# =============================================================
# wiki.list
# =============================================================


class ListInput(BaseModel):
    prefix: str | None = None
    type: PageType | None = None
    updated_since: datetime | None = None
    cursor: str | None = None
    limit: int = Field(default=100, ge=1, le=500)


class ListEntry(BaseModel):
    path: str
    type: PageType
    owners: list[str]
    updated: datetime
    version: int
    global_version: int


class ListOutput(BaseModel):
    entries: list[ListEntry]
    next_cursor: str | None = None


# =============================================================
# wiki.append_log
# =============================================================


class LogEntryInput(BaseModel):
    timestamp: datetime
    server_id: str
    app: str
    modules_touched: list[str] = Field(default_factory=list)
    integrations_updated: list[str] = Field(default_factory=list)
    notable: list[str] = Field(default_factory=list)


class LogEntryOutput(BaseModel):
    id: int
    global_version: int


# =============================================================
# wiki.search
# =============================================================


SearchMode = Literal["lexical", "semantic", "hybrid"]


class SearchInput(BaseModel):
    """Query inputs for ``wiki.search``.

    ``mode`` selects the ranker:

    - ``lexical`` (default) — Postgres ``websearch_to_tsquery`` over a
      tsvector that combines title (weight A) and body (weight B).
    - ``semantic`` — pgvector cosine distance against ``embeddings``.
      Requires the master to have an embedding provider configured.
    - ``hybrid`` — both rankers combined via Reciprocal Rank Fusion.
    """

    query: str = Field(..., min_length=1)
    prefix: str | None = None
    type: PageType | None = None
    mode: SearchMode = "lexical"
    limit: int = Field(default=20, ge=1, le=200)


class SearchResult(BaseModel):
    path: str
    type: PageType
    owners: list[str]
    updated: datetime
    version: int
    score: float
    snippet: str = ""


class SearchOutput(BaseModel):
    entries: list[SearchResult]


# =============================================================
# wiki.audit
# =============================================================


class AuditInput(BaseModel):
    """Filters for ``wiki.audit``.

    All fields are optional. ``path`` matches exactly (use a follow-up
    tool with prefix support if needed); ``since`` filters by ``ts``.
    """

    path: str | None = None
    since: datetime | None = None
    server_id: str | None = None
    app: str | None = None
    operation: Operation | None = None
    limit: int = Field(default=100, ge=1, le=500)


class AuditEntry(BaseModel):
    id: int
    ts: datetime
    operation: Operation
    path: str | None = None
    server_id: str
    app: str
    agent_model: str | None = None
    prompt_hash: str | None = None
    version_before: int | None = None
    version_after: int | None = None
    outcome: Outcome
    error_code: str | None = None
    client_ip: str | None = None


class AuditOutput(BaseModel):
    entries: list[AuditEntry]


# =============================================================
# ext.* (extensions) — per docs/03-extensibility.md
# =============================================================


# Whitelist of column types extensions are allowed to declare. Avoids
# arbitrary SQL injection through type strings, and keeps cross-app
# schema simple. Add to this set deliberately as needs surface.
EXT_COLUMN_TYPES: tuple[str, ...] = (
    "text",
    "int",
    "bigint",
    "smallint",
    "boolean",
    "uuid",
    "timestamptz",
    "date",
    "jsonb",
    "numeric",
    "real",
    "double precision",
)


class ExtColumnSpec(BaseModel):
    """One column in an extension table. Modeled after the API in
    docs/03-extensibility.md.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=63, pattern=r"^[a-z][a-z0-9_]*$")
    type: str = Field(...)
    primary_key: bool = False
    not_null: bool = False
    default: str | None = None  # raw SQL expression (e.g. "NOW()", "'pending'")
    references: str | None = None  # raw "<table>(<col>)" — soft-checked


class ExtPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    can_read_pages: bool = False
    can_read_audit: bool = False
    can_use_embeddings: bool = False
    can_subscribe_events: bool = False


class ExtRegisterInput(BaseModel):
    name: str = Field(..., min_length=1, max_length=63, pattern=r"^[a-z][a-z0-9_]*$")
    schema_prefix: str | None = None  # default: "ext_<name>_"
    owners: list[str] = Field(default_factory=list)
    policy: ExtPolicy = Field(default_factory=ExtPolicy)


class ExtRegisterOutput(BaseModel):
    name: str
    schema_prefix: str
    schema_version: int
    created_at: datetime


class ExtListOutput(BaseModel):
    extensions: list[ExtRegisterOutput]


class ExtDeclareTableInput(BaseModel):
    extension: str = Field(..., min_length=1)
    name: str = Field(
        ..., min_length=1, max_length=63, pattern=r"^[a-z][a-z0-9_]*$"
    )
    columns: list[ExtColumnSpec] = Field(..., min_length=1)


class ExtDeclareTableOutput(BaseModel):
    extension: str
    full_table_name: str   # e.g. "ext_crm_contacts"
    column_count: int


class ExtQueryInput(BaseModel):
    extension: str = Field(..., min_length=1)
    sql: str = Field(..., min_length=1)
    params: list[Any] = Field(default_factory=list)
    limit: int = Field(default=500, ge=1, le=5000)


class ExtQueryOutput(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool


# =============================================================
# wiki.subscribe (SSE streaming)
# =============================================================


class SubscribeInput(BaseModel):
    prefix: str | None = None
    since_global_version: int | None = Field(default=None, ge=0)


class StreamEvent(BaseModel):
    """One event in the SSE stream sent to subscribed agents."""

    event: EventType
    global_version: int
    # Conditional fields per event type:
    path: str | None = None
    version: int | None = None
    content: str | None = None
    metadata: PageMetadata | None = None
    log_entry: LogEntryInput | None = None
    server_time: datetime | None = None  # heartbeat only
