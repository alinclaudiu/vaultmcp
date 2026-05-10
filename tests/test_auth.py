"""Unit tests for the bearer-token auth primitives."""

from __future__ import annotations

from vaultmcp.master.auth import (
    AuthenticatedServer,
    generate_token,
    hash_token,
    parse_bearer,
)


def test_generate_token_is_unique_and_urlsafe() -> None:
    a = generate_token()
    b = generate_token()
    assert a != b
    # secrets.token_urlsafe yields ~43 chars for nbytes=32
    assert len(a) >= 32
    # Only URL-safe alphabet (alphanumeric + - + _).
    assert all(c.isalnum() or c in "-_" for c in a)


def test_hash_token_is_deterministic() -> None:
    t = "hunter2"
    assert hash_token(t) == hash_token(t)
    # SHA-256 → 64 hex chars.
    assert len(hash_token(t)) == 64
    # Different input → different hash.
    assert hash_token("hunter2") != hash_token("hunter3")


def test_parse_bearer_happy_path() -> None:
    assert parse_bearer("Bearer abc123") == "abc123"
    # Case-insensitive scheme.
    assert parse_bearer("bearer abc123") == "abc123"
    assert parse_bearer("BEARER abc123") == "abc123"


def test_parse_bearer_strips_whitespace_around_token() -> None:
    assert parse_bearer("Bearer   abc123  ") == "abc123"


def test_parse_bearer_rejects_malformed_or_missing() -> None:
    assert parse_bearer(None) is None
    assert parse_bearer("") is None
    assert parse_bearer("Bearer") is None              # no token part
    assert parse_bearer("Bearer ") is None             # empty token
    assert parse_bearer("Basic abc:def") is None       # wrong scheme
    assert parse_bearer("just-a-token") is None        # no scheme


def test_authenticated_server_is_immutable_and_hashable() -> None:
    s = AuthenticatedServer(id="server-1", apps=("webstore", "admin"))
    # frozen dataclass -> hashable, comparable
    assert s == AuthenticatedServer(id="server-1", apps=("webstore", "admin"))
    assert {s} == {s}
