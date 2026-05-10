"""Master configuration loaded from environment + optional YAML file.

For Phase 1+2, configuration is intentionally minimal: the DB URL and the
on-disk wiki rendering directory. Phase 3 will expand this with auth
tokens and ownership rules.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class MasterConfig:
    # Postgres connection string. Standard libpq-style URI.
    database_url: str

    # Where rendered markdown files live on master. Each `wiki.write`
    # also writes a file here, in sync with the DB transaction.
    wiki_dir: Path

    # MCP server bind. v0.2 binds localhost only (no auth yet).
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8765

    # FastAPI sidecar (healthz, metrics, future dashboard).
    http_host: str = "127.0.0.1"
    http_port: int = 8080

    # Hard cap on a single page's content size, in bytes. 1 MiB default.
    max_file_size_bytes: int = 1 * 1024 * 1024

    # Dashboard HTTP Basic auth. When ``dashboard_password`` is None,
    # the dashboard is open (relies on the localhost bind for protection).
    # When set, every dashboard route requires Basic auth matching the
    # configured username + password.
    dashboard_username: str = "admin"
    dashboard_password: str | None = None

    # Optional path to the ownership/policy YAML config. None means
    # ``/srv/vaultmcp/config.yaml`` if it exists, else no path-level
    # rules are enforced.
    ownership_config_path: Path | None = None

    # Path to the schema.sql shipped with the package; resolved at runtime.
    schema_sql_path: Path = field(
        default_factory=lambda: Path(__file__).parent / "schema.sql"
    )

    @classmethod
    def from_env(cls) -> "MasterConfig":
        """Build a config from process env. Useful for systemd EnvironmentFile."""
        database_url = os.environ.get("VAULTMCP_DATABASE_URL")
        if not database_url:
            raise RuntimeError(
                "VAULTMCP_DATABASE_URL is required (e.g. postgres://user:pass@localhost/vaultmcp)"
            )

        wiki_dir = Path(
            os.environ.get("VAULTMCP_WIKI_DIR", "/srv/vaultmcp/wiki")
        ).expanduser().resolve()

        return cls(
            database_url=database_url,
            wiki_dir=wiki_dir,
            mcp_host=os.environ.get("VAULTMCP_MCP_HOST", "127.0.0.1"),
            mcp_port=int(os.environ.get("VAULTMCP_MCP_PORT", "8765")),
            http_host=os.environ.get("VAULTMCP_HTTP_HOST", "127.0.0.1"),
            http_port=int(os.environ.get("VAULTMCP_HTTP_PORT", "8080")),
            max_file_size_bytes=int(
                os.environ.get("VAULTMCP_MAX_FILE_SIZE_BYTES", str(1 * 1024 * 1024))
            ),
            ownership_config_path=(
                Path(os.environ["VAULTMCP_CONFIG_FILE"]).expanduser().resolve()
                if "VAULTMCP_CONFIG_FILE" in os.environ
                else None
            ),
            dashboard_username=os.environ.get("VAULTMCP_DASHBOARD_USERNAME", "admin"),
            dashboard_password=os.environ.get("VAULTMCP_DASHBOARD_PASSWORD") or None,
        )
