"""Unit tests for the agent's SQLite version cache."""

from __future__ import annotations

from pathlib import Path

from vaultmcp.agent.version_cache import VersionCache


def test_set_and_get_version(tmp_path: Path) -> None:
    cache = VersionCache(tmp_path / "v.db")
    assert cache.get_version("a.md") is None
    cache.set_version("a.md", 7, 100)
    assert cache.get_version("a.md") == 7


def test_version_upsert(tmp_path: Path) -> None:
    cache = VersionCache(tmp_path / "v.db")
    cache.set_version("a.md", 1, 1)
    cache.set_version("a.md", 2, 5)
    cache.set_version("a.md", 3, 9)
    assert cache.get_version("a.md") == 3


def test_cursor_default_is_zero(tmp_path: Path) -> None:
    cache = VersionCache(tmp_path / "v.db")
    assert cache.get_cursor() == 0


def test_cursor_persists(tmp_path: Path) -> None:
    cache = VersionCache(tmp_path / "v.db")
    cache.set_cursor(42)
    # Re-open
    cache2 = VersionCache(tmp_path / "v.db")
    assert cache2.get_cursor() == 42
