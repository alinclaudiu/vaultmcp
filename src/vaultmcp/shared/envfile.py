"""Minimal env-file loader for CLI ergonomics.

Both ``vaultmcp-master`` and ``vaultmcp-agent`` are configured through
process-environment variables. Under systemd that's the
``EnvironmentFile`` directive and everything Just Works. Run from a
shell, however, and an operator typing ``vaultmcp-agent status`` gets
``RuntimeError: VAULTMCP_MASTER_URL is required`` even when the
correct env file sits next to the user's other config.

This module pulls a key=value file into ``os.environ`` *without*
overriding values already set by the parent process. Same precedence
rule systemd uses, so behavior is consistent between the two
invocation modes.

Format: very plain — ``KEY=VALUE`` per line, ``#`` comments, blank
lines OK, single/double quotes around the value are stripped. No
shell expansion, no exports, no continuation lines. Anything fancier
belongs in a real config file (Phase 3+ already accepts a YAML
config; the env file is the systemd-friendly cousin).
"""

from __future__ import annotations

import os
from pathlib import Path


def maybe_load_env_file(path: Path | str | None) -> bool:
    """Read ``KEY=VALUE`` lines from ``path`` into ``os.environ``.

    Returns True iff the file was opened and parsed (even if empty).
    Already-set environment variables win — systemd-launched runs see
    no change because their env is populated before the CLI starts.

    Failures (missing file, permission denied, malformed line) are
    swallowed: this is a convenience layer, not a safety boundary.
    """
    if path is None:
        return False
    p = Path(path)
    try:
        content = p.read_text(encoding="utf-8")
    except (OSError, PermissionError):
        return False

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        # Strip a single matched pair of surrounding quotes.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)
    return True


def default_agent_env_paths() -> list[Path]:
    """Locations the agent CLI inspects when env isn't pre-populated.

    Order: explicit ``VAULTMCP_ENV_FILE``, XDG config, classic
    ``~/.config``, then ``./agent.env`` for repo-root development.
    """
    paths: list[Path] = []
    explicit = os.environ.get("VAULTMCP_ENV_FILE")
    if explicit:
        paths.append(Path(explicit))
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        paths.append(Path(xdg) / "vaultmcp" / "agent.env")
    paths.append(Path.home() / ".config" / "vaultmcp" / "agent.env")
    paths.append(Path.cwd() / "agent.env")
    return paths


def default_master_env_paths() -> list[Path]:
    """Locations the master CLI inspects when env isn't pre-populated."""
    paths: list[Path] = []
    explicit = os.environ.get("VAULTMCP_ENV_FILE")
    if explicit:
        paths.append(Path(explicit))
    paths.append(Path("/srv/vaultmcp/master.env"))
    paths.append(Path.cwd() / "master.env")
    return paths


def autoload_agent_env() -> Path | None:
    """First-match wins; returns the path that was loaded, or None."""
    for p in default_agent_env_paths():
        if maybe_load_env_file(p):
            return p
    return None


def autoload_master_env() -> Path | None:
    for p in default_master_env_paths():
        if maybe_load_env_file(p):
            return p
    return None
