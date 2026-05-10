# Roadmap

Current state: **v0.6.1 shipped, first production deploy live**
(`webApps` master + `BcDev` first agent). Phases v0.1 → v0.6 are
all complete; details in [`CHANGELOG.md`](CHANGELOG.md).

## v0.1 — Skeleton ✅

- [x] Postgres schema (`pages`, `log_entries`, `audit`, `events`, `subscriptions`, `servers`)
- [x] Master MCP server: `wiki.read`, `wiki.write`, `wiki.list`
- [x] Wiki rendering to disk after every write
- [x] One-server agent with file watcher → MCP write
- [x] **Goal met:** a change on Server A appears on Server B within seconds, no `vs`/`vp`

## v0.2 — Real-time + offline ✅

- [x] `wiki.subscribe` (SSE)
- [x] Postgres `LISTEN/NOTIFY` → master fanout to subscribers
- [x] Conflict semantics (version etag)
- [x] Offline queue on agent
- [x] Catch-up after disconnect via `since_global_version`

## v0.3 — Security ✅

- [x] Bearer-token auth + `add-server` / `rotate-token` / `remove-server` / `list-servers` CLI
- [x] `servers` table, per-server-app authorization, path-level ownership from `config.yaml`
- [x] Validation middleware (frontmatter required keys, secret-pattern catalog, encoding, size)
- [x] `audit` table + `wiki.audit` MCP tool

## v0.4 — Search & dashboard ✅

- [x] pgvector setup, `embeddings` + `embedding_jobs` tables
- [x] Embedding worker (pluggable provider; `OpenAICompatibleEmbeddingProvider`)
- [x] `wiki.search` (`mode=lexical | semantic | hybrid` via Reciprocal Rank Fusion)
- [x] Dashboard: live activity (SSE-driven), servers, search, audit, vector explorer
- [x] FastAPI + Jinja2 + HTMX + PicoCSS, HTTP Basic gate

## v0.5 — Extensibility ✅

- [x] `extensions` table
- [x] `ext.register / list / declare_table / query / exec / embed / emit_event / deregister` MCP tools
- [x] Schema-prefix isolation via real Postgres roles + `SET LOCAL ROLE` per query
- [x] Worked example: CRM extension covered by `tests/test_extensions_e2e.py`

## v0.6 — Hardening ✅

- [x] Production deployment guide ([`deploy/README.md`](deploy/README.md))
- [x] Backup / restore runbook ([`deploy/BACKUP.md`](deploy/BACKUP.md))
- [x] Reverse-proxy / TLS guidance (operator-side; in-process mTLS deferred)
- [x] Performance benchmarks ([`deploy/PERFORMANCE.md`](deploy/PERFORMANCE.md), `bench/run.py` + `bench/parallel.py`)
- [x] Official MCP transport (`mcp-stdio` CLI + `/mcp/streamable` HTTP endpoint)
- [x] `/metrics` Prometheus endpoint
- [x] `vaultmcp-master render-all` for full restore
- [x] First production deploy walkthrough verified end-to-end

## v1.0 — Stable API

- [ ] Frozen MCP tool surface
- [ ] Versioned breaking changes commitment
- [ ] Migration guide from VaultMesh

## v1.x and beyond

- [ ] Master read-replicas (for >50 servers)
- [ ] Federation across organizations
- [ ] Pluggable embedding models / providers
- [ ] Ingest tooling (Web Clipper, transcript ingestion)
- [ ] Dashboard panel API for extensions
- [ ] Primitives library (dedup, content-fingerprinting, etc. — OB1-inspired)

---

**Bias:** keep core small. Extensions go in companion repos (`vaultmcp-dashboard-pro`, `vaultmcp-ingest`, etc.) when they're not strictly needed in core. The same discipline that keeps VaultMesh tiny applies here.
