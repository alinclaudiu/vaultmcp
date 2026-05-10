"""Unit tests for shared.frontmatter — runs without any infrastructure."""

from __future__ import annotations

from datetime import datetime

import pytest

from vaultmcp.shared.frontmatter import (
    FrontmatterError,
    extract_owners,
    extract_type,
    extract_updated,
    parse,
    serialize,
)


def test_parse_with_frontmatter() -> None:
    content = """---
title: Inventory README
type: app
owners: [inventory]
updated: 2026-05-09
---

# Inventory

Some body content.
"""
    metadata, body = parse(content)
    assert metadata["title"] == "Inventory README"
    assert metadata["type"] == "app"
    assert metadata["owners"] == ["inventory"]
    assert isinstance(metadata["updated"], datetime)
    assert body.startswith("# Inventory")


def test_parse_without_frontmatter() -> None:
    content = "# No frontmatter here\n\nJust plain body."
    metadata, body = parse(content)
    assert metadata == {}
    assert body == content


def test_parse_unclosed_frontmatter_raises() -> None:
    content = "---\ntitle: foo\nstill in frontmatter, no close\n"
    with pytest.raises(FrontmatterError):
        parse(content)


def test_parse_invalid_yaml_raises() -> None:
    content = "---\nfoo: : : bad\n---\n\nbody"
    with pytest.raises(FrontmatterError):
        parse(content)


def test_parse_non_mapping_yaml_raises() -> None:
    content = "---\n- just\n- a\n- list\n---\n\nbody"
    with pytest.raises(FrontmatterError):
        parse(content)


def test_extract_type_known() -> None:
    assert extract_type({"type": "app"}) == "app"
    assert extract_type({"type": "integration"}) == "integration"


def test_extract_type_unknown_falls_back() -> None:
    assert extract_type({"type": "weird"}) == "shared"
    assert extract_type({}) == "shared"


def test_extract_owners() -> None:
    assert extract_owners({"owners": ["a", "b"]}) == ["a", "b"]
    assert extract_owners({"owners": "single"}) == ["single"]
    assert extract_owners({}) == []


def test_extract_updated() -> None:
    assert extract_updated({"updated": "2026-05-09"}) == datetime(2026, 5, 9)
    assert extract_updated({"updated": datetime(2026, 5, 9, 12)}) == datetime(2026, 5, 9, 12)
    assert extract_updated({}) is None
    assert extract_updated({"updated": "not-a-date"}) is None


def test_serialize_roundtrip() -> None:
    metadata = {"title": "X", "type": "app", "owners": ["x"]}
    body = "# X\n\nbody.\n"
    text = serialize(metadata, body)
    assert text.startswith("---\n")
    parsed_meta, parsed_body = parse(text)
    assert parsed_meta == metadata
    assert parsed_body == body


def test_serialize_empty_metadata() -> None:
    assert serialize({}, "body").endswith("\n")
