"""YAML frontmatter parsing for wiki pages.

A wiki page is a markdown file that starts with a YAML front-matter block
delimited by ``---`` lines:

    ---
    title: Inventory README
    type: app
    status: active
    owners: [inventory]
    updated: 2026-05-09
    ---

    # Inventory

    The actual markdown body...

This module parses that block into a Python dict and returns the body
separately. It does *not* validate semantic correctness (required keys,
type enums, etc.) — that lives in the master's validation middleware
(Phase 3). It only enforces that the front-matter is parseable YAML.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import yaml

# The page type values listed in DESIGN.md §3.3.
KNOWN_PAGE_TYPES = frozenset(
    {
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
    }
)


class FrontmatterError(ValueError):
    """Raised when frontmatter is malformed (missing delimiters, invalid YAML)."""


def parse(content: str) -> tuple[dict[str, Any], str]:
    """Parse frontmatter and body from a wiki page.

    Returns ``(metadata_dict, body_text)``. If the page has no frontmatter
    (no leading ``---``), returns ``({}, content)`` — the absence is not
    an error here; enforcement happens at the validation layer.

    Raises :class:`FrontmatterError` if frontmatter is started but malformed.
    """
    # Strip BOM if present; tolerate CRLF in delimiter lines.
    if content.startswith("﻿"):
        content = content[1:]

    lines = content.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, content

    # Find the closing delimiter.
    try:
        end_index = next(
            i for i in range(1, len(lines)) if lines[i].strip() == "---"
        )
    except StopIteration as exc:
        raise FrontmatterError(
            "Frontmatter block opened with '---' but never closed"
        ) from exc

    fm_text = "\n".join(lines[1:end_index])
    body = "\n".join(lines[end_index + 1 :])
    # Trim a single leading blank line in the body (idiomatic).
    if body.startswith("\n"):
        body = body[1:]

    try:
        parsed = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as exc:
        raise FrontmatterError(f"Frontmatter is not valid YAML: {exc}") from exc

    if not isinstance(parsed, dict):
        raise FrontmatterError(
            f"Frontmatter must be a YAML mapping, got {type(parsed).__name__}"
        )

    return _normalize(parsed), body


def serialize(metadata: dict[str, Any], body: str) -> str:
    """Compose a wiki page from frontmatter dict + body.

    Inverse of :func:`parse` (modulo formatting choices). Always writes
    a trailing newline.
    """
    if not metadata:
        return body if body.endswith("\n") else body + "\n"

    fm_text = yaml.safe_dump(
        _denormalize(metadata),
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    ).rstrip("\n")

    if body and not body.endswith("\n"):
        body += "\n"

    return f"---\n{fm_text}\n---\n\n{body}"


def extract_type(metadata: dict[str, Any]) -> str:
    """Return the page type, defaulting to ``"shared"`` if absent.

    The validation layer (Phase 3) will reject pages with a missing or
    unknown type. For now, we accept anything and let the column NOT NULL
    constraint do its work via the default.
    """
    raw = metadata.get("type")
    if isinstance(raw, str) and raw in KNOWN_PAGE_TYPES:
        return raw
    return "shared"


def extract_owners(metadata: dict[str, Any]) -> list[str]:
    """Return owners as a list of strings; empty if absent or malformed."""
    raw = metadata.get("owners")
    if isinstance(raw, list):
        return [str(x) for x in raw if isinstance(x, (str, int))]
    if isinstance(raw, str):
        return [raw]
    return []


def extract_updated(metadata: dict[str, Any]) -> datetime | None:
    """Return the ``updated`` field as a datetime, or None if absent/invalid."""
    raw = metadata.get("updated")
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, date):
        return datetime.combine(raw, datetime.min.time())
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None
    return None


# =============================================================
# Internal helpers
# =============================================================


def _normalize(parsed: dict[str, Any]) -> dict[str, Any]:
    """Normalize YAML output for downstream use.

    Currently only converts ``date`` to ``datetime`` (Postgres TIMESTAMPTZ
    expects datetime), and keeps everything else as-is.
    """
    out: dict[str, Any] = {}
    for k, v in parsed.items():
        if isinstance(v, date) and not isinstance(v, datetime):
            out[k] = datetime.combine(v, datetime.min.time())
        else:
            out[k] = v
    return out


def _denormalize(metadata: dict[str, Any]) -> dict[str, Any]:
    """Inverse of :func:`_normalize` for serialization.

    Converts datetimes back to ISO-date strings if their time component is
    midnight (matches the typical ``updated: YYYY-MM-DD`` convention).
    """
    out: dict[str, Any] = {}
    for k, v in metadata.items():
        if isinstance(v, datetime) and v.time() == datetime.min.time():
            out[k] = v.date().isoformat()
        else:
            out[k] = v
    return out
