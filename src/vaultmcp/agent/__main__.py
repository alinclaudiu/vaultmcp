"""Entry point for the agent.

Usage:
    vaultmcp-agent run         # start watcher + sync (long-running)
    vaultmcp-agent status      # show connection state, queue depth, cache size
    vaultmcp-agent verify      # walk local vault and compare with master

Configuration via environment (see :mod:`vaultmcp.agent.config`).
"""

from __future__ import annotations

import asyncio
import logging
import sys

import click

from .client import MasterClient, MasterClientError
from .config import AgentConfig
from .queue import WriteQueue
from .sync import SyncEngine
from .version_cache import VersionCache
from .watcher import FileWatcher


@click.group()
@click.option(
    "--log-level",
    default="INFO",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
)
def cli(log_level: str) -> None:
    """vaultmcp-agent — per-server agent for VaultMCP."""
    logging.basicConfig(
        level=log_level.upper(),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )


@cli.command()
def run() -> None:
    """Start the watcher + sync loops (foreground; intended for systemd)."""

    async def _main() -> None:
        config = AgentConfig.from_env()
        cache = VersionCache(config.state_dir / "version-cache.db")
        queue = WriteQueue(config.state_dir / "queue")
        client = MasterClient(config.master_url)

        watcher = FileWatcher(config, client, cache, queue)
        sync = SyncEngine(client=client, cache=cache, vault_dir=config.vault_dir)

        try:
            await asyncio.gather(watcher.run(), sync.run())
        finally:
            await client.close()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        click.echo("Stopped.")


@cli.command()
def status() -> None:
    """Print connection state, queue depth, cache size."""

    async def _main() -> None:
        config = AgentConfig.from_env()
        cache = VersionCache(config.state_dir / "version-cache.db")
        queue = WriteQueue(config.state_dir / "queue")
        client = MasterClient(config.master_url)

        click.echo(f"vault_dir:     {config.vault_dir}")
        click.echo(f"state_dir:     {config.state_dir}")
        click.echo(f"master_url:    {config.master_url}")
        click.echo(f"server_id:     {config.server_id}")
        click.echo(f"app:           {config.app}")
        click.echo(f"queue depth:   {queue.depth()}")
        click.echo(f"cursor (gv):   {cache.get_cursor()}")
        try:
            await client.list_pages(limit=1)
            click.echo("master:        reachable")
        except MasterClientError as exc:
            click.echo(f"master:        UNREACHABLE ({exc})")
        finally:
            await client.close()

    asyncio.run(_main())


@cli.command()
@click.option("--prefix", default=None, help="Restrict to a path prefix")
def verify(prefix: str | None) -> None:
    """Compare local vault with master; report drift but do nothing about it."""

    async def _main() -> None:
        config = AgentConfig.from_env()
        client = MasterClient(config.master_url)

        try:
            page_paths: set[str] = set()
            cursor: str | None = None
            while True:
                resp = await client.list_pages(prefix=prefix, cursor=cursor, limit=500)
                for entry in resp["entries"]:
                    page_paths.add(entry["path"])
                cursor = resp.get("next_cursor")
                if not cursor:
                    break

            local_paths: set[str] = set()
            for fp in config.vault_dir.rglob("*.md"):
                rel = fp.relative_to(config.vault_dir).as_posix()
                if prefix and not rel.startswith(prefix):
                    continue
                local_paths.add(rel)

            only_local = sorted(local_paths - page_paths)
            only_master = sorted(page_paths - local_paths)

            if not only_local and not only_master:
                click.echo("In sync with master.")
                return

            if only_local:
                click.echo(f"Only local ({len(only_local)}):")
                for p in only_local:
                    click.echo(f"  + {p}")
            if only_master:
                click.echo(f"Only on master ({len(only_master)}):")
                for p in only_master:
                    click.echo(f"  - {p}")
            sys.exit(1)
        finally:
            await client.close()

    asyncio.run(_main())


def main() -> None:
    """Console-script entry point."""
    cli(standalone_mode=True)


if __name__ == "__main__":
    main()
