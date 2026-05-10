# CLAUDE.md — VaultMCP project context

This file is auto-loaded by Claude Code (and any compatible LLM agent) on session start in this repository. It carries the context any contributor needs to be productive without re-reading every doc.

For the architectural deep-dive, read [`DESIGN.md`](DESIGN.md). For the project's "why," read [`docs/01-vision.md`](docs/01-vision.md). This file is the **operational guide** — what to do, what not to do, where things live.

## What this project is

**VaultMCP** is an MCP-first knowledge mesh for AI agents. Postgres+pgvector at master; small Python agent on each server mirrors the wiki locally and syncs through MCP. Sibling project of [VaultMesh](https://github.com/alinclaudiu/vaultmesh) — same wiki structure, different transport (MCP+DB vs git+files).

Status: **v0.6.1 is the current tag**; ROADMAP phases v0.0 → v0.6 all landed. The `main` branch beyond v0.6.1 has the network MCP transport (`/mcp/streamable`), the ruff cleanup pass, and a Claude Desktop integration walkthrough in the README — likely cut as v0.7.0 when the next ROADMAP item begins. First production deploy is live on `webApps` (master) + `BcDev` (first agent). Code requires Python 3.11+ and Postgres 15+ with pgvector.

## Confirmed stack (do not change without an ADR)

- **Python 3.11+** — `requires-python` in `pyproject.toml`
- **`mcp` Python SDK** — for the MCP protocol surface (currently used via JSON-over-HTTP at `/mcp/call`; full SDK transport adapter is a follow-up)
- **`asyncpg`** — Postgres driver (async; required for the LISTEN/NOTIFY SSE fanout to work without blocking)
- **`fastapi` + `uvicorn`** — HTTP layer for `/healthz`, `/mcp/call`, `/mcp/subscribe` (SSE), and the future dashboard
- **`pydantic` v2** — wire-format validation; models live in `src/vaultmcp/shared/types.py`
- **`watchdog`** — agent file watcher (cross-platform)
- **`httpx`** — agent HTTP client (`http2`-capable; needed for clean SSE streaming)
- **`click`** — CLIs for both master and agent
- **`pytest` + `pytest-asyncio`** — tests; e2e gated behind `VAULTMCP_TEST_DATABASE_URL`
- **`uv`** preferred for dependency management (`make install` falls back to pip if `uv` isn't installed)

## Repository structure

```
vaultmcp/
├── pyproject.toml              hatchling build, two console scripts, ruff/mypy/pytest config
├── Makefile                    install / fmt / lint / test / e2e / migrate / run-master / run-agent / docker-up
├── README.md                   public pitch (vision + diagram + status)
├── DESIGN.md                   implementation-ready architecture (~9000 words)
├── ATTRIBUTION.md              VaultMesh, OB1, Karpathy, MCP, pgvector
├── ROADMAP.md                  v0.1 → v1.x phases
├── CHANGELOG.md
├── docs/                       deeper reading (vision, vs-vaultmesh, extensibility, dashboard)
│   └── diagrams/               4 Mermaid sources
│
├── src/vaultmcp/
│   ├── shared/                 frontmatter parsing + Pydantic types — used by both master and agent
│   ├── master/
│   │   ├── schema.sql          Postgres migration (Phase 1+2 tables + LISTEN/NOTIFY trigger)
│   │   ├── config.py           env-driven config
│   │   ├── db.py               asyncpg pool + write_page (with optimistic concurrency)
│   │   ├── render.py           atomic file rendering, traversal-safe
│   │   ├── tools.py            pure tool handlers (handle_read, handle_write, etc.)
│   │   ├── sse.py              EventBroadcaster: Postgres LISTEN → in-memory fanout to N HTTP subscribers
│   │   ├── server.py           FastAPI app: /healthz, /mcp/call, /mcp/subscribe
│   │   └── __main__.py         click CLI: serve, migrate
│   └── agent/
│       ├── config.py           env-driven config
│       ├── client.py           httpx async client + SSE parser
│       ├── queue.py            durable on-disk write queue (offline mode)
│       ├── version_cache.py    SQLite cache (per-page version + global cursor)
│       ├── watcher.py          watchdog file watcher with debouncing
│       ├── sync.py             SSE subscribe loop with exponential-backoff reconnect
│       └── __main__.py         click CLI: run, status, verify
│
├── tests/                      unit + e2e
└── deploy/                     docker-compose, systemd units, env examples
```

## Module boundaries (who imports what)

- `vaultmcp.shared` — imported by both master and agent. Must NOT import from `vaultmcp.master` or `vaultmcp.agent`.
- `vaultmcp.master.*` — must NOT import from `vaultmcp.agent.*`.
- `vaultmcp.agent.*` — must NOT import from `vaultmcp.master.*` (the agent talks to master only via the HTTP/SSE wire).
- `tests/*` — may import any internal module for unit testing.

If you find yourself wanting to cross these boundaries, the answer is almost always: extract the shared piece into `vaultmcp.shared`.

## Architectural decisions you should not relitigate without an ADR

These are pinned in `DESIGN.md` §17. The key ones to keep in mind while coding:

- **D9. DB-first.** Postgres is canonical. Files on master are a projection rendered post-commit. Don't add code that treats files as authoritative.
- **D5. Optimistic concurrency via version etag.** No row locks beyond the `SELECT ... FOR UPDATE` inside `write_page`. Conflicts return 409 with the master's current state; the agent replaces local content (no auto-merge).
- **D6. Append-only log bypasses etag.** `wiki.append_log` always succeeds; it's an INSERT, not a write-with-version.
- **D8. Full mirror on each agent.** Don't add prefix-based caching or eviction; agents always carry the full wiki.
- **D11. Dashboard is core.** Ships in this repo (FastAPI + Jinja2 + HTMX), not a separate project.
- **D12. Extensions share the DB.** Other apps create tables under `ext_<name>_*` namespaces and share the `embeddings` + `events` + `audit` infrastructure. Every extension runs under its own NOLOGIN Postgres role; `ext.query` / `ext.exec` use `SET LOCAL ROLE` so the role's GRANTs are the security boundary.

## Coding conventions

- **Type hints** are required everywhere. `mypy --strict` is the target. New code must pass `make lint`.
- **Async** by default. The master is fully async (`asyncpg`, `fastapi`, async test fixtures). Don't introduce `requests` or threaded I/O.
- **Errors**: subclass `ToolError` (master) or `MasterClientError` (agent). Never raise bare `Exception`.
- **Logging**: use `structlog` or stdlib `logging` (we already use stdlib). Log payloads as structured fields, not f-strings.
- **No print statements** in src/. Use `click.echo` in CLI commands.
- **Tests over comments** for non-obvious behavior. If you can write a 3-line test that explains it, do that instead of a paragraph of comments.
- **Pydantic v2 syntax** — `model_dump`, `ConfigDict`, etc. No v1 `.dict()` etc.

## Testing conventions

- Unit tests don't need infrastructure; they should run from a fresh checkout with `make install && make test-unit`.
- E2E tests are gated behind `VAULTMCP_TEST_DATABASE_URL` and the `e2e` pytest marker. Default `pytest` runs them only if explicitly requested.
- Don't introduce a test that requires a network connection to anything other than `localhost`.

## Git workflow

This is a regular Python project, not a wiki — there's **no `vs`/`vp` discipline here**. Use git directly.

- Branch from `main` for non-trivial changes; small fixes can land directly.
- Commit per task. One concern per commit message.
- For PRs: `gh pr create`, request review (human), wait for CI green, merge.
- Keep `main` deployable. The `make test-unit` target should always pass on `main`.
- The repo's commit history starts from a single seed commit `3762f0f` authored as `alinclaudiu` via the GitHub noreply email. Match that pattern: don't introduce author identities that include personal email addresses.

## What's already in (don't re-implement)

Everything from v0.2 → v0.6.1 has shipped. Concretely:

- **Bearer-token auth** (`vaultmcp.master.auth`) + `add-server` / `rotate-token` / `remove-server` / `list-servers` CLI. Auth is bypassed only when the `servers` table is empty (dev mode).
- **Per-server-app authorization** (session.server_id ↔ token, session.app ↔ servers.apps).
- **Path-level ownership** rules from `/srv/vaultmcp/config.yaml` (`producer=`, `write=human-only`).
- **Validation middleware** — size, encoding, required frontmatter keys, secret-pattern catalog.
- **`wiki.audit`** MCP tool over the audit log.
- **Embeddings + pgvector** — `embeddings` + `embedding_jobs` tables, async worker, `OpenAICompatibleEmbeddingProvider` (works with litellm / vLLM / OpenAI), `NullEmbeddingProvider` for tests, default dim 1024.
- **`wiki.search`** with `mode=lexical | semantic | hybrid` (RRF combine).
- **Dashboard** — five pages (activity, servers, search, audit, vectors) at `/`, HTTP Basic gate via `VAULTMCP_DASHBOARD_PASSWORD`, SSE-driven live activity.
- **Extensions** — `ext.register / list / declare_table / query / exec / embed / emit_event / deregister` with real Postgres-role isolation per extension.
- **Official MCP transport** — `vaultmcp-master mcp-stdio` (stdio) + `/mcp/streamable` (network). The legacy `/mcp/call` JSON-over-HTTP shim from v0.2 is still around.
- **`/metrics`** Prometheus text endpoint.
- **`vaultmcp-master render-all`** — rebuild `/srv/vaultmcp/wiki/` from the DB after a restore.
- **Backup runbook + perf baseline** — `deploy/BACKUP.md`, `deploy/PERFORMANCE.md`. Single + concurrent benchmarks under `bench/`.

## What NOT to do (yet)

- ❌ **`wiki.delete`** — out of scope; pages are marked `status: deprecated` in frontmatter instead.
- ❌ **Multi-master / federation** — out of scope until v1.x.
- ❌ **structlog migration** — `structlog` is in deps but the codebase uses stdlib `logging`. The migration is intentionally deferred to v1.x — the value/risk ratio is poor right now.
- ❌ **`Co-Authored-By: Claude` trailers on commits** — the user explicitly does not want them. Commit author stays as the user.
- ❌ **Adding `claude-code` to the GitHub topics** — established preference from the VaultMesh sibling project.

## Where to read for more context

- [`README.md`](README.md) — the public pitch
- [`DESIGN.md`](DESIGN.md) — full architecture (start with §3 storage, §4 MCP tools, §5 conflicts, §6 offline)
- [`docs/01-vision.md`](docs/01-vision.md) — why this project exists separately from VaultMesh
- [`docs/02-vs-vaultmesh.md`](docs/02-vs-vaultmesh.md) — when to pick which
- [`docs/03-extensibility.md`](docs/03-extensibility.md) — the OB1-style extension model (shipped in v0.5/v0.6)
- [`docs/04-dashboard.md`](docs/04-dashboard.md) — the live dashboard (shipped in v0.4)
- [`deploy/README.md`](deploy/README.md) — production install, including the one-time superuser steps (`CREATE EXTENSION vector`, `ALTER ROLE vaultmcp WITH CREATEROLE`)
- [`deploy/BACKUP.md`](deploy/BACKUP.md) — daily backup script + restore + reverse-proxy / TLS guidance
- [`deploy/PERFORMANCE.md`](deploy/PERFORMANCE.md) — baseline numbers from `bench/run.py` + `bench/parallel.py`
- [`ROADMAP.md`](ROADMAP.md) — phases through v1.x
- Open issues: https://github.com/alinclaudiu/vaultmcp/issues

## When in doubt

1. Read the relevant section of `DESIGN.md` first. It's implementation-ready and authoritative.
2. If `DESIGN.md` doesn't answer it, check whether it's a Phase 3+ concern (see "What NOT to do" above).
3. If it's a real gap in the design, open an issue tagged `design-question` rather than guessing.
