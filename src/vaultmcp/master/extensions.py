"""Extension lifecycle: register, declare tables, run scoped queries.

Postgres-role isolation
-----------------------

Each extension gets a dedicated NOLOGIN Postgres role
(``vaultmcp_ext_<name>``). On register, the master grants the role the
core-table privileges spelled out in the extension's policy
(``can_read_pages`` → ``GRANT SELECT ON pages``, etc.) and ``USAGE`` on
``public``. On ``ext.declare_table``, the new ``ext_<name>_*`` table is
granted full DML to the role.

``ext.query`` and ``ext.exec`` open a transaction and issue
``SET LOCAL ROLE <role>`` before running the user's SQL — ``SET LOCAL``
auto-resets at transaction end so even an ``asyncpg`` exception cannot
leave the connection running as the extension role.

Per ``docs/03-extensibility.md``, the master is a substrate that other
applications can build on. Each extension owns a reserved schema prefix
(``ext_<name>_*``) and reads from core tables according to its policy.

The v0.5 implementation enforces the prefix at the application layer.
Full Postgres-role isolation (``CREATE ROLE`` per extension, schema-
level ``GRANT``s, ``SET ROLE`` per query) lands in v0.6 hardening once
the operator-side privilege story is documented.

Pieces in this module:

- :func:`column_spec_to_sql` — Pydantic ``ExtColumnSpec`` → DDL fragment.
- :func:`build_create_table_sql` — full ``CREATE TABLE`` for an
  extension declaring a new table. Validates the table name + column
  spec; the resulting SQL has no user-controlled identifiers (every
  bit is regex-validated by the Pydantic models or comes from this
  function), so it's safe to splice.
- :func:`assert_query_is_readonly` — quick lexical reject of write
  keywords in ``ext.query`` SQL. Belt; the suspenders are a Postgres
  ``READ ONLY`` transaction in :class:`Database`.
"""

from __future__ import annotations

import re
from typing import Final

from ..shared.types import EXT_COLUMN_TYPES, ExtColumnSpec
from .errors import ToolError, ValidationFailed


# Identifier regex re-stated here so we can validate names that aren't
# coming through Pydantic (e.g. parameters synthesized at runtime).
_IDENT_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]*$")


# Default schema-prefix template when ``ext.register`` doesn't pass one.
def default_schema_prefix(name: str) -> str:
    return f"ext_{name}_"


def role_name_for(extension_name: str) -> str:
    """Postgres role name owned by an extension. Derived from a regex-
    validated extension name, so the result is safe to splice into DDL.
    """
    if not _IDENT_RE.fullmatch(extension_name):
        raise ValidationFailed(
            f"Extension name {extension_name!r} cannot be turned into a role name"
        )
    return f"vaultmcp_ext_{extension_name}"


def grants_for_policy(role_name: str, policy: dict[str, bool]) -> list[str]:
    """Return the GRANT statements that map a policy to Postgres ACLs.

    The role always gets ``USAGE`` on ``public`` (so it can resolve
    table names) and ``SELECT`` on ``extensions`` itself (so the
    extension can introspect its own metadata if it wants).
    Everything else is gated by the policy flags.
    """
    grants = [
        f"GRANT USAGE ON SCHEMA public TO {role_name}",
        f"GRANT SELECT ON extensions TO {role_name}",
    ]
    if policy.get("can_read_pages"):
        grants.append(f"GRANT SELECT ON pages TO {role_name}")
    if policy.get("can_read_audit"):
        grants.append(f"GRANT SELECT ON audit TO {role_name}")
    if policy.get("can_use_embeddings"):
        # Extensions index their own content; needs DML on embeddings
        # plus the embedding_jobs queue to enqueue work.
        grants.append(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON embeddings TO {role_name}"
        )
        grants.append(
            f"GRANT SELECT, INSERT ON embedding_jobs TO {role_name}"
        )
        grants.append(
            f"GRANT USAGE ON SEQUENCE embedding_jobs_id_seq TO {role_name}"
        )
    if policy.get("can_subscribe_events"):
        # Extensions emit events alongside wiki activity (ext.emit_event).
        grants.append(f"GRANT SELECT, INSERT ON events TO {role_name}")
        grants.append(f"GRANT USAGE ON SEQUENCE events_id_seq TO {role_name}")
    return grants


# Lexical reject list for ``ext.query``. Matched as whole words,
# case-insensitive. The READ ONLY transaction would catch any of these
# at the DB layer too — this is the front-line check that produces a
# clear error before we even open a transaction.
_WRITE_KEYWORDS: Final[tuple[str, ...]] = (
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "truncate",
    "create",
    "grant",
    "revoke",
    "comment",
    "copy",
    "vacuum",
    "cluster",
    "do",
    "set",
)
_WRITE_KW_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(" + "|".join(_WRITE_KEYWORDS) + r")\b", re.IGNORECASE
)


def assert_query_is_readonly(sql: str) -> None:
    """Reject SQL that contains an obvious write keyword.

    First-line defense. The transaction is also opened READ ONLY by
    :class:`Database`, so a clever bypass still can't mutate state —
    but we'd rather refuse with a clear message than wait for the DB.
    """
    match = _WRITE_KW_RE.search(sql)
    if match:
        raise ValidationFailed(
            f"ext.query is read-only; SQL contains forbidden keyword "
            f"{match.group(1).lower()!r}. Use the corresponding extension API "
            "(e.g. ext.exec, when added) for mutations."
        )


def assert_valid_extension_name(name: str) -> None:
    if not _IDENT_RE.fullmatch(name):
        raise ValidationFailed(
            f"Extension name {name!r} must match ^[a-z][a-z0-9_]*$"
        )


def column_spec_to_sql(col: ExtColumnSpec) -> str:
    """Translate one validated column spec into a DDL fragment."""
    if col.type not in EXT_COLUMN_TYPES:
        raise ValidationFailed(
            f"Column {col.name!r}: type {col.type!r} not in the allowlist "
            f"{list(EXT_COLUMN_TYPES)}"
        )
    parts: list[str] = [col.name, col.type]
    if col.primary_key:
        parts.append("PRIMARY KEY")
    if col.not_null and not col.primary_key:
        parts.append("NOT NULL")
    if col.default is not None:
        # Default expression is raw SQL — operators are trusted (they
        # called ext.declare_table with this value). The Pydantic model
        # restricts to a string; we don't try to validate the expression
        # itself.
        parts.append(f"DEFAULT {col.default}")
    if col.references is not None:
        # Soft check: must look like "<table>(<col>)". We don't try to
        # verify the target exists.
        if not re.fullmatch(r"[a-z_][a-z0-9_]*\([a-z_][a-z0-9_]*\)", col.references):
            raise ValidationFailed(
                f"Column {col.name!r}: references must be of the form "
                "'table(column)'"
            )
        parts.append(f"REFERENCES {col.references}")
    return " ".join(parts)


def build_create_table_sql(
    *,
    schema_prefix: str,
    table_name: str,
    columns: list[ExtColumnSpec],
) -> tuple[str, str]:
    """Render the CREATE TABLE for an extension table declaration.

    Returns ``(sql, full_table_name)``. Caller passes the **already-
    validated** ``schema_prefix`` from the extensions row so this
    function doesn't trust user input directly for the qualified
    table name.
    """
    if not _IDENT_RE.fullmatch(table_name):
        raise ValidationFailed(
            f"Table name {table_name!r} must match ^[a-z][a-z0-9_]*$"
        )
    if not columns:
        raise ValidationFailed("At least one column is required")

    seen: set[str] = set()
    primary_keys = 0
    for c in columns:
        if c.name in seen:
            raise ValidationFailed(f"Duplicate column name: {c.name!r}")
        seen.add(c.name)
        if c.primary_key:
            primary_keys += 1
    if primary_keys > 1:
        raise ValidationFailed(
            "At most one column can be primary_key=True (compound keys "
            "are not supported through this API yet)"
        )

    full = f"{schema_prefix}{table_name}"
    body = ",\n    ".join(column_spec_to_sql(c) for c in columns)
    sql = f"CREATE TABLE IF NOT EXISTS {full} (\n    {body}\n)"
    return sql, full


__all__ = [
    "ToolError",  # re-export so callers don't need a second import
    "assert_query_is_readonly",
    "assert_valid_extension_name",
    "build_create_table_sql",
    "column_spec_to_sql",
    "default_schema_prefix",
    "grants_for_policy",
    "role_name_for",
]
