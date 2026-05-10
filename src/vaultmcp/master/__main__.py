"""Entry point for the master server.

Usage:
    vaultmcp-master serve              # start the server
    vaultmcp-master migrate             # apply schema only and exit

Configuration via environment variables (see :mod:`vaultmcp.master.config`).
"""

from __future__ import annotations

import asyncio
import logging
import sys

import click
import uvicorn

from .config import MasterConfig
from .db import Database
from .server import build_app


@click.group()
@click.option(
    "--log-level",
    default="INFO",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
)
def cli(log_level: str) -> None:
    """vaultmcp-master — master MCP server for VaultMCP."""
    logging.basicConfig(
        level=log_level.upper(),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )


@cli.command()
def serve() -> None:
    """Start the master server (HTTP + MCP-over-HTTP + SSE)."""
    config = MasterConfig.from_env()
    app = build_app(config)
    uvicorn.run(
        app,
        host=config.http_host,
        port=config.http_port,
        log_level="info",
    )


@cli.command()
def migrate() -> None:
    """Apply the schema migration and exit."""

    async def run() -> None:
        config = MasterConfig.from_env()
        db = await Database.connect(config.database_url)
        try:
            await db.apply_schema(config.schema_sql_path)
            click.echo(f"Schema applied from {config.schema_sql_path}")
        finally:
            await db.close()

    asyncio.run(run())


def main() -> None:
    """Console-script entry point."""
    cli(standalone_mode=True)


if __name__ == "__main__":
    main()
