---
title: "VaultMCP — Project state snapshot"
type: guide
status: current
updated: 2026-05-10
---

# Project state snapshot

The single anchor for picking the project up again — by you, or by
a future Claude Code session, or by a contributor coming in cold.
Everything specific to a moment in time goes here so the rest of the
docs can stay structural.

If you're reading this in a fresh session, also load
[`CLAUDE.md`](../CLAUDE.md) (auto-loaded by Claude Code in this
repo) and skim [`CHANGELOG.md`](../CHANGELOG.md) for the per-tag
history.

## Status as of 2026-05-10

- **Latest tag:** `v0.7.0`. `main` has commits past it
  (`feat(cli): vaultmcp-master ingest …`, lint fixes) that will fold
  into the next tag.
- **Tags on origin:** `v0.2.0`, `v0.2.1`, `v0.4.0`, `v0.4.1`,
  `v0.5.0`, `v0.6.0`, `v0.6.1`, `v0.7.0`.
- **CI:** `.github/workflows/ci.yml` running on push/PR to main +
  workflow_dispatch. Job 1 = ruff + unit; Job 2 = pgvector container
  + e2e. Last run on `main` was green.
- **Tests:** 162 passing — 123 unit + 39 e2e against a real Postgres.
- **Repo:** https://github.com/alinclaudiu/vaultmcp (public, MIT).

## Production deploy (live)

This section captures *layout*, not secrets. Token + dashboard
password live in `/srv/vaultmcp/master.env` on the master; rotate
through the operations manual without echoing them anywhere else.

### Master host: `webApps`

- LAN IP: `192.168.0.7`
- Postgres 17.8 local, `vaultmcp` DB owned by the `vaultmcp` role.
  Role has `LOGIN`, `CREATEROLE`. `vector` extension installed in
  the `public` schema (one-time superuser step).
- Code at `/opt/vaultmcp/venv/` (system venv; `pip install` the repo).
- Config + state at `/srv/vaultmcp/`:
  - `/srv/vaultmcp/master.env` — root:vaultmcp 0640
  - `/srv/vaultmcp/wiki/` — vaultmcp:vaultmcp 0750, the rendered
    markdown projection
  - `/srv/vaultmcp/config.yaml` — optional, path-level ownership
    rules (not deployed yet on this instance)
- Service: `vaultmcp-master.service` (`/etc/systemd/system/`),
  enabled, runs as user/group `vaultmcp`, `WorkingDirectory=/srv/vaultmcp`.
- Bound on `192.168.0.7:8080` (LAN-direct, no TLS yet).
- ufw rule: `8080/tcp ALLOW IN 192.168.0.0/16`.

### First agent: `BcDev`

- Host: `admin@192.168.10.11` (different subnet from master; routing
  works, master sees BcDev as `192.168.0.38` after NAT).
- Code: `~/projects/vaultmcp` (git clone), venv at
  `~/.local/share/vaultmcp-venv/`.
- Config: `~/.config/vaultmcp/agent.env` (chmod 600).
- Vault dir: `~/vault-bcdev/`.
- Service: `vaultmcp-agent.service` under `systemd --user` (so the
  user session starts/stops it; survives logout when linger is on).
- Server registration: `BcDev` with apps `bcdev,shared`.
- App identity: the agent runs as `VAULTMCP_APP=bcdev`. To also
  write under `shared/`, a second agent with the same token but
  `VAULTMCP_APP=shared` and a separate vault dir would be needed.

### Embeddings — not active yet

- `VAULTMCP_EMBEDDING_PROVIDER` is left commented in master.env.
- The intended provider is `openai-compat` against
  `https://v5.bcnet.ro/v1` with model `bge-m3` (1024-d).
- Activation gated on rotating the litellm key (the previous one was
  pasted in chat during the cross-lingual cosine probe).

### Endpoints exposed

| Endpoint | Purpose | Auth |
|---|---|---|
| `GET /healthz` | Liveness | none |
| `GET /metrics` | Prometheus text format | none (LAN gate) |
| `POST /mcp/call` | JSON-over-HTTP tool dispatch (legacy) | bearer |
| `GET /mcp/subscribe` | SSE event stream | bearer |
| `POST /mcp/streamable/` | Official MCP-over-HTTP | bearer |
| `GET /` | Dashboard activity | HTTP Basic |
| `GET /servers, /search, /audit, /vectors` | Other dashboard pages | HTTP Basic |
| `GET /dashboard/sse-events` | SSE feed for live dashboard | HTTP Basic |
| (CLI) `vaultmcp-master mcp-stdio` | MCP over stdin/stdout | inherited |

`bearer` = `Authorization: Bearer <token>` against
`servers.token_hash`. When the `servers` table is empty, auth is
bypassed (dev mode).

## Module map

```
src/vaultmcp/
├── shared/
│   ├── envfile.py        autoload .env files when not under systemd
│   ├── frontmatter.py    YAML frontmatter parse / serialize
│   └── types.py          ALL Pydantic input/output models for tools
├── master/
│   ├── __main__.py       CLI: serve | migrate | mcp-stdio | ingest |
│   │                          render-all | add-server | rotate-token |
│   │                          remove-server | list-servers
│   ├── auth.py           token gen + hash, bearer parse,
│   │                     AuthenticatedServer dataclass
│   ├── config.py         MasterConfig + from_env()
│   ├── dashboard/
│   │   ├── router.py     FastAPI router, HTTP Basic gate
│   │   └── templates/    PicoCSS + HTMX, base.html + per-page
│   ├── db.py             asyncpg pool + every Database.* method;
│   │                     the single boundary between code + Postgres
│   ├── embeddings.py     EmbeddingProvider Protocol,
│   │                     NullEmbeddingProvider,
│   │                     OpenAICompatibleEmbeddingProvider,
│   │                     EmbeddingWorker
│   ├── errors.py         ToolError + every subclass (Conflict,
│   │                     Forbidden, NotFound, ValidationFailed,
│   │                     SizeLimitExceeded). Imports nothing from
│   │                     master.tools so validation can raise them
│   │                     without a circular import.
│   ├── extensions.py     ext.* DDL helpers + role-name builder
│   ├── mcp_adapter.py    Official MCP SDK integration (stdio + the
│   │                     server factory used by /mcp/streamable)
│   ├── metrics.py        Prometheus text rendering
│   ├── ownership.py      /srv/vaultmcp/config.yaml loader + rule
│   │                     evaluation
│   ├── render.py         atomic, traversal-safe page render to disk
│   ├── schema.sql        single source of truth for the DB schema;
│   │                     idempotent, includes a DO block that drops
│   │                     embedding tables when their dim changes
│   ├── server.py         FastAPI app factory + lifespan +
│   │                     /healthz + /metrics + /mcp/call +
│   │                     /mcp/subscribe + /mcp/streamable mount
│   ├── sse.py            EventBroadcaster: one Postgres LISTEN +
│   │                     in-memory fanout to N HTTP subscribers
│   ├── tools.py          PURE handlers (handle_read, handle_write,
│   │                     handle_search, handle_ext_*, …) +
│   │                     call_handler_by_name dispatcher
│   └── validation.py     size + encoding + frontmatter +
│                         secret-pattern checks
└── agent/
    ├── __main__.py       CLI: run | status | verify
    ├── client.py         MasterClient (httpx + SSE parser),
    │                     adds Authorization header when token is set
    ├── config.py         AgentConfig + from_env()
    ├── queue.py          on-disk queue for offline writes
    ├── suppressor.py     SyncSuppressor — keeps watcher and sync
    │                     from echoing each other into a 409 loop
    ├── sync.py           SSE subscribe loop with backoff reconnect
    ├── version_cache.py  per-page version + global cursor (SQLite)
    └── watcher.py        watchdog file watcher + debounced wiki.write
```

## Architecture decisions made during build

These augment `DESIGN.md` §17 (the original ADR set). Where the
implementation diverged or added detail, it's recorded here.

### Pre-1.0, schema is destructive across embedding-dim changes

`schema.sql`'s `DO` block detects `embeddings.embedding`'s current
type and DROPs `embeddings + embedding_jobs` if it doesn't match the
literal in the next `CREATE TABLE`. The worker re-embeds on next
master start. Rationale: pre-1.0, no users have built migrations on
top of this; the cleanup is faster than wiring a real ALTER
+ re-cast pipeline.

When v1.x lands and we promise embedding stability, this becomes a
real migration tool. Until then: documented in `deploy/README.md`.

### `SET LOCAL ROLE` over a per-extension connection pool

For ext.* role isolation we considered:

1. A separate connection pool per extension, each authenticated as
   the extension's role.
2. Master holds one pool, each ext.query / ext.exec runs `SET LOCAL
   ROLE <ext_role>` inside a transaction — auto-resets at `COMMIT`
   / `ROLLBACK`.

We picked (2). Reasons:

- N pools per extension blow up file descriptors and Postgres
  `max_connections` linearly.
- `SET LOCAL ROLE` is the documented Postgres mechanism for exactly
  this; it's atomic with the transaction and can't leak.
- The defensive `GRANT <ext_role> TO CURRENT_USER` at register +
  cleanup time covers the cases where an earlier crash left
  membership stale.

### Three layers of defense for ext.query

Belt + suspenders + cufflinks:

1. **Lexical reject** at the tools layer for write keywords (INSERT,
   UPDATE, DELETE, DROP, ALTER, TRUNCATE, GRANT, REVOKE, CREATE, DO,
   SET, COMMENT, COPY, VACUUM, CLUSTER). Cheap and produces a clear
   error message before the DB sees anything.
2. **READ ONLY transaction** (`conn.transaction(readonly=True)`).
   Postgres rejects all DDL/DML at execute time even if the lexical
   check is somehow bypassed.
3. **Role-level GRANTs** scoped by the extension's policy. Even if
   the SQL is read-only, it can only see what the role was granted
   (`pages` only when `can_read_pages: true`, etc.).

ext.exec drops layer 1 (mutations are the point) and layer 2 (RW
transaction). The role's GRANTs remain the boundary. Tested by
`test_extension_role_isolation_enforces_policy`.

### `/srv/vaultmcp/` over the FHS-classic split

We bind config (`master.env`), state (`wiki/`), and future config
files (`config.yaml`) all under `/srv/vaultmcp/` instead of the
classic `/etc/vaultmcp/` + `/var/lib/vaultmcp/` split.

Trade: we deviate slightly from FHS — `/srv` is "site-specific data
served by this system" which fits but isn't where most ops folks
look first for app config. The win is a single backup target, single
chmod policy, single systemd `ReadWritePaths=`. For a project that
self-hosts as one app per host, this is cleaner. If we ever package
as `.deb`/`.rpm`, mainainers would push back; we deal with that
when we cross that bridge.

### Embedding worker co-located with the master process

The worker runs as an asyncio task inside the master's lifespan.
Alternative considered: a separate `vaultmcp-embedding-worker`
process subscribing via SSE.

Picked co-located because:

- Direct DB access (no HTTP RTT per job).
- Shares the same `MasterConfig` provider build path.
- The worker is rate-limited by the embedding provider's API, not
  by master's CPU; co-location doesn't compete for resources.
- One fewer process to monitor.

Decoupling becomes interesting only if the embedding model is local
and CPU/GPU-heavy enough to compete with the FastAPI loop — at that
point a `vaultmcp-embedding-worker` companion is straightforward.

### MCP transport: both stdio AND streamable-HTTP

The `mcp` SDK supports both. We ship both because they target
different clients:

- **stdio** — Claude Desktop, IDE plugins. Spawn as subprocess.
  Trust boundary is the parent process. No auth needed.
- **streamable-HTTP** — network MCP clients. Same auth boundary as
  every other `/mcp/*` route.

The legacy `POST /mcp/call` JSON-over-HTTP shim from v0.2 stays
around until at least v1.0; old callers continue to work.

### Per-server-app authorization, not Postgres-row-per-page

Authorization is an in-memory dict + bearer-token check, not a row
filter at the DB layer. Tradeoff:

- Wins: no per-row policy framework to maintain; the same
  `wiki.read` returns all rows regardless of caller (reads are
  globally trusted within an authenticated cluster); audit captures
  everything for after-the-fact analysis.
- Loses: a malicious authenticated server can read every page on the
  master. We accept this — VaultMCP is a "shared brain" model.
  Multi-tenant strict isolation is out of scope; use a separate
  master per tenant if you need it.

## Bugs found + fixes (postmortem style)

These bit during implementation and would bite again if forgotten.

### asyncpg listener cleanup warning

Symptom: `InterfaceWarning: <Connection ...> is being released to
the pool but has 1 active notification listener` on every
broadcaster shutdown.

Cause: `EventBroadcaster.stop()` cancelled the LISTEN task but the
task's `db.listen()` context manager called `UNLISTEN + pool.release()`
without first removing the in-Python listener registered via
`conn.add_listener()`.

Fix (commit `ed043fb`): wrap the inner sleep loop in `try/finally`
that calls `conn.remove_listener()` before the context exits.

### Graceful shutdown deadlock with active SSE clients

Symptom: `systemctl stop vaultmcp-master` hung the full
`TimeoutStopSec` (90s) and was SIGKILL'd. Live agent's
`/mcp/subscribe` connection was the cause.

Why: uvicorn's graceful shutdown order is "stop accepting new
connections → wait for active requests → run lifespan shutdown".
`broadcaster.stop()` runs in lifespan `__aexit__`, which is *after*
the wait. The SSE request is in `await sub.queue.get()` forever.
Deadlock.

Fix (commit `d4cfe05`): subscribers now poll the queue with
`asyncio.wait_for(timeout=1.0)` and check `_stopping` themselves.
A short `timeout_graceful_shutdown=5` on uvicorn is the backstop.
Stop time went from 90s+SIGKILL to 0.16s.

### asyncpg's home-dir SSL key probe

Symptom: master service crashed at startup with
`PermissionError: [Errno 13] Permission denied:
'/home/vaultmcp/.postgresql/postgresql.key'`. The `vaultmcp` system
user has no home directory.

Why: asyncpg, when no SSL params are explicit in the DSN, computes
default cert paths from the user's home. With no home, the
`Path.exists()` call hits the system root and surfaces the error.

Fix: append `?sslmode=disable` to `VAULTMCP_DATABASE_URL` in
`master.env`. Postgres is on localhost; SSL is unnecessary anyway.
Documented in `deploy/README.md` and `docs/05-operations.md`.

### `bench/concurrent.py` shadowed stdlib

Symptom: `python bench/concurrent.py` died at import time with
`ImportError: cannot import name 'wait_for' from partially
initialized module 'asyncio' (most likely due to a circular
import)`.

Why: `asyncio` imports `concurrent.futures` during its own
initialization. Python resolves `concurrent` to whatever's first
on `sys.path` — when run from the `bench/` directory, that's our
file.

Fix: renamed `bench/concurrent.py` → `bench/parallel.py`.

### `clean_database` fixture dropped pgvector extension

Symptom: every e2e test failed with
`InsufficientPrivilegeError: permission denied to create extension
"vector"` after the `clean_database` fixture ran.

Why: original fixture did `DROP SCHEMA public CASCADE; CREATE
SCHEMA public;`. The vector extension lives in `public`; dropping
the schema drops the extension. The test role can't recreate it.

Fix: switched to dropping our specific tables (`embeddings`,
`embedding_jobs`, `pages`, `audit`, …) so the extension survives.
Plus discovers and drops `ext_*` tables left behind by extension
declarations + `vaultmcp_ext_*` roles.

### `DROP OWNED BY` requires role membership

Symptom: test fixture's `DROP OWNED BY vaultmcp_ext_<name>` failed
with `permission denied to drop objects` after a crashed test left
a stale role.

Why: the registration that created the role normally also runs
`GRANT <role> TO CURRENT_USER`. A crash before that GRANT lands
leaves the role without master membership; subsequent cleanups
can't `DROP OWNED`.

Fix: cleanup tries `GRANT <role> TO CURRENT_USER` defensively before
`DROP OWNED`. Idempotent, harmless if already granted.

### FastAPI `Body(...)` / `Depends(...)` + Ruff B008

Not a runtime bug, but every CI run flagged the FastAPI idiom of
calling `Body(...)` / `Depends(...)` as argument defaults — B008
("Do not perform function call in argument defaults"). Pinning these
files in `[tool.ruff.lint.per-file-ignores]` is cleaner than
scattering `# noqa: B008` everywhere.

## What's next (priority-ordered, with tradeoffs)

1. **Activate embeddings on the live master.** 5 minutes of work
   (rotate litellm key + uncomment env block + restart). Blocks
   semantic + hybrid + vector explorer until done. *Operational*,
   not code.
2. **Use the system.** Write real wiki content from BcDev's Claude
   Code. Will surface what's actually missing better than guessing.
3. **TLS via reverse proxy** when expanding beyond the trusted LAN.
   Caddy is the lowest-friction option.
4. **VaultMesh migration** if there's content in a VaultMesh repo
   to bring over. `vaultmcp-master ingest` is ready; needs the
   source path.
5. **v1.0 freeze** — mostly a CHANGELOG entry + a `versions.md`
   commitment. The MCP tool surface, the bearer-token auth shape,
   and the ext.* API are stable; we just declare it.
6. **structlog migration** — deferred. The codebase uses stdlib
   `logging` and works fine. Move only when JSON log aggregation
   becomes a real need.
7. **Federation / multi-master / read replicas** — v1.x stretch.
   `DESIGN.md` §15 has the rough plan. Not blocking anyone.

## How to pick this up in a future session

Reading order, fastest to deepest:

1. **This file** — current state + decisions + bugs.
2. [`CLAUDE.md`](../CLAUDE.md) — operational guide auto-loaded by
   Claude Code.
3. [`CHANGELOG.md`](../CHANGELOG.md) — what landed in each tag,
   with rationale.
4. [`docs/05-operations.md`](05-operations.md) — concrete tasks +
   troubleshooting.
5. [`README.md`](../README.md) — the public pitch + Quick start.
6. [`DESIGN.md`](../DESIGN.md) — full architecture (when you need
   to change something structural).

Sanity-check commands to run on a fresh checkout:

```bash
make install
make test-unit            # 123 tests, ~1s
# (optional) full e2e — needs a Postgres + pgvector + a clean DB
VAULTMCP_TEST_DATABASE_URL=postgres://… pytest -m e2e
```

If the master is live, `curl http://<master>:8080/healthz` and
checking the dashboard's `/servers` page tell you in one minute
whether everything's still alive.
