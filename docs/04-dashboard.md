---
title: "VaultMCP Dashboard"
type: guide
status: v0.0.1
updated: 2026-05-09
---

# VaultMCP Dashboard

Built into master. FastAPI + Jinja2 + HTMX + PicoCSS. No build pipeline. Five pages.

## Why a dashboard at all

The wiki itself is browsable as markdown. But there are questions the markdown can't answer at a glance:

- **What's happening right now across all servers?**
- **Which pages have changed in the last hour?**
- **What did Server 4 do yesterday that affects me?**
- **Who has been writing to the inventory docs lately?**
- **What pages mention something semantically related to "idempotency at batch upload"?**

Each can be answered by SQL queries; the dashboard makes them one click away.

## Pages

### 1. Live activity

A real-time feed of `events`. Auto-updates via SSE. Each row shows:

- Time
- Event type (PageChanged / LogAppended / PageDeleted / ext.* events)
- Path
- Server
- App
- Last writer (if applicable)
- Link to the page

Filterable by server, app, type. Default: last hour, all servers, all types.

Implementation: server subscribes to Postgres `LISTEN events_channel`; on NOTIFY, server pushes the new row through SSE to all connected dashboard sessions. Browser uses HTMX `hx-sse` to inject rows into the feed.

### 2. Servers

Per-server status table:

- ID
- Apps
- Connected: yes/no
- Last sync: <relative time>
- Queue depth (from agent's reported metric)
- Recent errors (last 5 min)
- Latest log entry (truncated)

Implementation: simple SELECT against `subscriptions` joined with recent `audit` and `log_entries`. Auto-refresh every 10s via HTMX `hx-trigger="every 10s"`.

### 3. Search

Form with:

- Query input (free text)
- Mode dropdown (lexical / semantic / hybrid)
- Filters: type, owners, prefix, updated_since

Results: list of hits with snippet, score, path link.

For hybrid mode, the dashboard shows the breakdown ("matched via title" vs "matched via embedding") so the user can see *why* a hit was returned.

Implementation: form posts to `/search`, dashboard calls `wiki.search` MCP tool internally, renders results.

### 4. Audit

Filterable view of `audit` table:

- Time range (default: last 24h)
- Path / path-prefix
- Server
- App
- Outcome (ok / conflict / forbidden / validation_failed)

Useful for compliance, debugging, "who broke X".

Each row links to a detail panel showing the full audit row plus the `version_before` and `version_after` of the affected page (if applicable).

### 5. Vector explorer

Debug page: enter a path, see "10 most similar pages by embedding distance".

Implementation: `SELECT path, 1 - (embedding <#> (SELECT embedding FROM embeddings WHERE path=$1)) AS similarity FROM embeddings ORDER BY similarity DESC LIMIT 10`.

Useful for:

- Validating embedding quality after a model change
- Finding "what other pages does the LLM consider related to this one"
- Spotting orphan content (pages with no close neighbors)

## Authentication

The dashboard requires a separate web auth (default: HTTP Basic via `htpasswd`, configurable). Not exposed to the public internet by default; runs on `127.0.0.1:8080` and accessed via SSH tunnel or VPN.

For prod deployments, put it behind a reverse proxy (nginx, Caddy) and use OAuth2 / OIDC if your org has a single-sign-on.

## Extensions on the dashboard

Extensions can register dashboard panels. Each panel is a simple template + a SQL query (or MCP tool call). Master collects panels at startup and renders them on a "Extensions" tab.

Example: a CRM extension might register a "Recent contacts" panel that shows the top 10 most recently created `ext_crm_contacts` rows.

```python
# In the CRM extension's server-side code
@panel("Recent contacts", tab="extensions")
def recent_contacts():
    return query("SELECT name, email, created_at FROM ext_crm_contacts ORDER BY created_at DESC LIMIT 10")
```

The dashboard renders this as an auto-refreshing table next to wiki panels.

The Extensions tab gracefully degrades — if no extensions register panels, the tab is hidden.

## Tech choices, briefly

- **FastAPI** — already running for `/healthz`/`/metrics`, no extra runtime
- **Jinja2** — server-side rendering, simple, debuggable
- **HTMX** — interactivity without a frontend framework. Auto-refresh, SSE, form posts handled with HTML attributes.
- **PicoCSS** — minimal stylesheet, no theme decisions
- **No JavaScript build pipeline** — all assets are static; the only "frontend code" is HTMX (CDN-loaded, ~14kB gzipped)

The result is a dashboard that ships in a single Python process, has no Node/npm/webpack, and can be hardened/proxied like any other internal HTTP service.

## What's intentionally not in v0.1

- Full-text edit of wiki pages from the dashboard (read-only for v0.1)
- User accounts beyond Basic auth
- Multi-org separation
- Advanced graph visualizations (orphan detection, link graphs — roadmap)
- Embedding-quality reports (roadmap)
- A separate "vaultmcp-dashboard-pro" project may ship a richer Next.js / SvelteKit frontend later. Core stays minimal.

## A note on philosophy

The dashboard is **not** the place to build a wiki-editing experience. Wiki edits go through the LLM agent in your editor. The dashboard is for **observing**, not editing.

This split keeps the dashboard simple (no editor widgets, no markdown rendering except for previews, no diff view beyond audit comparisons). The wiki, the agents, and the dashboard each do one thing.
