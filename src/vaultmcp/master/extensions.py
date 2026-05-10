"""Extension lifecycle: register, declare tables, run scoped queries.

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
    "assert_query_is_readonly",
    "assert_valid_extension_name",
    "build_create_table_sql",
    "column_spec_to_sql",
    "default_schema_prefix",
    "ToolError",  # re-export so callers don't need a second import
]
