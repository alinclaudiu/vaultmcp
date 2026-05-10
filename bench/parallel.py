"""Concurrent-load benchmark for the master.

Companion to ``bench/run.py``. Fires N parallel async clients against
a single in-process master and reports aggregate p50 / p95 / p99
across every operation. Useful for spotting connection-pool ceilings
and contention on hot paths (write_page's ``SELECT … FOR UPDATE``).

Usage::

    VAULTMCP_TEST_DATABASE_URL=postgres://… python bench/parallel.py
    VAULTMCP_TEST_DATABASE_URL=… python bench/parallel.py \\
        --concurrency 16 --writes-per-worker 50

The default 8 workers × 50 writes each = 400 writes total, ~30
seconds on the dev box.
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vaultmcp.agent.client import MasterClient  # noqa: E402
from vaultmcp.master.config import MasterConfig  # noqa: E402
from vaultmcp.master.server import build_app  # noqa: E402


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
    s = sorted(samples)
    n = len(s)
    return {
        "p50": s[int(n * 0.50)],
        "p95": s[int(n * 0.95)],
        "p99": s[int(n * 0.99)],
        "max": s[-1],
        "mean": statistics.fmean(s),
    }


def _fmt(stats: dict[str, float]) -> str:
    return (
        f"p50={stats['p50']*1000:7.2f}ms  "
        f"p95={stats['p95']*1000:7.2f}ms  "
        f"p99={stats['p99']*1000:7.2f}ms  "
        f"max={stats['max']*1000:7.2f}ms  "
        f"mean={stats['mean']*1000:7.2f}ms"
    )


def _make_page(worker: int, i: int) -> str:
    today = datetime.now(tz=UTC).date().isoformat()
    return (
        "---\n"
        f"title: Worker {worker} page {i}\n"
        "type: app\n"
        "owners: [bench]\n"
        f"updated: {today}\n"
        "---\n\n"
        f"# w{worker}/p{i}\n\nLorem ipsum.\n"
    )


async def _worker_writes(
    base_url: str, worker: int, count: int, samples_out: list[list[float]]
) -> None:
    """Run ``count`` writes from one worker; record per-op latency."""
    client = MasterClient(base_url)
    samples: list[float] = []
    session: dict[str, Any] = {
        "server_id": f"bench-{worker}",
        "app": "bench",
        "agent_model": "bench",
        "session_id": "",
    }
    try:
        for i in range(count):
            path = f"apps/bench/w{worker:03d}-p{i:05d}.md"
            content = _make_page(worker, i)
            t0 = time.perf_counter()
            await client.write(
                path=path, content=content, base_version=None, session=session
            )
            samples.append(time.perf_counter() - t0)
    finally:
        await client.close()
    samples_out.append(samples)


async def _worker_reads(
    base_url: str, worker: int, count: int, total_pages: int,
    samples_out: list[list[float]],
) -> None:
    client = MasterClient(base_url)
    samples: list[float] = []
    try:
        for i in range(count):
            # Round-robin across the population so workers don't
            # serialise on a single hot row.
            global_i = (worker * count + i) % total_pages
            page_w = global_i // count
            page_p = global_i % count
            path = f"apps/bench/w{page_w:03d}-p{page_p:05d}.md"
            t0 = time.perf_counter()
            await client.read(path)
            samples.append(time.perf_counter() - t0)
    finally:
        await client.close()
    samples_out.append(samples)


async def _drop_master_tables(database_url: str) -> None:
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
    tmp = Path(args.wiki_dir) if args.wiki_dir else Path("/tmp/vmcp-bench-concurrent")

    n = args.concurrency
    per = args.writes_per_worker
    total = n * per

    async with _master(dsn, tmp) as base_url:
        # ---- writes (concurrent) ----
        print(
            f"# wiki.write — {n} workers × {per} writes = {total} total"
        )
        all_writes: list[list[float]] = []
        t0 = time.perf_counter()
        await asyncio.gather(
            *(
                _worker_writes(base_url, w, per, all_writes)
                for w in range(n)
            )
        )
        wall = time.perf_counter() - t0
        flat = [x for sub in all_writes for x in sub]
        print(f"  {_fmt(_percentiles(flat))}")
        print(f"  wall: {wall:.2f}s  →  aggregate {total/wall:7.1f} writes/s")

        # ---- reads (concurrent, over the population we just wrote) ----
        print(f"\n# wiki.read — {n} workers × {per} reads = {total} total")
        all_reads: list[list[float]] = []
        t0 = time.perf_counter()
        await asyncio.gather(
            *(
                _worker_reads(base_url, w, per, total, all_reads)
                for w in range(n)
            )
        )
        wall = time.perf_counter() - t0
        flat = [x for sub in all_reads for x in sub]
        print(f"  {_fmt(_percentiles(flat))}")
        print(f"  wall: {wall:.2f}s  →  aggregate {total/wall:7.1f} reads/s")

    if not os.environ.get("VAULTMCP_BENCH_KEEP_DB"):
        await _drop_master_tables(dsn)


def main() -> None:
    p = argparse.ArgumentParser(description="Concurrent-load benchmark.")
    p.add_argument("--concurrency", "-c", type=int, default=8)
    p.add_argument("--writes-per-worker", "-w", type=int, default=50)
    p.add_argument("--wiki-dir", default=None)
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
