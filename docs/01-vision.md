---
title: "Why VaultMCP — a new vision"
type: essay
status: v0.0.1
updated: 2026-05-09
---

# Why VaultMCP — a new vision

This essay is the answer to "why a new project, not a feature inside [VaultMesh](https://github.com/alinclaudiu/vaultmesh)?"

## The first answer: VaultMesh

VaultMesh is a *brilliant, scrappy* answer to "how do AI agents share knowledge across servers?" Its bet: **git is enough**. Each server clones the wiki. Two shell aliases (`vs`, `vp`) sync. The append-only log is the message bus. Ownership is encoded in `CLAUDE.md`. Pre-commit hook stops secrets. Done.

VaultMesh works. We've used it; others should too. It's the *right tool* when:

- You don't want to operate any new infrastructure
- Manual sync discipline is acceptable
- Your only AI agent is Claude Code (or you're fine with filesystem access for others)
- Offline-first matters

But VaultMesh leaves three things on the table.

## What VaultMesh leaves on the table

### 1. The discipline tax

Every session, every engineer, every server must remember:
- Run `vs` at the start.
- Run `vp "msg"` at the end.

Skip `vs`, work on stale state. Skip `vp`, lose the broadcast. The tax is small per session, but compounds. After three months of "I forgot to `vs`, that's why my agent didn't know", a small but real fraction of incidents trace back to discipline failure.

We could solve this with a cron daemon (auto-`vs`/`vp`). That works. It's the cheapest fix. But it doesn't solve the next two.

### 2. The integration boundary

VaultMesh is filesystem-native. Other agents — Cursor, Aider, OpenAI's MCP-aware agents, custom scripts — can technically open the files, but they don't get the *semantics* (ownership rules, validation, conflict awareness, version tracking). They become second-class.

If your AI fleet includes more than one tool, VaultMesh's filesystem-as-API forces every tool to re-implement the schema understanding. That's leak-prone and high-friction.

### 3. The scale ceiling

VaultMesh works through ~100 wiki pages on ~5 servers. Past that:

- The log gets noisy (every session, every server, every day adds an entry; reading "what's relevant for me" becomes a `grep` exercise).
- Cross-app references multiply (every new app squares the integration matrix).
- Git history becomes the only audit trail, and `git log` isn't great for "who changed this paragraph and why" at scale.

You can fix these one at a time (lint operation, `qmd` for search, dashboards). But you're building a new thing, piece by piece, on top of a transport (git) that wasn't designed for any of it.

## The second answer: VaultMCP

VaultMCP makes a different bet. **Run a small server with a serious database.** In exchange:

- **Sync becomes invisible.** No `vs`. No `vp`. The agent on each server quietly mirrors writes to master and listens for changes. Sessions read local files; never know about networking.
- **Standard API.** Master speaks MCP. Any MCP-aware agent — Claude Code, Cursor, Aider, custom — calls `wiki.read`, `wiki.write`, `wiki.search`, `wiki.subscribe`. No filesystem-mounting needed.
- **Server-side governance.** Ownership rules, frontmatter requirements, secret-pattern detection — all enforced in master middleware, not in client hooks. Single source of truth for policy.
- **Native semantic search.** pgvector lives next to the wiki. Embeddings update on write. `wiki.search(mode=semantic)` works out of the box.
- **A dashboard.** Live activity, search, audit — built in, not "next time".
- **A shared substrate.** The DB isn't private to VaultMCP. Other apps plug in.

That last point is the biggest difference vs the previous iteration of this design. Inspired directly by [Open Brain (OB1)](https://github.com/NateBJones-Projects/OB1), the database is positioned as **a foundation for other applications to build on** — not just a backing store for one tool.

A small org adopting VaultMCP gets, for the price of one master, both:
- An AI-agent knowledge mesh
- A platform for any future internal app that wants to share embeddings, audit, or knowledge

This compounds. Six months in, when someone wants to build a CRM, they don't spin up another database — they extend the existing one. The CRM knows what the wiki knows.

## The third answer (which is no, but worth naming): pure OB1

You could just run OB1. It's open source, has more existing extensions, and a community.

The reason VaultMCP exists separately is **engineering coordination is its primary use case**, while OB1 is built for personal life management (Household Knowledge Base, Meal Planning, Job Hunt). The pattern overlaps; the audiences don't.

VaultMCP imports OB1's architecture. It doesn't import OB1's content focus. The wiki's first-class concepts — apps, integrations, flows, ADRs, runbooks, debugging pages, log entries — are engineering primitives. The case study is a 5-app e-commerce ecosystem, not a household.

A user could, in principle, run **both** — VaultMCP for engineering, OB1 for life. Same MCP standard. Different brains.

## Different bets, different audiences

VaultMesh is for the **scrappy small org**: minimum overhead, single ops person, "git is fine".

VaultMCP is for the **proper small-to-medium org**: already runs services, wants real-time consistency, has more than one AI tool in the stack, considers governance non-negotiable.

A small org might start with VaultMesh and migrate to VaultMCP when it outgrows the discipline tax or the single-tool limit. The wiki itself ports across — only the transport changes.

## Why ship the design first, code later

This repo currently ships only the design (v0.0.1). No code yet. Three reasons.

**First**, an implementation of VaultMCP is a serious engineering effort — ~4 focused weeks for v0.1. Before sinking that, the design needs to survive contact with critique. A written design is the cheapest way to find errors.

**Second**, VaultMesh deserves room to breathe. It just shipped (v0.1.0). If we immediately publish "here's its successor", we step on its adoption. Letting both projects stand side-by-side, with clear "use this when..." guidance, is more honest.

**Third**, the MCP specification is still evolving. Building too quickly means rebuilding when the spec changes. A design doc ages better than half-finished code.

The intent is: when someone with budget and intent says "I want this for my org", the design hands them everything except the keystrokes. They can choose to fund the build, contribute back, or fork.

## What this repo will become

Stages:

1. **v0.0.1 (now)** — Vision + design. What you're reading.
2. **v0.1.0** — Working skeleton (Phase 1 of the design's implementation plan). One master, one agent, one server.
3. **v0.5.0** — Production-internal-grade. All five phases. Auth, conflict semantics, offline queue, audit, pgvector, dashboard, extensibility.
4. **v1.0.0** — Stable API. Documentation. A few external adopters who will tell us what we got wrong.
5. **v1.x** — Federation, dashboard panel API for extensions, model-pluggable embeddings, ingest tooling.

If you're reading this in stage 2 or beyond, the README should reflect what's actually shipped. If you're reading it in stage 1, this is the contract.
