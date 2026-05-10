# Performance baseline

Last measured: 2026-05-10 against `main` (commit f615fe3).

The runner lives at `bench/run.py` and boots the master in-process so
the numbers reflect VaultMCP's HTTP handler latency, not network RTT.
Single-client, sequential calls — concurrent-load characterisation
is a separate exercise.

## Method

```bash
VAULTMCP_TEST_DATABASE_URL=postgres://vaultmcp:…@localhost:5432/vaultmcp \
  python bench/run.py --iterations 300
```

- Master: `vaultmcp serve` in-process (uvicorn, lifespan="on").
- Postgres 17.8 on the same host (Unix socket via TCP/localhost).
- Hardware: dev workstation (single-socket Linux 6.12, no special
  tuning).
- Baseline page: minimal valid frontmatter + ~150 chars of body.
- Each round: drop+re-create master tables → boot master → run.

## Results — single-client, sequential

| Operation | p50 | p95 | p99 | max | throughput |
|---|---:|---:|---:|---:|---:|
| `wiki.write` | 5.6 ms | 6.4 ms | 6.7 ms | 54.0 ms | ~175 writes/s |
| `wiki.read` | 2.5 ms | 3.6 ms | 3.8 ms | 3.9 ms | ~371 reads/s |
| `wiki.search` (lexical) | 4.7 ms | 5.3 ms | 6.4 ms | 6.4 ms | ~218 searches/s |

The `max` on writes is dominated by an outlier at startup (first
write opens a connection from the pool, applies the LISTEN-loop's
notification listener, and primes the prepared-statement cache).
After that it sits at ~6 ms p95.

## What the numbers tell us

- **Writes are the slow path.** Optimistic-concurrency check, audit
  insert, event insert, embedding-jobs insert, and the on-disk
  rendering all run inside the request. ~6 ms p95 is dominated by
  Postgres + filesystem fsync cost — well within the ROADMAP target
  of "p95 under tens of milliseconds for prod loads".
- **Reads are pure indexed lookups** on `pages.path` (PK), so they
  hit the asyncpg fast path. ~370 r/s on a single client + sequential
  is far below what the pool can do in parallel.
- **Lexical search** uses the GIN index on `content_tsv` and a small
  result set; it's competitive with point reads.

## How to compare across changes

Run `bench/run.py` before + after a perf-relevant change and check
that p95 doesn't regress by more than ~10%. The bench drops + re-
creates master tables each run, so back-to-back runs on the same DB
are independent.

For multi-client throughput, run several `bench/run.py` invocations
in parallel against the same Postgres but different ports (the
script uses `_free_port()`); a real concurrent-load runner would
share the master connection pool, which lands when the project gets
a `bench/concurrent.py` companion.

## Concurrent load — 8 workers × 25 ops

Run via:

```bash
VAULTMCP_TEST_DATABASE_URL=postgres://… \
  python bench/parallel.py --concurrency 8 --writes-per-worker 25
```

| Operation | p50 | p95 | p99 | aggregate throughput |
|---|---:|---:|---:|---:|
| `wiki.write` (8×25 = 200) | 28.9 ms | 33.8 ms | 303.7 ms | ~190 writes/s |
| `wiki.read` (8×25 = 200) | 16.8 ms | 19.9 ms | 69.9 ms | ~406 reads/s |

Two observations to act on under prod load:

1. **Write throughput barely scales with concurrency.** Single-client
   ~175 w/s vs 8-client ~190 w/s — the bottleneck is the
   `SELECT … FOR UPDATE` inside `write_page`. Two writers contending
   for the same path serialise; two writers on different paths still
   share the global_version_seq nextval and the events INSERT path.
   Real-world write rate is bounded by Postgres's write-WAL throughput
   on the master + the wiki render fsync. To push beyond, the next
   step is a `synchronous_commit=off` audit on the audit/events
   inserts (acceptable trade for those rows).
2. **Read throughput also barely scales** because reads are already
   point-lookups on `pages.path` (PK). Single-client = 370 r/s,
   8-client = 406 r/s. Latency is dominated by FastAPI/uvicorn
   request handling, not Postgres. Multi-process uvicorn
   (`--workers 4`) would help on a heavily read-loaded master;
   single-process is fine for the typical "5-10 servers" profile.
3. **Tail latency on writes** (p99 = 304 ms vs p50 = 29 ms) is mostly
   the first-write contention spike — eight workers grabbing
   FOR UPDATE on cold cache. Steady-state p99 stays close to p95.

## What's not in the baseline yet

- Semantic / hybrid search numbers (need an embedding provider that's
  not blocking the worker queue at bench start; will appear once the
  bench can pre-warm via the null provider).
- ext.* throughput (extension-side queries through `SET LOCAL ROLE`).
- 2-agent end-to-end propagation latency (already covered by
  `tests/test_two_agents.py` qualitatively; would need a high-cadence
  variant to surface a number).
