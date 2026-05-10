# Attribution

VaultMCP stands on the shoulders of these projects and ideas. We owe each, and want to be loud about it.

## VaultMesh

VaultMCP shares its wiki structure, ownership conventions, frontmatter requirements, writing patterns, security rules (secret regex catalog), and case study with [VaultMesh](https://github.com/alinclaudiu/vaultmesh). The two projects represent different transport bets but the same belief: structured markdown maintained by AI agents is a good idea.

If you're using VaultMCP and find yourself wondering "where did this convention come from?", read VaultMesh's `template/CLAUDE.md` — most of the answer is there.

## Open Brain (OB1) — Nate B. Jones

[OB1](https://github.com/NateBJones-Projects/OB1) is the most direct architectural inspiration for VaultMCP's database layer. Three patterns we borrow directly:

- **DB-as-platform.** OB1 positions Postgres+pgvector as a foundation that multiple applications build on, not as private storage. VaultMCP's extension model (`docs/03-extensibility.md`) is a direct adaptation.
- **Primitives.** OB1's `/primitives` folder collects cross-cutting helpers (e.g. content fingerprint deduplication). VaultMCP's roadmap includes a similar primitives layer for v1.x.
- **MCP-first.** OB1 demonstrated that exposing knowledge through MCP works in production. We didn't have to invent the pattern; we adapted it.

OB1 itself is built for personal/family knowledge management (Household Knowledge Base, Meal Planning, Job Hunt). VaultMCP's case is engineering coordination. Different audiences, shared architecture.

If you're using VaultMCP and find yourself wanting personal-life extensions, you might genuinely prefer OB1 over building them inside VaultMCP. Both run side-by-side without conflict.

## Andrej Karpathy — *LLM Wiki* (2025)

The three-layer pattern (raw / wiki / schema), the operations vocabulary (Ingest / Query / Lint), the append-only `log.md`, the "LLMs eliminate maintenance burden" framing — all from Karpathy's gist:

> https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f

Karpathy framed it for one user with one wiki. VaultMesh extended that to N servers via git. VaultMCP extends it again — DB-first, MCP transport, extensible substrate.

## Vannevar Bush — *As We May Think* (1945)

Bush's *Memex* — a personal device for storing, cross-linking, and re-finding knowledge through associative trails — is the philosophical ancestor of every wiki, every hyperlink, and every personal knowledge graph since. Karpathy nods to it; we nod through Karpathy.

> https://www.theatlantic.com/magazine/archive/1945/07/as-we-may-think/303881/

## Model Context Protocol (MCP)

The wire protocol VaultMCP uses for the master ↔ agent boundary. Spec: https://modelcontextprotocol.io/. Originated at Anthropic, adopted broadly. Without MCP, we'd be designing a custom RPC and reinventing client integration for every agent.

## pgvector

The optional embedding store. https://github.com/pgvector/pgvector. PostgreSQL extension that makes "Postgres + vector search" a first-class option without a separate vector DB. The single most important enabling technology for VaultMCP's "DB-first" design.

## Other influences

- **Architecture Decision Records** (Michael Nygard, 2011) — the ADR shape (Context / Decision / Alternatives / Consequences). VaultMesh added a `proposed → promoted` workflow; VaultMCP inherits it.
- **Optimistic concurrency control** — etcd, Consul, S3 conditional writes, HTTP `If-Match`. The version-etag conflict model is theirs.
- **Server-Sent Events (SSE)** — the streaming-HTTP variant that MCP supports. Simpler than WebSockets, firewall-friendly, fits our one-way-push pattern.
- **Reciprocal Rank Fusion** (Cormack, Clarke, Buettcher 2009) — the algorithm we use to combine lexical and semantic search scores in `wiki.search` hybrid mode.
- **HTMX** — interactivity without a frontend framework. The dashboard ships as Jinja2 + HTMX, no Node, no build pipeline.
- **systemd** — for running master and agent as supervised services on Linux.
