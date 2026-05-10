---
title: "VaultMCP vs VaultMesh"
type: comparison
status: v0.0.1
updated: 2026-05-09
---

# VaultMCP vs VaultMesh

Both projects share the same wiki structure, the same writing conventions, the same case study. They differ in **transport**, **infrastructure footprint**, and **target audience**.

## At a glance

| Concern | VaultMesh | VaultMCP |
|---|---|---|
| Transport | git pull / push | MCP over HTTPS+SSE |
| User discipline | `vs` / `vp` per session | none — auto-sync |
| Real-time | no (after manual sync) | yes (1–3s typical) |
| Master required | only at sync time | preferred always; offline OK with queue |
| Local files on each server | full mirror (git clone) | full mirror (agent-managed) |
| Server-side enforcement | client-side hook (per server) | server-side middleware (consistent) |
| Audit trail | git history | structured audit table + optional git |
| Vector search | external add-on | native (pgvector, required) |
| Standard agent API | filesystem | MCP tools |
| Dashboard | none | built in (FastAPI + HTMX) |
| Extension model | none | OB1-style (other apps share the DB) |
| Ops cost | git remote only | git remote + master MCP server + Postgres |
| Conflict handling | git rebase | optimistic concurrency (version etag) |

## Pick VaultMesh if

- You don't want to operate any new server
- Sync discipline (`vs`/`vp`) is fine for your team
- Claude Code is your only AI agent (or you're OK with filesystem access)
- You're starting small and want to see if the pattern fits before investing
- Offline-first is non-negotiable
- You don't (yet) want to build other apps on top of the same data

## Pick VaultMCP if

- You already run services; one more isn't a problem
- You have more than one AI tool (or expect to soon)
- You want real-time updates without thinking about sync
- You want server-side governance — ownership and secret-detection enforced uniformly
- Semantic search over knowledge matters to you
- You want a proper audit table, not just git history
- You want a dashboard for live visibility
- You expect to build other internal apps that should share the same knowledge / vector store

## Migration: VaultMesh → VaultMCP

VaultMesh wikis are byte-compatible with VaultMCP wikis. The structure (`wiki/apps/`, `wiki/integrations/`, etc.), frontmatter, wikilinks, log format — all the same.

To migrate:

1. Stand up master (Postgres + master MCP server). Point its rendering folder at a copy of your VaultMesh wiki, then ingest the wiki into Postgres (`vaultmcp ingest /path/to/vaultmesh/wiki`).
2. Install agents on each server.
3. Decommission `vs`/`vp` aliases on each server when its agent is healthy.

Rollback is reverse: stop the agent, resume `vs`/`vp`. The master's git mirror is a regular remote — it stays in sync because of auto-commit on master writes.

The wiki is the value; the transport is implementation detail.

## When to NOT use either

If you have:

- One service on one server with one developer → use [Karpathy's plain LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f). Multi-server overhead is wasted.
- A 200+-app organization → both projects will struggle. Look at federated approaches; we don't address them yet.
- Strong real-time-collaboration needs (multiple humans editing the same paragraph) → use a CRDT-based system. VaultMCP's per-file conflicts won't satisfy you.
- Personal life management as the primary use case → consider [Open Brain (OB1)](https://github.com/NateBJones-Projects/OB1) directly. VaultMCP's shape is engineering-focused; OB1 has more existing extensions for personal domains.

## Coexistence

VaultMCP's master can run *both* the git remote (for VaultMesh-style clients) and the MCP server simultaneously. Auto-commit on master keeps both transports synchronized. So you can:

- Have some servers on VaultMesh (legacy or simple servers)
- Have some servers on VaultMCP (the bulk of your active development)
- Migrate gradually, server by server

There's no flag day. The brain is one; the access patterns are many.
