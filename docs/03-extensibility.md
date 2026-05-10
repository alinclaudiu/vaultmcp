---
title: "Extending VaultMCP — building other apps on the same DB"
type: guide
status: v0.0.1
updated: 2026-05-09
---

# Extending VaultMCP

VaultMCP's master runs Postgres+pgvector. The database isn't private to VaultMCP — it's designed as a substrate that **other applications can build on**.

This pattern is borrowed directly from [Open Brain (OB1)](https://github.com/NateBJones-Projects/OB1), where one Postgres holds the user's knowledge plus extensions for life-management domains.

## Why this matters

If you adopt VaultMCP and later want to build:

- A **CRM** that links contacts to wiki pages and runs semantic search across both
- A **planning tool** that consumes the wiki's deployment runbooks and adds its own scheduling tables
- A **time-series tracker** of incidents, drawing on debugging pages
- A **custom AI agent** with its own memory, sharing audit + embeddings infrastructure

…you don't spin up a second database. You register an extension.

## The model

Each extension has:

- A unique **name** (e.g. `crm`, `incident_tracker`)
- A reserved **schema prefix** (e.g. `ext_crm_`, `ext_incident_`)
- A **policy** declaring what it can read/write/query in core tables
- An **owner** (one or more apps in the wiki that maintain it)

VaultMCP enforces:

- Extensions can create tables ONLY under their schema prefix.
- Extensions can READ core tables (`pages`, `embeddings`, `audit`, `events`) according to policy.
- Extensions cannot WRITE to core tables (writes go through `wiki.write`/`wiki.append_log` MCP tools, never SQL).
- Extensions can use the shared `embeddings` table for their own content (with their own `path` namespace, e.g. `ext/crm/contacts/<id>`).

## Registration flow

```typescript
// Via MCP, by an authorized agent (typically a human ops action)
ext.register({
  name: "crm",
  schema_prefix: "ext_crm_",
  owners: ["webstore", "admin"],
  policy: {
    can_read_pages: true,
    can_read_audit: false,
    can_use_embeddings: true,
    can_subscribe_events: true
  }
})

// Then the extension declares its tables
ext.declare_table("crm", "contacts", {
  columns: [
    { name: "id", type: "uuid", primary_key: true },
    { name: "name", type: "text", not_null: true },
    { name: "email", type: "text" },
    { name: "linked_page_path", type: "text" },           // FK-soft to pages.path
    { name: "created_at", type: "timestamptz", not_null: true }
  ]
})
// Master creates: CREATE TABLE ext_crm_contacts (...)
```

## Querying

```typescript
ext.query("crm", `
  SELECT c.name, p.metadata->>'updated' as last_wiki_update
  FROM ext_crm_contacts c
  LEFT JOIN pages p ON p.path = c.linked_page_path
  WHERE c.created_at > $1
`, ["2026-04-01"])
```

The query runs as a Postgres role bound to the extension's schema. Attempts to touch core tables outside the policy return SQL-level permission errors.

## Sharing embeddings

The `embeddings` table is shared. An extension can index its own content:

```typescript
// CRM indexes a contact's free-text notes
ext.embed("crm", "ext/crm/contacts/123", "Met at conference; interested in pos integration")
// → master computes embedding, stores in embeddings with path="ext/crm/contacts/123"

// CRM searches semantically across CRM contacts AND wiki
wiki.search({
  query: "pos integration",
  prefix: "",                  // or "ext/crm/" to limit to CRM
  filters: { type: ["app", "integration", "ext_crm"] }
})
```

This means a CRM agent asking "tell me everything about POS integrations" finds:

- The wiki page `apps/pos/README.md`
- The contract `integrations/01-inventory--pos--product-data.md`
- The CRM contact who's interested in pos
- The audit entry showing who last edited the pos pages

…all in one query.

## Audit and event integration

Extensions write to their own tables but emit events through master's `events` table. Subscribers (including the dashboard) see CRM activity alongside wiki activity.

```typescript
// Extension emits an event
ext.emit_event("crm", {
  event_type: "ContactCreated",
  payload: { contact_id: "123", linked_page: "apps/pos/README.md" }
})
```

These events appear in the dashboard's live activity feed, alongside `PageChanged` and `LogAppended` from wiki sessions.

## What extensions don't get (and why)

- **Direct writes to `pages`** — would break consistency with the file rendering and MCP `wiki.write` flow. Use `wiki.write` if a CRM needs to write a wiki page.
- **Direct writes to `audit`** — audit is master-owned. Extensions emit events; master logs.
- **Modifying core schema** — backward compatibility issue. Extensions own their schema; master owns core.

## When NOT to make something an extension

- If your app is read-only over the wiki — use MCP read tools, don't touch the DB directly.
- If your app's data has its own consistency requirements that conflict with VaultMCP's transaction model — run a separate database.
- If you need full control over the DB instance — same.

Extensions are for things that *belong* in the same brain.

## A worked example: tiny CRM

The full example will land in v0.5 (when extensions are implemented). Sketch:

```sql
-- After ext.register("crm", ...) and ext.declare_table calls:

CREATE TABLE ext_crm_contacts (
  id UUID PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT,
  linked_page_path TEXT,
  notes TEXT,
  created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE ext_crm_opportunities (
  id UUID PRIMARY KEY,
  contact_id UUID REFERENCES ext_crm_contacts(id),
  status TEXT NOT NULL CHECK (status IN ('lead', 'qualified', 'won', 'lost')),
  linked_integration_path TEXT,             -- e.g. "integrations/01-inventory--pos--product-data.md"
  created_at TIMESTAMPTZ NOT NULL
);

-- Embeddings are shared:
-- ext.embed("crm", "ext/crm/contacts/<uuid>", contact.notes)
-- → row in core embeddings table with path="ext/crm/contacts/<uuid>"

-- Now wiki.search with mode=semantic finds CRM contacts whose notes match a query,
-- alongside wiki pages.
```

A small CRM-specific UI could query the extension's tables and call `wiki.search` for cross-source recall. That's the OB1 pattern.

## Migration story

A user can prototype an extension in a separate database and migrate later — VaultMCP's schema prefixes prevent table-name collisions. This means you don't have to commit to extension-mode upfront.

## Versioning

Extensions declare their own schema migrations. VaultMCP tracks `extensions.schema_version` per extension. Migration tools (run by extension code, validated by master) handle ALTER TABLE within the extension's namespace.

## Security caveats

Extensions can read core tables. That means:

- An extension with `can_read_pages: true` sees all wiki content. Don't grant this to extensions you don't trust.
- An extension with `can_read_audit: true` sees who's been writing what. Privacy-sensitive.
- The `embeddings` table is shared by default. Extensions can technically read each other's embeddings unless namespaces are enforced (planned for v0.6).

Best practice: audit `extensions.policy` quarterly. Treat the DB as a multi-tenant environment.
