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

    # Polling fallback if SSE stream isn't reachable.
    poll_interval_seconds: int = 5

    @classmethod
    def from_env(cls) -> "AgentConfig":
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

        return cls(
            master_url=master_url.rstrip("/"),
            vault_dir=vault_dir,
            state_dir=state_dir,
            server_id=server_id,
            app=app,
            agent_model=os.environ.get("VAULTMCP_AGENT_MODEL", "unspecified"),
            poll_interval_seconds=int(
                os.environ.get("VAULTMCP_POLL_INTERVAL", "5")
            ),
        )
