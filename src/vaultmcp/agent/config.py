"""Agent configuration loaded from environment + optional YAML.

Minimal for v0.2: master URL, vault path, server identity. Phase 3 adds
the bearer token. For now, the agent's identity is a free-form string.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AgentConfig:
    # The master's HTTP base URL, e.g. "http://127.0.0.1:8080"
    master_url: str

    # Where the local wiki mirror lives (will be watched).
    vault_dir: Path

    # Where the agent stores its state: token, version cache, queue.
    state_dir: Path

    # The agent's identity for audit / authorization.
    server_id: str
    app: str
    agent_model: str = "unspecified"

    # Bearer token for master auth. None when running against an
    # un-configured master (no servers registered).
    token: str | None = None

    # Polling fallback if SSE stream isn't reachable.
    poll_interval_seconds: int = 5

    @classmethod
    def from_env(cls) -> "AgentConfig":
        # When invoked from a shell (not systemd's EnvironmentFile),
        # try to source ``~/.config/vaultmcp/agent.env`` so commands
        # like ``vaultmcp-agent status`` work without manual ``source``.
        # systemd-launched runs already have env populated; setdefault
        # in the loader keeps them as-is.
        from ..shared.envfile import autoload_agent_env
        autoload_agent_env()

        master_url = os.environ.get("VAULTMCP_MASTER_URL")
        if not master_url:
            raise RuntimeError("VAULTMCP_MASTER_URL is required")

        server_id = os.environ.get("VAULTMCP_SERVER_ID")
        if not server_id:
            raise RuntimeError("VAULTMCP_SERVER_ID is required")

        app = os.environ.get("VAULTMCP_APP")
        if not app:
            raise RuntimeError("VAULTMCP_APP is required")

        vault_dir = Path(
            os.environ.get("VAULTMCP_VAULT_DIR", str(Path.home() / "vault"))
        ).expanduser().resolve()
        state_dir = Path(
            os.environ.get(
                "VAULTMCP_STATE_DIR", str(Path.home() / ".vaultmcp")
            )
        ).expanduser().resolve()

        # Token: prefer VAULTMCP_TOKEN env var. Fall back to
        # ``<state_dir>/token`` (per DESIGN.md §3.5 and §7.1) so
        # systemd units can keep the secret out of EnvironmentFile.
        token = os.environ.get("VAULTMCP_TOKEN")
        if not token:
            token_file = state_dir / "token"
            if token_file.is_file():
                token = token_file.read_text(encoding="utf-8").strip() or None

        return cls(
            master_url=master_url.rstrip("/"),
            vault_dir=vault_dir,
            state_dir=state_dir,
            server_id=server_id,
            app=app,
            token=token,
            agent_model=os.environ.get("VAULTMCP_AGENT_MODEL", "unspecified"),
            poll_interval_seconds=int(
                os.environ.get("VAULTMCP_POLL_INTERVAL", "5")
            ),
        )
