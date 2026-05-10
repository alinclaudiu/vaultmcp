# Roadmap

VaultMCP v0.0.1 (current) is a **design release**. Code starts in v0.1.

The phases below match `DESIGN.md` §15. Each is roughly one focused engineer-week.

## v0.1 — Skeleton (~1 week)

- [ ] Postgres schema (core tables: `pages`, `log_entries`, `audit`, `events`, `subscriptions`, `servers`)
- [ ] Master MCP server: `wiki.read`, `wiki.write`, `wiki.list`
- [ ] Wiki rendering to disk after every write
- [ ] One-server agent with file watcher → MCP write
- [ ] No auth (localhost-only)
- [ ] **Goal:** a change on Server A appears on Server B within 60 seconds, without `vs`/`vp`

## v0.2 — Real-time + offline (~1 week)

- [ ] `wiki.subscribe` (SSE)
- [ ] Postgres `LISTEN/NOTIFY` → master fanout to subscribers
- [ ] Conflict semantics (version etag)
- [ ] Offline queue on agent
- [ ] Catch-up after disconnect via `since_global_version`

## v0.3 — Security (~1 week)

- [ ] Bearer-token auth
- [ ] `servers` table, ownership/authorization config
- [ ] Validation middleware (frontmatter, secret patterns, encoding, size)
- [ ] `audit` table + `wiki.audit` MCP tool

## v0.4 — Search & dashboard (~1 week)

- [ ] pgvector setup, `embeddings` table
- [ ] Embedding worker
- [ ] `wiki.search` (lexical / semantic / hybrid via Reciprocal Rank Fusion)
- [ ] Dashboard: live activity, servers, search, audit, vector explorer
- [ ] FastAPI + HTMX, Basic auth

## v0.5 — Extensibility (~½ week)

- [ ] `extensions` table
- [ ] `ext.register`, `ext.declare_table`, `ext.query` MCP tools
- [ ] Schema-prefix isolation (Postgres roles)
- [ ] Worked example: a tiny CRM extension end-to-end

## v0.6 — Hardening (~1 week)

- [ ] Production deployment guide
- [ ] Backup / restore runbook (Postgres + git mirror)
- [ ] mTLS option
- [ ] Performance benchmarks (read p95, write p95, search p95, conflict rate)

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
