"""Unit tests for master.render — pure filesystem, no DB."""

from __future__ import annotations

from pathlib import Path

import pytest

from vaultmcp.master.render import remove_from_disk, render_to_disk


def test_render_creates_file_and_dirs(tmp_path: Path) -> None:
    render_to_disk(tmp_path, "apps/inventory/README.md", "# inventory\n")
    target = tmp_path / "apps" / "inventory" / "README.md"
    assert target.exists()
    assert target.read_text() == "# inventory\n"


def test_render_overwrites_atomically(tmp_path: Path) -> None:
    render_to_disk(tmp_path, "x.md", "v1\n")
    render_to_disk(tmp_path, "x.md", "v2\n")
    assert (tmp_path / "x.md").read_text() == "v2\n"


def test_render_path_traversal_blocked(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        render_to_disk(tmp_path, "../escape.md", "no")
    with pytest.raises(ValueError):
        render_to_disk(tmp_path, "/etc/passwd", "no")


def test_remove_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "x.md"
    target.write_text("hi")
    remove_from_disk(tmp_path, "x.md")
    assert not target.exists()
    # Removing again is a no-op.
    remove_from_disk(tmp_path, "x.md")
