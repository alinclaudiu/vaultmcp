# Changelog

All notable changes to VaultMCP are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/).

## [0.7.0] — 2026-05-10

**MCP-native release.** Closes the "MCP-first project that doesn't
fully speak MCP" gap that's been carried in the README and DESIGN.md
since v0.2. All other changes are documentation alignment + a code
cleanup pass.

### Added

- **`/mcp/streamable` HTTP endpoint** — official MCP-over-HTTP via
  the `mcp` SDK's `StreamableHTTPSessionManager`. Off-the-shelf MCP
  clients (Claude Desktop, IDE plugins, MCP HTTP clients) can dial
  it directly without spawning a subprocess. Stateless, JSON
  responses, mounted as a raw ASGI route alongside the legacy
  `/mcp/call` shim. Same six `wiki.*` tools as the stdio transport.

### Changed

- **`ruff check src tests` is now fully green.** 26 pre-existing
  warnings cleared (`UP037`, `UP017`, `UP035`, `I001`, `F401`,
  `E402`, `N812` auto-fixed). FastAPI's `Depends(...)` /
  `Body(...)` argument-default idiom and the broadcaster's
  fire-and-forget `asyncio.create_task` calls are pinned via
  `[tool.ruff.lint.per-file-ignores]` rather than scattered
  `# noqa` comments.
- **README** — new "Wiring an MCP-aware client" section under
  Quick start with a concrete Claude Desktop config snippet.
- **CLAUDE.md** rewritten to reflect v0.6.1+ reality (was still
  saying "v0.2 skeleton"). New "What's already in" section so a
  fresh Claude Code session in the repo doesn't try to rebuild
  what already shipped.
- **ROADMAP.md** — every checkbox under v0.1 → v0.6 is now [x],
  with the concrete deliverables enumerated.

## [0.6.1] — 2026-05-10

Polish + first-deploy fixes on top of v0.6.0. No breaking changes.

### Added

- **`vaultmcp-master mcp-stdio`** — official MCP protocol over
  stdin/stdout via the `mcp` Python SDK. Any MCP-aware client
  (Claude Desktop, IDE plugins) can now spawn the master as a
  subprocess and talk natively. Six tools registered (`wiki.read`,
  `list`, `search`, `audit`, `write`, `append_log`); `ext.*` stays
  on the auth'd HTTP path.
- **`vaultmcp-master render-all [--prefix ...]`** — rebuild the
  on-disk wiki projection from the DB after a `pg_restore`.
  Streams pages in path order so a wiki with hundreds of thousands
  of entries doesn't materialise at once.
- **`ext.deregister`** — closes the extension lifecycle. Drops every
  `ext_<name>_*` table, the namespaced pages (cascades to
  `embeddings` + `embedding_jobs`), the Postgres role, and the
  `extensions` row. Idempotent.
- **`/metrics` Prometheus endpoint** — gauges for pages, embeddings,
  embedding-job queue depth (pending/failed), audit by outcome,
  servers, extensions, plus `build_info{version=...}`. No external
  prometheus_client dep.
- **`bench/parallel.py`** — concurrent-load runner. Fires N async
  workers in parallel, reports aggregate p50/p95/p99. Numbers in
  `deploy/PERFORMANCE.md` (8×25 ops: writes p95 33.8ms ~190/s
  aggregate, reads p95 19.9ms ~406/s aggregate).
- **Auto-source env files** — when invoked from a shell (not
  systemd), `vaultmcp-master` and `vaultmcp-agent` now look for
  `~/.config/vaultmcp/agent.env` and `/srv/vaultmcp/master.env`
  before raising. Setdefault means systemd-launched runs still win.

### Fixed

- **Graceful shutdown deadlock with active SSE subscribers**
  (`d4cfe05`). `systemctl stop vaultmcp-master` was hanging the
  full 90 s `TimeoutStopSec` while a remote agent had a
  `/mcp/subscribe` connection open. uvicorn was parked waiting for
  the request, the request was parked in `await sub.queue.get()`,
  and `broadcaster.stop()` (which would have woken it) only runs
  in lifespan `__aexit__` — *after* uvicorn finishes the wait.
  Fix: subscribers now poll the queue with a 1s `wait_for` and
  check `_stopping` themselves; uvicorn `timeout_graceful_shutdown=5`
  as backstop. Stop time went from 90s+SIGKILL to 0.16s.
- **`PermissionError` in `load_rules('/srv/vaultmcp/config.yaml')`**
  when called from a user other than `vaultmcp` (tests, one-off
  CLIs). `Path.is_file()` raised on the protected directory; now
  caught and treated as "no config file present".

### Operator notes

- `bench/concurrent.py` was renamed to `bench/parallel.py` because
  the former shadows stdlib `concurrent` (which asyncio imports at
  startup) and broke the entire async runtime when invoked from
  the `bench/` directory.

## [0.6.0] — 2026-05-10

**Hardening phase.** Closes the v0.5/v0.6 ROADMAP slate: real
Postgres-role isolation for extensions, the rest of the `ext.*`
surface (write + embed + emit_event), backup/restore runbook, and
a perf baseline.

### Added

- **`ext.exec`** — mutating sibling of `ext.query`. Read-write
  transaction under the extension's role; rows_affected returned.
- **`ext.embed`** — extensions index their own content into the
  shared `embeddings` table. Path is forced into the `ext/<name>/…`
  namespace; the embedding worker drains the queue exactly like for
  `wiki.write`.
- **`ext.emit_event`** — extensions push live events into the
  shared `events` feed; `event_type` is namespaced to
  `ext_<name>_<type>` so the dashboard's activity page surfaces
  them alongside `PageChanged`.
- **`bench/run.py`** — single-client latency + throughput
  benchmark. Captures p50/p95/p99 + throughput for write/read/search.
- **`deploy/BACKUP.md`** — full backup/restore runbook (Postgres
  custom-format dump, wiki tar, daily script with retention,
  verification path, reverse-proxy / TLS guidance).
- **`deploy/PERFORMANCE.md`** — baseline numbers on the dev box.

### Changed — security

- **Postgres-role isolation** for extensions (deferred from v0.5).
  Each `ext.register` now provisions a NOLOGIN
  `vaultmcp_ext_<name>` role and grants it precisely what the
  policy declares: `USAGE` on `public`, optional `SELECT` on
  `pages` / `audit`, optional DML on `embeddings` + `events` +
  the relevant sequences. `ext.query` / `ext.exec` issue
  `SET LOCAL ROLE` so the role auto-resets at transaction end —
  the role's GRANTs become the actual security boundary.
- Three layers of defense for `ext.query`:
  1. Lexical reject of write keywords at the tools layer.
  2. `READ ONLY` transaction at the DB layer.
  3. Postgres role-level GRANTs scoped by policy.
- `extensions.role_name TEXT NOT NULL UNIQUE` added to the schema.
- Test fixture `clean_database` now also drops `vaultmcp_ext_*`
  roles, with a defensive `GRANT … TO CURRENT_USER` so
  `DROP OWNED BY` works even when a previous test crashed before
  the registration's own grant landed.

### Operator step

- `vaultmcp` DB role needs `CREATEROLE`. Documented in
  `deploy/README.md` alongside the existing `CREATE EXTENSION
  vector` step. One-time superuser command:
  ```bash
  sudo -u postgres psql -c "ALTER ROLE vaultmcp WITH CREATEROLE;"
  ```

## [0.5.0] — 2026-05-10

**Extensions phase.** Master now hosts other applications on the same
DB per `docs/03-extensibility.md` and ROADMAP v0.5.

### Added — `ext.*` MCP tools

- **`ext.register`** — create an extension entry. Validates name +
  schema_prefix (default `ext_<name>_`); persists owners + policy.
- **`ext.list`** — enumerate registered extensions (no policy details).
- **`ext.declare_table`** — translate a column-spec list into a
  `CREATE TABLE IF NOT EXISTS ext_<name>_<table>`. Type whitelist
  (text/int/bigint/uuid/timestamptz/jsonb/numeric/...), per-column
  primary_key / not_null / default / references.
- **`ext.query`** — run a SELECT against the extension's tables (and
  any core tables the policy grants). Two layers of defense:
  - lexical reject of write keywords (`INSERT`, `UPDATE`, `DELETE`,
    `DROP`, `ALTER`, `TRUNCATE`, `GRANT`, `CREATE`, `DO`, …) up
    front, with a clear error;
  - Postgres `READ ONLY` transaction at the DB layer so even a
    bypass cannot mutate state.

### Schema

- `extensions(name PK, schema_prefix UNIQUE, owners[], policy JSONB,
  schema_version, created_at)`.

### Tests

- 21 unit tests covering DDL helpers, name validation, write-keyword
  rejection (parametrised over the mutation catalogue).
- 2 e2e tests walking the full worked-example flow from
  `docs/03-extensibility.md`: register a CRM extension, declare a
  contacts table, INSERT, run `ext.query` with a JOIN against
  `pages`, verify the mutation guard refuses `DELETE`.

### Deferred to v0.6 hardening

Schema-prefix isolation via real Postgres roles + `SET ROLE` per query
is deliberately not in this release. The application-layer prefix
enforcement plus the `READ ONLY` transaction is enough for trusted
authenticated extensions; full role isolation requires `CREATEROLE`
on the master DB role and a documented operator setup step.

## [0.4.1] — 2026-05-10

Polish on top of `v0.4.0`. The biggest user-facing change is that the
default embedding dimension drops from 1536 to 1024 — see the schema
migration note below if you're upgrading.

### Added

- **OpenAI-compatible embedding provider**
  (`vaultmcp.master.embeddings.OpenAICompatibleEmbeddingProvider`).
  POSTs to `/embeddings` on a configurable base URL with the OpenAI
  request shape, validates dimension. Same code talks to OpenAI,
  litellm proxy, vLLM with `--embeddings`, LocalAI, etc. Configure
  via `VAULTMCP_EMBEDDING_PROVIDER=openai-compat` plus
  `VAULTMCP_EMBEDDING_BASE_URL` / `_API_KEY` / `_MODEL`.
- **SSE-driven live activity** on the dashboard. `/dashboard/sse-events`
  forwards `EventBroadcaster` notifications as Server-Sent Events; the
  activity table re-fetches on each. A 10 s polling fallback covers
  closed connections (proxy idle, laptop sleep).
- **Worker state** surfaced on the `/servers` dashboard: provider
  name, embedded pages, pending / failed job counts, last error.

### Changed

- **Default embedding dimension is now 1024** (from 1536). Matches
  BGE-M3, mxbai-embed-large, snowflake-arctic-embed, and most modern
  open-source multilingual encoders served via Ollama / TEI.
  - Schema migration: `schema.sql` opens with a `DO` block that drops
    `embeddings` + `embedding_jobs` if their existing column type
    doesn't match the new literal. Pre-1.0 the tables are
    considered ephemeral on dim changes; the worker re-embeds
    everything on the next master start.
  - For OpenAI text-embedding-3-small (1536), set
    `VAULTMCP_EMBEDDING_DIM=1536` *and* edit the `vector(1024)`
    literal in `schema.sql` before running `migrate`.

### Fixed

- `EventBroadcaster.stop()` now `remove_listener`s on the LISTEN
  connection before the pool reclaim. Eliminates the
  `InterfaceWarning: ... is being released to the pool but has 1
  active notification listener` that surfaced on every test teardown.

## [0.4.0] — 2026-05-10

**Security + Search + Dashboard.** Combines the v0.3 (security) and
v0.4 (search & dashboard) ROADMAP phases. Master now refuses
unauthenticated writes once a server is registered, validates every
write against a regex catalogue and a frontmatter schema, exposes
audit + lexical + semantic + hybrid search through MCP tools, runs an
async embedding worker against pgvector, and ships a five-page
HTMX dashboard. 96 tests pass (74 unit + 22 e2e).

### Added — Security (v0.3)

- **Bearer-token auth** (`vaultmcp.master.auth`). Tokens are stored
  hashed (`servers.token_hash`, SHA-256) and verified against an
  `Authorization: Bearer <token>` header. Auth is bypassed when the
  `servers` table is empty (dev mode); enforcing flips on the moment
  the first server is registered.
- **CLI for token lifecycle**: `vaultmcp-master add-server`,
  `rotate-token`, `remove-server`, `list-servers`. Tokens are printed
  exactly once at creation.
- **Per-server-app authorization**: `wiki.write` and `wiki.append_log`
  require the session's `server_id` to match the token and the session's
  `app` to be in the server's registered list. Mismatch → 403.
- **Path-level ownership** (`vaultmcp.master.ownership`) loaded from
  `/srv/vaultmcp/config.yaml`. Supported expressions: `producer=<app>`
  (only that app may write here), `write=human-only` (MCP rejects every
  write). Glob patterns; first match wins. `consumer=*` and policy
  blocks are recognised by the parser and raise a clear error so they
  aren't silently ignored.
- **Validation middleware** (`vaultmcp.master.validation`). Every
  `wiki.write` is checked for size (1 MiB cap, configurable),
  encoding (UTF-8, no NULs, no bare CR), frontmatter (required
  `title`, `type`, `owners`, `updated`; type from the known set), and
  secret patterns (AWS keys, GitHub PATs, Slack tokens, bcrypt hashes,
  Postgres/MySQL URIs with passwords, PEM private keys). Errors
  surface the pattern *name*, never the matched string.
- **`wiki.audit`** MCP tool exposes the audit log with filters for
  path, since, server, app, operation, limit.
- **Agent token plumbing**: `VAULTMCP_TOKEN` env var or
  `<state_dir>/token` file (per `DESIGN.md` §3.5). `MasterClient`
  injects `Authorization: Bearer …` on every HTTP and SSE request.

### Added — Search & dashboard (v0.4)

- **Lexical search** via Postgres full-text. Generated `tsvector`
  column over title (weight A) + content (weight B), GIN index,
  `websearch_to_tsquery` syntax, `ts_headline` snippets.
- **pgvector** (`CREATE EXTENSION vector` — superuser one-time step
  documented in `deploy/README.md`). `embeddings(path, version, model,
  dim, embedding vector(1536))` with HNSW cosine index, plus an
  `embedding_jobs` queue populated atomically inside the
  `wiki.write` transaction.
- **Embedding worker** (`vaultmcp.master.embeddings`). Pluggable
  provider (`EmbeddingProvider` Protocol). Ships `NullEmbeddingProvider`
  — deterministic SHA-256-derived unit-norm vectors — so the pipeline
  is testable without an external API. Worker claims jobs via `FOR
  UPDATE SKIP LOCKED`, retires after 5 attempts.
- **Semantic + hybrid search** via Reciprocal Rank Fusion (k=60).
  `wiki.search` gains a `mode` parameter (`lexical` | `semantic` |
  `hybrid`); hybrid combines both ranker outputs. Default stays
  `lexical` so callers from `v0.3` keep working.
- **Dashboard** (`vaultmcp.master.dashboard`) — FastAPI + Jinja2 +
  PicoCSS + HTMX, no JS build pipeline. Five pages:
  - `/` — Live activity (recent events; auto-refresh every 5 s).
  - `/servers` — Registered servers with apps + last activity.
  - `/search` — Form + ranked results with snippets.
  - `/vectors` — Vector explorer: top-10 nearest neighbors of any path.
  - `/audit` — Filterable audit log (path, server, app, outcome).
- **HTTP Basic gate** for the dashboard, opt-in via
  `VAULTMCP_DASHBOARD_PASSWORD`. Constant-time credential comparison.

### Changed

- Test fixture `clean_database` now drops the master's tables
  explicitly instead of `DROP SCHEMA public CASCADE`, so the
  superuser-installed `vector` extension survives between tests.
- `tools.py` exception classes (`ToolError`, `ConflictError`,
  `ForbiddenError`, `NotFoundError`, `ValidationFailed`,
  `SizeLimitExceeded`) moved to `vaultmcp.master.errors` so the
  validation module can raise them without a circular import.
  `tools.py` re-exports the same names — existing imports keep working.

### Deploy

- `deploy/README.md` documents the one-time `CREATE EXTENSION vector`
  superuser step.
- `deploy/config.example.yaml` ships an example ownership config
  covering all six apps from `DESIGN.md` §7.2.
- `deploy/agent.env.example` includes the new `VAULTMCP_TOKEN` slot.

## [0.2.1] — 2026-05-10

Post-`v0.2.0` cleanup. No new features; tightens deploy layout and
backfills the multi-agent regression test.

### Added

- `tests/test_two_agents.py` — multi-agent E2E that boots master + two
  agents in-process and asserts cross-server propagation in under
  ten seconds. Regression guard for the `SyncSuppressor` fix from
  commit 918f334.
- `tests/conftest.py` — shared `database_url` and `clean_database`
  fixtures so e2e tests are repeatable against a real Postgres.

### Changed

- Master deployment paths consolidated under `/srv/vaultmcp/` (config
  + rendered wiki + future state). Replaces the split between
  `/etc/vaultmcp/` and `/var/lib/vaultmcp/`. Affects
  `deploy/master.service`, `deploy/master.env.example`,
  `deploy/README.md`, and the `VAULTMCP_WIKI_DIR` default in
  `vaultmcp.master.config`.

## [0.2.0] — 2026-05-10

**First implementation release.** Combines the v0.1 (skeleton) and v0.2
(real-time + offline) ROADMAP phases into a single milestone. The goal
from ROADMAP — *a change on Server A appears on Server B within 60
seconds without git push/pull* — is demonstrated end-to-end against
Postgres 17 + Python 3.13.

### Added

#### Master (`src/vaultmcp/master/`)

- `schema.sql` — Postgres tables `pages`, `log_entries`, `audit`,
  `events`, `subscriptions`, `servers`; `global_version_seq` sequence;
  INSERT trigger on `pages` that emits `NOTIFY events_channel`.
- `db.py` — asyncpg connection pool; `write_page` with optimistic
  concurrency (`SELECT ... FOR UPDATE` + version etag); JSONB datetime
  encoder; `events_since` for SSE catch-up.
- `tools.py` — pure handlers for `wiki.read`, `wiki.write`, `wiki.list`,
  `wiki.append_log` (the append-only log bypasses the etag).
- `render.py` — atomic, traversal-safe markdown render to
  `<wiki_dir>/<path>` after every write.
- `sse.py` — `EventBroadcaster`: one dedicated LISTEN connection,
  in-memory fanout to N HTTP subscribers, prefix filtering, 30 s
  heartbeats.
- `server.py` — FastAPI app with `/healthz`, `/mcp/call` (JSON-over-HTTP
  tool dispatch), `/mcp/subscribe` (SSE).
- `__main__.py` — `vaultmcp-master serve` and `migrate` CLIs.

#### Agent (`src/vaultmcp/agent/`)

- `client.py` — async HTTP client + SSE parser; raises `ConflictError`
  on 409 with master's current content.
- `watcher.py` — watchdog observer with 0.5 s debounce; on conflict,
  replaces the local file with master's copy (no auto-merge).
- `sync.py` — SSE subscribe loop with exponential-backoff reconnect;
  applies `PageChanged` by fetching and writing the local mirror.
- `queue.py` — durable on-disk write queue for offline mode; replayed
  on reconnect.
- `version_cache.py` — SQLite cache of per-page version + global cursor.
- `suppressor.py` — `SyncSuppressor` prevents the watcher from echoing
  master's own broadcasts back as fresh writes (which would trigger a
  409 → re-apply → echo loop).
- `__main__.py` — `vaultmcp-agent run`, `status`, `verify` CLIs.

#### Tests

- Unit tests for frontmatter, render, queue, version cache.
- E2E test that boots the master in-process and exercises a full
  write→read against a real Postgres.
- Multi-agent E2E test that boots master + two agents and asserts
  cross-server propagation in under 10 seconds.

#### Deploy

- `deploy/master.service`, `deploy/agent.service` — systemd unit files
  (master system-wide, agent as `--user`).
- `deploy/master.env.example`, `deploy/agent.env.example` —
  environment-variable templates.
- `deploy/docker-compose.yml` — Postgres-only compose for local dev.
- `deploy/README.md` — install + dev walkthrough.

### Changed

- Master deployment paths consolidated under `/srv/vaultmcp/` (config
  + rendered wiki + future state). Replaces the split between
  `/etc/vaultmcp/` and `/var/lib/vaultmcp/`.

### Notes

- v0.2 binds master to localhost. Auth (bearer tokens), per-app
  ownership, validation middleware, and the `wiki.audit` tool land in
  v0.3.
- pgvector / embeddings / dashboard land in v0.4.
- See `ROADMAP.md` for the full phase plan.

## [0.0.1] — 2026-05-09

**Design release.** No code yet. This release ships a complete vision and an implementation-ready design.

### Added

- `README.md` — vision, 30-second pitch, architecture diagram, DB+dashboard prominent
- `DESIGN.md` — full architecture (DB-first, pgvector required, dashboard core, extension model)
- `docs/01-vision.md` — the "why a separate project" essay
- `docs/02-vs-vaultmesh.md` — head-to-head comparison and migration path
- `docs/03-extensibility.md` — how other apps plug into the DB (OB1-inspired)
- `docs/04-dashboard.md` — what the dashboard does and how it's built
- `docs/diagrams/` — four Mermaid sources (topology, sync sequence, ownership, extensibility)
- `ROADMAP.md` — phases v0.1 through v1.x
- `ATTRIBUTION.md` — credits to VaultMesh, OB1 (prominent), Karpathy, MCP, pgvector, others
- `LICENSE` — MIT

### Architecture decisions in this release

See `DESIGN.md` §17 for the full decision log. The four most consequential:

- **D9.** DB-first, not file-first. Postgres is canonical; markdown files are projection.
- **D10.** pgvector required, not optional. Semantic search is a core capability.
- **D11.** Dashboard is core, not a separate project. FastAPI + HTMX, deliberately minimal.
- **D12.** OB1-style extension model. Other apps share the DB through prefixed tables.
