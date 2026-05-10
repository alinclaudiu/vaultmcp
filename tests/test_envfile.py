"""Unit tests for the env-file loader."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from vaultmcp.shared.envfile import (
    autoload_agent_env,
    default_agent_env_paths,
    maybe_load_env_file,
)


def test_maybe_load_env_file_returns_false_for_missing(tmp_path: Path) -> None:
    assert maybe_load_env_file(tmp_path / "no-such") is False


def test_maybe_load_env_file_parses_basic(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("FOO", raising=False)
    monkeypatch.delenv("BAR", raising=False)

    p = tmp_path / ".env"
    p.write_text("FOO=hello\nBAR=world\n")
    assert maybe_load_env_file(p) is True
    assert os.environ["FOO"] == "hello"
    assert os.environ["BAR"] == "world"


def test_maybe_load_env_file_skips_comments_and_blanks(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("REAL", raising=False)

    p = tmp_path / ".env"
    p.write_text("# just a comment\n\nREAL=yes\n# REAL=no\n")
    maybe_load_env_file(p)
    assert os.environ["REAL"] == "yes"


def test_maybe_load_env_file_strips_quotes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("Q1", raising=False)
    monkeypatch.delenv("Q2", raising=False)
    monkeypatch.delenv("Q3", raising=False)

    p = tmp_path / ".env"
    p.write_text("Q1='single'\nQ2=\"double\"\nQ3=no-quotes\n")
    maybe_load_env_file(p)
    assert os.environ["Q1"] == "single"
    assert os.environ["Q2"] == "double"
    assert os.environ["Q3"] == "no-quotes"


def test_maybe_load_env_file_does_not_override_existing(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("WINS", "from-process")

    p = tmp_path / ".env"
    p.write_text("WINS=from-file\n")
    maybe_load_env_file(p)
    assert os.environ["WINS"] == "from-process"


def test_maybe_load_env_file_tolerates_malformed(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    p.write_text("=missingkey\nNOT_AN_ASSIGNMENT\nFOO=ok\n")
    assert maybe_load_env_file(p) is True


def test_maybe_load_env_file_swallows_permission_errors(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    p.write_text("X=y\n")
    p.chmod(0o000)
    try:
        # Root can read regardless of mode; skip this check then.
        if os.geteuid() == 0:
            pytest.skip("running as root; mode=000 is not a barrier")
        assert maybe_load_env_file(p) is False
    finally:
        p.chmod(0o644)


def test_default_agent_env_paths_honours_explicit_override(monkeypatch) -> None:
    monkeypatch.setenv("VAULTMCP_ENV_FILE", "/tmp/whatever.env")
    paths = default_agent_env_paths()
    assert Path("/tmp/whatever.env") in paths
    # Explicit override comes first.
    assert paths[0] == Path("/tmp/whatever.env")


def test_autoload_agent_env_returns_path_on_match(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("FROM_AUTO", raising=False)

    target = tmp_path / "agent.env"
    target.write_text("FROM_AUTO=1\n")
    monkeypatch.setenv("VAULTMCP_ENV_FILE", str(target))
    found = autoload_agent_env()
    assert found == target
    assert os.environ["FROM_AUTO"] == "1"


def test_autoload_agent_env_returns_none_when_no_file(monkeypatch, tmp_path) -> None:
    # Override every default path to point inside tmp_path so we don't
    # collide with whatever the caller's real ~/.config holds.
    monkeypatch.setenv("VAULTMCP_ENV_FILE", str(tmp_path / "nope.env"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path / "elsewhere") if (tmp_path / "elsewhere").exists() else (tmp_path / "elsewhere").mkdir()
    os.chdir(tmp_path / "elsewhere")
    assert autoload_agent_env() is None
