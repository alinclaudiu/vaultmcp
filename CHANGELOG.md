# Changelog

All notable changes to VaultMCP are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/).

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
