"""Latency + throughput benchmark for the master.

Boots a master in-process (so the numbers reflect VaultMCP's HTTP
handler latency, not network RTT), drives writes / reads / lexical
searches against it via :class:`MasterClient`, and prints
percentiles plus throughput.

Usage::

    VAULTMCP_TEST_DATABASE_URL=postgres://… python bench/run.py
    VAULTMCP_TEST_DATABASE_URL=… python bench/run.py --iterations 2000

The script intentionally lives outside ``tests/`` so ``make test`` /
CI doesn't run it. Set ``VAULTMCP_BENCH_KEEP_DB=1`` to skip the
automatic DROP TABLE cleanup at the end (handy when you want to
inspect rows after a run).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import statistics
import sys
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
import uvicorn

# ``bench/run.py`` lives next to ``src/``; insert ``src`` on the path so
# ``import vaultmcp`` works without ``pip install -e .``.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vaultmcp.agent.client import MasterClient
from vaultmcp.master.config import MasterConfig
from vaultmcp.master.server import build_app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@asynccontextmanager
async def _master(database_url: str, tmp_path: Path):
    port = _free_port()
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir(parents=True, exist_ok=True)
    app = build_app(MasterConfig(database_url=database_url, wiki_dir=wiki_dir))
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="error", lifespan="on"
        )
    )
    task = asyncio.create_task(server.serve())
    try:
        deadline = time.monotonic() + 10
        while not getattr(server, "started", False):
            if time.monotonic() > deadline:
                raise RuntimeError("master never started")
            await asyncio.sleep(0.05)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=5)
        except TimeoutError:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


def _percentiles(samples: list[float]) -> dict[str, float]:
    samples = sorted(samples)
    n = len(samples)
    return {
        "p50": samples[int(n * 0.50)],
        "p95": samples[int(n * 0.95)],
        "p99": samples[int(n * 0.99)],
        "max": samples[-1],
        "mean": statistics.fmean(samples),
    }


def _fmt(stats: dict[str, float]) -> str:
    return (
        f"p50={stats['p50']*1000:7.2f}ms  "
        f"p95={stats['p95']*1000:7.2f}ms  "
        f"p99={stats['p99']*1000:7.2f}ms  "
        f"max={stats['max']*1000:7.2f}ms  "
        f"mean={stats['mean']*1000:7.2f}ms"
    )


def _make_page(i: int) -> str:
    today = datetime.now(tz=UTC).date().isoformat()
    return (
        "---\n"
        f"title: Bench page {i}\n"
        "type: app\n"
        "owners: [bench]\n"
        f"updated: {today}\n"
        "---\n\n"
        f"# Page {i}\n\n"
        f"Lorem ipsum dolor sit amet, consectetur adipiscing elit. "
        f"Page index is {i}; benchmarking p95 latency of master writes.\n"
    )


SESSION: dict[str, Any] = {
    "server_id": "bench",
    "app": "bench",
    "agent_model": "bench",
    "session_id": "",
}


async def _bench_writes(client: MasterClient, n: int) -> list[float]:
    samples: list[float] = []
    for i in range(n):
        page = _make_page(i)
        path = f"apps/bench/page-{i:05d}.md"
        t0 = time.perf_counter()
        await client.write(
            path=path, content=page, base_version=None, session=SESSION
        )
        samples.append(time.perf_counter() - t0)
    return samples


async def _bench_reads(client: MasterClient, n: int, total_pages: int) -> list[float]:
    samples: list[float] = []
    for i in range(n):
        path = f"apps/bench/page-{i % total_pages:05d}.md"
        t0 = time.perf_counter()
        await client.read(path)
        samples.append(time.perf_counter() - t0)
    return samples


async def _bench_searches(client: MasterClient, n: int) -> list[float]:
    samples: list[float] = []
    queries = ["lorem", "page index", "p95 latency", "consectetur"]
    for i in range(n):
        q = queries[i % len(queries)]
        t0 = time.perf_counter()
        await client._call(  # type: ignore[attr-defined]
            "wiki.search", {"query": q, "limit": 20}
        )
        samples.append(time.perf_counter() - t0)
    return samples


async def _drop_master_tables(database_url: str) -> None:
    """Mirror tests/conftest.py's clean_database, sans extension roles."""
    tables = (
        "embedding_jobs",
        "embeddings",
        "audit",
        "events",
        "subscriptions",
        "log_entries",
        "pages",
        "servers",
        "extensions",
    )
    conn = await asyncpg.connect(database_url)
    try:
        ext_tables = [
            r["tablename"]
            for r in await conn.fetch(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname='public' AND tablename LIKE 'ext_%'"
            )
        ]
        for tbl in ext_tables:
            await conn.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")
        for tbl in tables:
            await conn.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")
        await conn.execute("DROP SEQUENCE IF EXISTS global_version_seq CASCADE")
    finally:
        await conn.close()


async def run(args: argparse.Namespace) -> None:
    dsn = os.environ.get("VAULTMCP_TEST_DATABASE_URL")
    if not dsn:
        print(
            "VAULTMCP_TEST_DATABASE_URL must point at a Postgres for benchmarking.",
            file=sys.stderr,
        )
        sys.exit(2)

    await _drop_master_tables(dsn)
    tmp = Path(args.wiki_dir) if args.wiki_dir else Path("/tmp/vmcp-bench")

    async with _master(dsn, tmp) as base_url:
        client = MasterClient(base_url)
        try:
            print(f"# wiki.write — {args.iterations} iterations")
            t0 = time.perf_counter()
            write_samples = await _bench_writes(client, args.iterations)
            write_throughput = args.iterations / (time.perf_counter() - t0)
            print(f"  {_fmt(_percentiles(write_samples))}")
            print(f"  throughput: {write_throughput:7.1f} writes/s")

            print(f"\n# wiki.read — {args.iterations} iterations")
            t0 = time.perf_counter()
            read_samples = await _bench_reads(
                client, args.iterations, total_pages=args.iterations
            )
            read_throughput = args.iterations / (time.perf_counter() - t0)
            print(f"  {_fmt(_percentiles(read_samples))}")
            print(f"  throughput: {read_throughput:7.1f} reads/s")

            print(f"\n# wiki.search — {args.iterations // 4} iterations")
            n_search = max(args.iterations // 4, 50)
            t0 = time.perf_counter()
            search_samples = await _bench_searches(client, n_search)
            search_throughput = n_search / (time.perf_counter() - t0)
            print(f"  {_fmt(_percentiles(search_samples))}")
            print(f"  throughput: {search_throughput:7.1f} searches/s")
        finally:
            await client.close()

    if not os.environ.get("VAULTMCP_BENCH_KEEP_DB"):
        await _drop_master_tables(dsn)


def main() -> None:
    p = argparse.ArgumentParser(
        description="Latency + throughput benchmark for the master."
    )
    p.add_argument(
        "--iterations",
        "-n",
        type=int,
        default=500,
        help="Number of write/read iterations (search runs at /4 of this).",
    )
    p.add_argument(
        "--wiki-dir",
        default=None,
        help="Where the master renders pages. Default: /tmp/vmcp-bench/.",
    )
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
