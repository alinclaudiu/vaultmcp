"""Write-side validation middleware.

Every ``wiki.write`` call passes through these checks **before** the DB
sees it. The four buckets, per DESIGN.md §7.3:

1. **Encoding.** UTF-8 only; no NUL bytes; no bare ``\\r`` line endings.
2. **Size.** Default 1 MiB cap, configurable via the master config.
3. **Frontmatter.** ``title``, ``type``, ``owners``, ``updated`` are
   required; ``type`` must be one of the known page types.
4. **Secrets.** Regex catalog of common credential patterns. Errors
   surface the *pattern name*, never the matched string, so the audit
   log doesn't become a leak vector.

All checks raise :class:`ValidationFailed` (HTTP 422) except the size
check, which raises :class:`SizeLimitExceeded` (HTTP 413).
"""

from __future__ import annotations

import re
from typing import Final

from ..shared import frontmatter as fm_mod
from .errors import SizeLimitExceeded, ValidationFailed

__all__ = [
    "DEFAULT_MAX_FILE_SIZE_BYTES",
    "REQUIRED_FRONTMATTER_KEYS",
    "validate_write_content",
]


DEFAULT_MAX_FILE_SIZE_BYTES: Final[int] = 1 * 1024 * 1024  # 1 MiB

REQUIRED_FRONTMATTER_KEYS: Final[tuple[str, ...]] = (
    "title",
    "type",
    "owners",
    "updated",
)


# =============================================================
# Secret pattern catalog
# =============================================================
#
# Names are surfaced in error messages — keep them stable, since users
# may want to add a `# noqa: <pattern>` style escape hatch later.
# Patterns are tuned for low false-positive rates. They are NOT a
# replacement for proper secret scanning — defense in depth only.

_SECRET_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    (
        "aws-access-key-id",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    ),
    (
        "github-personal-access-token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{36,255}\b"),
    ),
    (
        "github-fine-grained-pat",
        re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    ),
    (
        "slack-bot-or-user-token",
        re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    ),
    (
        "bcrypt-hash",
        re.compile(r"\$2[aby]?\$\d{2}\$[./A-Za-z0-9]{53}"),
    ),
    (
        "postgres-uri-with-password",
        # postgres://user:password@host... — only flags when ":" + "@"
        # both appear, so plain usernames-only DSNs slip through.
        re.compile(r"\bpostgres(?:ql)?://[^\s/:@]+:[^\s/:@]+@[^\s/]+"),
    ),
    (
        "mysql-uri-with-password",
        re.compile(r"\bmysql://[^\s/:@]+:[^\s/:@]+@[^\s/]+"),
    ),
    (
        "private-key-pem",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED |)PRIVATE KEY-----"
        ),
    ),
)


# =============================================================
# Public entry point
# =============================================================


def validate_write_content(
    content: str,
    *,
    max_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES,
) -> None:
    """Run every check in order; raise on the first failure.

    Order matters: cheap structural checks (size, encoding) run before
    parsing or regex scans so we don't spend time on huge garbage.
    """
    _check_size(content, max_bytes)
    _check_encoding(content)
    metadata = _check_frontmatter(content)
    _check_no_secrets(content)
    # metadata is returned by _check_frontmatter for the type-enum check
    # but the caller (handle_write) re-parses anyway — keep this private.
    _ = metadata


# =============================================================
# Individual checks
# =============================================================


def _check_size(content: str, limit: int) -> None:
    size = len(content.encode("utf-8", errors="ignore"))
    if size > limit:
        raise SizeLimitExceeded(size=size, limit=limit)


def _check_encoding(content: str) -> None:
    # Python strings are already Unicode; the input arrived via JSON, so
    # invalid UTF-8 bytes can't survive parsing. We still defend against
    # NULs and bare CRs, which are markers of binary or Windows-mangled
    # input that the wiki contract forbids.
    if "\x00" in content:
        raise ValidationFailed("Content contains NUL byte (0x00)")
    if _has_bare_cr(content):
        raise ValidationFailed(
            "Content contains bare \\r (CR not followed by LF); "
            "convert line endings to LF or CRLF"
        )


def _has_bare_cr(content: str) -> bool:
    # Scan once, allowing CR only when immediately followed by LF.
    for i, ch in enumerate(content):
        if ch != "\r":
            continue
        if i + 1 >= len(content) or content[i + 1] != "\n":
            return True
    return False


def _check_frontmatter(content: str) -> dict:
    try:
        metadata, _body = fm_mod.parse(content)
    except fm_mod.FrontmatterError as exc:
        raise ValidationFailed(f"Frontmatter invalid: {exc}") from exc

    if not metadata:
        raise ValidationFailed(
            "Frontmatter is required (page must start with a '---' block)"
        )

    missing = [k for k in REQUIRED_FRONTMATTER_KEYS if k not in metadata]
    if missing:
        raise ValidationFailed(
            f"Frontmatter is missing required keys: {', '.join(missing)}"
        )

    title = metadata.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValidationFailed("Frontmatter 'title' must be a non-empty string")

    type_ = metadata.get("type")
    if not isinstance(type_, str) or type_ not in fm_mod.KNOWN_PAGE_TYPES:
        raise ValidationFailed(
            f"Frontmatter 'type' must be one of {sorted(fm_mod.KNOWN_PAGE_TYPES)}; "
            f"got {type_!r}"
        )

    owners = metadata.get("owners")
    if not isinstance(owners, list) or not owners:
        raise ValidationFailed(
            "Frontmatter 'owners' must be a non-empty list of strings"
        )
    if any(not isinstance(o, str) or not o.strip() for o in owners):
        raise ValidationFailed("Every entry in 'owners' must be a non-empty string")

    updated = fm_mod.extract_updated(metadata)
    if updated is None:
        raise ValidationFailed(
            "Frontmatter 'updated' must be an ISO date or datetime"
        )

    return metadata


def _check_no_secrets(content: str) -> None:
    for name, pattern in _SECRET_PATTERNS:
        if pattern.search(content):
            # Surface the pattern name only — never the matched substring.
            raise ValidationFailed(
                f"Content matches secret pattern {name!r}; remove the secret "
                "(or rotate it if already committed) before retrying"
            )
