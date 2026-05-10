# Changelog

All notable changes to VaultMCP are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/).

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
