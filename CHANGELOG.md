# Changelog

All notable changes to VaultMCP are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/).

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
