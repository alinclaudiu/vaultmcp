"""Unit tests for the write-side validation middleware."""

from __future__ import annotations

import pytest

from vaultmcp.master.errors import SizeLimitExceeded, ValidationFailed
from vaultmcp.master.validation import (
    DEFAULT_MAX_FILE_SIZE_BYTES,
    REQUIRED_FRONTMATTER_KEYS,
    validate_write_content,
)

# ---------- helpers ----------


def _page(**overrides: object) -> str:
    """Build a minimal valid page; overrides replace specific frontmatter values."""
    fm = {
        "title": "Test",
        "type": "app",
        "owners": ["testapp"],
        "updated": "2026-05-10",
    }
    fm.update(overrides)
    lines = ["---"]
    for k, v in fm.items():
        if isinstance(v, list):
            inner = ", ".join(str(x) for x in v)
            lines.append(f"{k}: [{inner}]")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    lines.append("# Body.")
    return "\n".join(lines) + "\n"


# ---------- happy path ----------


def test_valid_page_passes() -> None:
    validate_write_content(_page())


def test_required_keys_constant_matches_design() -> None:
    # Tripwire: the validator and DESIGN.md should agree on the required set.
    assert set(REQUIRED_FRONTMATTER_KEYS) == {"title", "type", "owners", "updated"}


# ---------- size ----------


def test_oversize_payload_raises_413() -> None:
    huge = _page() + ("x" * (DEFAULT_MAX_FILE_SIZE_BYTES + 1))
    with pytest.raises(SizeLimitExceeded) as ei:
        validate_write_content(huge)
    assert ei.value.status == 413
    assert ei.value.size > ei.value.limit


def test_size_limit_is_configurable() -> None:
    with pytest.raises(SizeLimitExceeded):
        validate_write_content(_page(), max_bytes=10)


# ---------- encoding ----------


def test_nul_byte_rejected() -> None:
    with pytest.raises(ValidationFailed, match="NUL"):
        validate_write_content(_page() + "\x00")


def test_bare_cr_rejected() -> None:
    with pytest.raises(ValidationFailed, match=r"\\r"):
        validate_write_content(_page() + "trailing\rmore")


def test_crlf_is_allowed() -> None:
    validate_write_content(_page() + "windows\r\nlines\r\n")


# ---------- frontmatter ----------


def test_missing_frontmatter_block_rejected() -> None:
    with pytest.raises(ValidationFailed, match="required"):
        validate_write_content("# No frontmatter at all\n")


@pytest.mark.parametrize("missing_key", REQUIRED_FRONTMATTER_KEYS)
def test_missing_required_key_rejected(missing_key: str) -> None:
    fm = {
        "title": "Test",
        "type": "app",
        "owners": ["testapp"],
        "updated": "2026-05-10",
    }
    fm.pop(missing_key)
    lines = ["---"]
    for k, v in fm.items():
        lines.append(f"{k}: {v}")
    lines.extend(["---", "", "# Body.\n"])
    with pytest.raises(ValidationFailed, match=missing_key):
        validate_write_content("\n".join(lines))


def test_unknown_type_rejected() -> None:
    with pytest.raises(ValidationFailed, match="type"):
        validate_write_content(_page(type="unknown-bogus-type"))


def test_empty_owners_rejected() -> None:
    with pytest.raises(ValidationFailed, match="owners"):
        validate_write_content(_page(owners=[]))


def test_blank_title_rejected() -> None:
    with pytest.raises(ValidationFailed, match="title"):
        validate_write_content(_page(title="   "))


def test_invalid_updated_rejected() -> None:
    with pytest.raises(ValidationFailed, match="updated"):
        validate_write_content(_page(updated="not-a-date"))


# ---------- secrets ----------


@pytest.mark.parametrize(
    "leak,name_fragment",
    [
        (" Use AKIA1234567890ABCDEF in prod. ", "aws-access-key-id"),
        (" Token: ghp_abcdef" + "0" * 40, "github-personal-access-token"),
        (" Token: github_pat_" + "A" * 30, "github-fine-grained-pat"),
        (" xoxb-1234-5678-aBcDeFgHiJkLmN ", "slack-bot-or-user-token"),
        (
            " postgres://app:correcthorsebatterystaple@db.internal:5432/x ",
            "postgres-uri-with-password",
        ),
        (" mysql://root:hunter2@db:3306/foo ", "mysql-uri-with-password"),
        (
            " -----BEGIN OPENSSH PRIVATE KEY----- ",
            "private-key-pem",
        ),
    ],
)
def test_secret_pattern_rejected(leak: str, name_fragment: str) -> None:
    page = _page() + leak + "\n"
    with pytest.raises(ValidationFailed, match=name_fragment):
        validate_write_content(page)


def test_secret_pattern_error_does_not_echo_the_secret() -> None:
    secret = "AKIA1234567890ABCDEF"
    page = _page() + f"\nUse {secret} for testing\n"
    with pytest.raises(ValidationFailed) as ei:
        validate_write_content(page)
    # Tripwire: surfacing the matched string back to the caller would
    # send it to the audit log + 422 body. Pattern name only.
    assert secret not in str(ei.value)


def test_uri_without_password_is_not_flagged() -> None:
    # "postgres://app@db..." has no `:password@`, so the pattern shouldn't fire.
    validate_write_content(_page() + "\nDSN: postgres://app@db.internal:5432/x\n")
