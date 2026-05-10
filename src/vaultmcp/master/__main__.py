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

from .auth import generate_token, hash_token
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
        # Graceful-shutdown backstop: SSE clients are long-lived. The
        # broadcaster's per-subscriber poll already exits cleanly on
        # ``_stopping``, but we keep this short timeout as belt + braces
        # so a misbehaving client cannot keep systemctl stop hung.
        timeout_graceful_shutdown=5,
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


@cli.command(name="mcp-stdio")
def mcp_stdio_cmd() -> None:
    """Speak the official MCP protocol over stdin/stdout.

    Spawn this command from any MCP-aware client (Claude Desktop, an
    IDE plugin, …) to expose the master's wiki tools natively. Unlike
    ``serve``, this doesn't open an HTTP listener — it talks JSON-RPC
    framed by the MCP SDK directly on the inherited stdio.
    """
    from .mcp_adapter import serve_stdio

    asyncio.run(serve_stdio())


@cli.command(name="render-all")
@click.option(
    "--prefix",
    default=None,
    help="Only re-render pages whose path starts with this prefix.",
)
def render_all_cmd(prefix: str | None) -> None:
    """Re-render every page in the DB to ``$VAULTMCP_WIKI_DIR``.

    The wiki directory is a projection of the ``pages`` table; the
    master keeps it in sync on every write. After a restore from
    ``pg_dump`` (or after manually wiping the directory), this command
    rebuilds the projection without any agent activity.
    """
    from .render import render_to_disk

    async def run() -> None:
        config = MasterConfig.from_env()
        db = await Database.connect(config.database_url)
        rendered = 0
        failed = 0
        try:
            config.wiki_dir.mkdir(parents=True, exist_ok=True)
            async for page in db.iter_all_pages(prefix=prefix):
                try:
                    render_to_disk(config.wiki_dir, page.path, page.content)
                    rendered += 1
                except (OSError, ValueError) as exc:
                    failed += 1
                    click.echo(f"  ! {page.path}: {exc}", err=True)
        finally:
            await db.close()
        click.echo(
            f"Rendered {rendered} page(s) to {config.wiki_dir}"
            + (f"; {failed} failed" if failed else "")
        )
        if failed:
            sys.exit(1)

    asyncio.run(run())


@cli.command(name="add-server")
@click.option("--id", "server_id", required=True, help="Server identifier (must be unique).")
@click.option(
    "--apps",
    required=True,
    help="Comma-separated list of apps this server is allowed to write on behalf of.",
)
def add_server_cmd(server_id: str, apps: str) -> None:
    """Register a new server and print its bearer token (shown once)."""

    apps_list = [a.strip() for a in apps.split(",") if a.strip()]
    if not apps_list:
        click.echo("--apps must contain at least one app name.", err=True)
        sys.exit(2)

    async def run() -> None:
        config = MasterConfig.from_env()
        db = await Database.connect(config.database_url)
        try:
            token = generate_token()
            inserted = await db.add_server(
                server_id=server_id, apps=apps_list, token_hash=hash_token(token)
            )
            if not inserted:
                click.echo(f"Server {server_id!r} already exists.", err=True)
                sys.exit(1)
            click.echo(f"Server {server_id!r} registered for apps {apps_list}.")
            click.echo("")
            click.echo("Token (shown ONCE — store it now in the agent's VAULTMCP_TOKEN):")
            click.echo(f"  {token}")
        finally:
            await db.close()

    asyncio.run(run())


@cli.command(name="rotate-token")
@click.option("--id", "server_id", required=True, help="Server identifier.")
def rotate_token_cmd(server_id: str) -> None:
    """Generate a new token for an existing server (old token stops working)."""

    async def run() -> None:
        config = MasterConfig.from_env()
        db = await Database.connect(config.database_url)
        try:
            token = generate_token()
            updated = await db.update_server_token(
                server_id=server_id, token_hash=hash_token(token)
            )
            if not updated:
                click.echo(f"No server with id={server_id!r}.", err=True)
                sys.exit(1)
            click.echo(f"Token rotated for {server_id!r}.")
            click.echo("")
            click.echo("New token (shown ONCE):")
            click.echo(f"  {token}")
        finally:
            await db.close()

    asyncio.run(run())


@cli.command(name="remove-server")
@click.option("--id", "server_id", required=True, help="Server identifier.")
@click.confirmation_option(prompt="Remove this server? Its token will stop working.")
def remove_server_cmd(server_id: str) -> None:
    """Delete a server. Its token can no longer authenticate."""

    async def run() -> None:
        config = MasterConfig.from_env()
        db = await Database.connect(config.database_url)
        try:
            removed = await db.remove_server(server_id=server_id)
            if not removed:
                click.echo(f"No server with id={server_id!r}.", err=True)
                sys.exit(1)
            click.echo(f"Server {server_id!r} removed.")
        finally:
            await db.close()

    asyncio.run(run())


@cli.command(name="list-servers")
def list_servers_cmd() -> None:
    """Print registered servers (id, apps, last token rotation)."""

    async def run() -> None:
        config = MasterConfig.from_env()
        db = await Database.connect(config.database_url)
        try:
            servers = await db.list_servers()
            if not servers:
                click.echo("(no servers registered — auth is currently bypassed)")
                return
            for sid, apps, rotated in servers:
                rotated_str = rotated.isoformat() if rotated else "never"
                click.echo(f"{sid}\tapps={apps}\trotated={rotated_str}")
        finally:
            await db.close()

    asyncio.run(run())


def main() -> None:
    """Console-script entry point."""
    cli(standalone_mode=True)


if __name__ == "__main__":
    main()
