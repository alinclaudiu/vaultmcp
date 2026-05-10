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
from pathlib import Path

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


@cli.command(name="ingest")
@click.option(
    "--from",
    "src_dir",
    required=True,
    type=click.Path(
        exists=True, file_okay=False, dir_okay=True, readable=True, path_type=Path
    ),
    help="Root of the source markdown tree (typically a VaultMesh repo).",
)
@click.option(
    "--server-id",
    default="ingest",
    help="server_id recorded on every wiki.write the import performs.",
)
@click.option(
    "--app",
    "app_override",
    default=None,
    help="Override the auto-derived app for every page. Default: first "
    "segment of the path (e.g. apps/webstore/x.md -> 'webstore').",
)
@click.option(
    "--overwrite/--skip-existing",
    default=False,
    help="What to do when the target page already exists. Default: skip.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Walk + validate only; no writes.",
)
@click.option(
    "--prefix",
    default=None,
    help="Only ingest files whose relative path starts with this prefix.",
)
def ingest_cmd(
    src_dir: Path,
    server_id: str,
    app_override: str | None,
    overwrite: bool,
    dry_run: bool,
    prefix: str | None,
) -> None:
    """Bulk-import a VaultMesh-style markdown tree into the master.

    Each ``*.md`` file under SRC_DIR becomes a wiki page whose path is
    the file's path relative to SRC_DIR (e.g.
    ``apps/webstore/notes.md``). Files that already exist on master
    are skipped by default; pass ``--overwrite`` to bump them.

    The import is an *operator* action and intentionally bypasses both
    the bearer-token auth and the path-level ownership rules — the
    operator running this command is the trust boundary, not an
    authenticated agent. Validation (size, encoding, frontmatter,
    secret patterns) still runs; files that fail are skipped and
    listed at the end so you can fix the source and re-run.

    Run separately per source repo with the appropriate ``--server-id``
    when migrating multi-repo VaultMesh layouts (each origin server
    appears in audit rows under its own id).
    """
    from ..shared.frontmatter import parse as fm_parse
    from ..shared.types import Session, WriteInput
    from .errors import SizeLimitExceeded, ValidationFailed
    from .render import render_to_disk
    from .tools import handle_write

    def _derive_app(rel: str) -> str:
        if app_override:
            return app_override
        parts = rel.split("/")
        if len(parts) >= 2 and parts[0] == "apps":
            return parts[1]
        if parts and parts[0]:
            return parts[0]
        return "shared"

    async def run() -> None:
        config = MasterConfig.from_env()
        db = await Database.connect(config.database_url)
        try:
            config.wiki_dir.mkdir(parents=True, exist_ok=True)
            md_files = sorted(src_dir.rglob("*.md"))
            if prefix:
                md_files = [
                    p
                    for p in md_files
                    if p.relative_to(src_dir).as_posix().startswith(prefix)
                ]
            click.echo(
                f"Found {len(md_files)} *.md file(s) under {src_dir}"
                + (f" (prefix={prefix!r})" if prefix else "")
            )

            counts = {"imported": 0, "skipped_existing": 0, "skipped_invalid": 0}
            invalid: list[tuple[Path, str]] = []

            for fp in md_files:
                rel = fp.relative_to(src_dir).as_posix()
                content = fp.read_text(encoding="utf-8")

                # Skip if the page already exists and the operator
                # didn't ask for an overwrite. Cheaper than validating.
                existing = await db.get_page(rel)
                if existing is not None and not overwrite:
                    counts["skipped_existing"] += 1
                    continue

                if dry_run:
                    # Validate without committing so the report is accurate.
                    try:
                        fm_parse(content)
                    except Exception as exc:
                        invalid.append((fp, str(exc)))
                        counts["skipped_invalid"] += 1
                        continue
                    counts["imported"] += 1
                    continue

                session = Session(
                    server_id=server_id,
                    app=_derive_app(rel),
                    agent_model="ingest",
                    session_id="",
                )
                inp = WriteInput(
                    path=rel,
                    content=content,
                    base_version=(existing.version if existing else None),
                    session=session,
                )
                try:
                    # Bypass auth + ownership; the operator is the trust
                    # boundary on this code path.
                    await handle_write(
                        db,
                        config.wiki_dir,
                        inp,
                        authed=None,
                        max_bytes=config.max_file_size_bytes,
                        ownership=None,
                    )
                    counts["imported"] += 1
                except (ValidationFailed, SizeLimitExceeded) as exc:
                    invalid.append((fp, str(exc)))
                    counts["skipped_invalid"] += 1
                    continue

            click.echo(
                f"Imported   : {counts['imported']}"
                + ("  (dry-run)" if dry_run else "")
            )
            click.echo(f"Skipped existing: {counts['skipped_existing']}")
            click.echo(f"Skipped invalid : {counts['skipped_invalid']}")
            if invalid:
                click.echo("\nFiles with validation problems:")
                for path, reason in invalid[:30]:
                    short = reason if len(reason) <= 100 else reason[:97] + "..."
                    click.echo(f"  ! {path.relative_to(src_dir)}: {short}")
                if len(invalid) > 30:
                    click.echo(f"  ... and {len(invalid) - 30} more.")
                # Also re-render every successfully-imported page so the
                # on-disk projection matches the DB after a bulk import.
                # (handle_write already renders on each call; this is a
                # belt-and-braces step for cases where the renderer
                # short-circuited because content was unchanged.)
            if not dry_run and counts["imported"] > 0:
                rendered = 0
                async for page in db.iter_all_pages():
                    try:
                        render_to_disk(config.wiki_dir, page.path, page.content)
                        rendered += 1
                    except (OSError, ValueError):
                        pass
                click.echo(f"Re-rendered {rendered} page(s) to {config.wiki_dir}.")
        finally:
            await db.close()

    asyncio.run(run())


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
