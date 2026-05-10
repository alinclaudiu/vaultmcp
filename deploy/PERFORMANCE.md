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

## What's not in the baseline yet

- Semantic / hybrid search numbers (need an embedding provider that's
  not blocking the worker queue at bench start; will appear once the
  bench can pre-warm via the null provider).
- ext.* throughput (extension-side queries through `SET LOCAL ROLE`).
- 2-agent end-to-end propagation latency (already covered by
  `tests/test_two_agents.py` qualitatively; would need a high-cadence
  variant to surface a number).
