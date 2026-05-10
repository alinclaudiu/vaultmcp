-- VaultMCP master — initial schema (v0.2)
--
-- Apply once on a fresh Postgres database.
-- Idempotent: every CREATE uses IF NOT EXISTS where Postgres allows.
--
-- Phase 1+2 tables only. The pgvector / extensions / search bits land in v0.4+.
--
-- Apply with:
--   psql -U vaultmcp -d vaultmcp -f schema.sql
-- or via the master CLI:
--   vaultmcp-master migrate

-- =============================================================
-- Sequences
-- =============================================================

CREATE SEQUENCE IF NOT EXISTS global_version_seq;

-- =============================================================
-- Servers (registered client servers)
--
-- Phase 3 will gate writes by token_hash. For Phase 1+2, this table
-- exists but is not yet enforced; the master accepts writes from any
-- caller that supplies a valid server_id string.
-- =============================================================

CREATE TABLE IF NOT EXISTS servers (
    id           TEXT PRIMARY KEY,
    apps         TEXT[] NOT NULL,
    token_hash   TEXT,                    -- nullable in v0.2; becomes NOT NULL in v0.3
    rotated_at   TIMESTAMPTZ
);

-- =============================================================
-- Pages — the canonical wiki content
-- =============================================================

CREATE TABLE IF NOT EXISTS pages (
    path                TEXT PRIMARY KEY,
    content             TEXT NOT NULL,
    version             BIGINT NOT NULL,                          -- per-page monotonic
    global_version      BIGINT NOT NULL,                          -- monotonic across all pages
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,       -- parsed frontmatter
    type                TEXT NOT NULL,                            -- app|integration|flow|adr|log|index|module|debugging|runbook|shared
    owners              TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    updated             TIMESTAMPTZ NOT NULL,
    last_writer_server  TEXT NOT NULL,
    last_writer_app     TEXT NOT NULL,
    last_writer_session TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS pages_type_idx ON pages(type);
CREATE INDEX IF NOT EXISTS pages_owners_idx ON pages USING GIN(owners);
CREATE INDEX IF NOT EXISTS pages_global_version_idx ON pages(global_version);
CREATE INDEX IF NOT EXISTS pages_updated_idx ON pages(updated DESC);

-- =============================================================
-- Append-only session log (the message-bus role)
-- =============================================================

CREATE TABLE IF NOT EXISTS log_entries (
    id                    BIGSERIAL PRIMARY KEY,
    ts                    TIMESTAMPTZ NOT NULL,
    server_id             TEXT NOT NULL,
    app                   TEXT NOT NULL,
    modules_touched       TEXT[],
    integrations_updated  TEXT[],
    notable               TEXT[],
    global_version        BIGINT NOT NULL
);

CREATE INDEX IF NOT EXISTS log_ts_idx ON log_entries(ts DESC);
CREATE INDEX IF NOT EXISTS log_server_idx ON log_entries(server_id, ts DESC);

-- =============================================================
-- Audit (every operation, for Phase 3+ but populated from Phase 1)
-- =============================================================

CREATE TABLE IF NOT EXISTS audit (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    operation       TEXT NOT NULL,                                -- read|write|delete|subscribe|append_log|search
    path            TEXT,
    server_id       TEXT NOT NULL,
    app             TEXT NOT NULL,
    agent_model     TEXT,
    prompt_hash     TEXT,
    version_before  BIGINT,
    version_after   BIGINT,
    outcome         TEXT NOT NULL,                                -- ok|conflict|forbidden|validation_failed
    error_code      TEXT,
    client_ip       INET
);

CREATE INDEX IF NOT EXISTS audit_path_ts_idx ON audit(path, ts DESC);
CREATE INDEX IF NOT EXISTS audit_server_ts_idx ON audit(server_id, ts DESC);
CREATE INDEX IF NOT EXISTS audit_ts_idx ON audit(ts DESC);

-- =============================================================
-- Events (the SSE feed source)
--
-- Master INSERTs into this table inside the same transaction as the
-- pages write. A trigger NOTIFYs on insert; the master process LISTENs
-- on `events_channel` and fans out to subscribed agents via SSE.
-- =============================================================

CREATE TABLE IF NOT EXISTS events (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    event_type      TEXT NOT NULL,                                -- PageChanged|PageDeleted|LogAppended|Heartbeat
    path            TEXT,
    global_version  BIGINT NOT NULL,
    payload         JSONB
);

CREATE INDEX IF NOT EXISTS events_ts_idx ON events(ts DESC);
CREATE INDEX IF NOT EXISTS events_global_version_idx ON events(global_version DESC);

-- LISTEN/NOTIFY trigger: announce every new event so the master process
-- can push it to SSE subscribers in real time.
CREATE OR REPLACE FUNCTION notify_event() RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_notify(
        'events_channel',
        json_build_object(
            'id', NEW.id,
            'event_type', NEW.event_type,
            'path', NEW.path,
            'global_version', NEW.global_version
        )::text
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS events_notify_trigger ON events;
CREATE TRIGGER events_notify_trigger
    AFTER INSERT ON events
    FOR EACH ROW EXECUTE FUNCTION notify_event();

-- =============================================================
-- Subscriptions (per-agent stream cursors)
--
-- Updated by the master whenever an agent connects via wiki.subscribe
-- and on every event delivered. Useful for the dashboard's per-agent view.
-- =============================================================

CREATE TABLE IF NOT EXISTS subscriptions (
    agent_id              TEXT PRIMARY KEY,
    server_id             TEXT NOT NULL,
    last_global_version   BIGINT NOT NULL DEFAULT 0,
    connected_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    prefix_filter         TEXT
);

CREATE INDEX IF NOT EXISTS subscriptions_server_idx ON subscriptions(server_id);

-- =============================================================
-- Extensions (per docs/03-extensibility.md)
--
-- An extension is another application that owns tables under a
-- reserved schema prefix (e.g. `ext_crm_*`) and reads from core
-- tables according to its policy. Master enforces the prefix at the
-- application layer; full Postgres-role isolation lands in v0.6.
-- =============================================================

CREATE TABLE IF NOT EXISTS extensions (
    name           TEXT PRIMARY KEY,                     -- "crm", "incident_tracker"
    schema_prefix  TEXT NOT NULL UNIQUE,                 -- "ext_crm_", "ext_incident_"
    owners         TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    policy         JSONB NOT NULL DEFAULT '{}'::jsonb,   -- {can_read_pages, can_read_audit, ...}
    schema_version INT NOT NULL DEFAULT 1,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS extensions_owners_idx
    ON extensions USING GIN (owners);

-- =============================================================
-- Full-text search (lexical)
--
-- A generated tsvector column on `pages` enables `wiki.search` over
-- the page body (and title, weighted higher). Semantic search via
-- pgvector layers on top in v0.4 — `wiki.search` will combine the two
-- through Reciprocal Rank Fusion. Until then this gives basic search
-- with zero external dependencies.
-- =============================================================

ALTER TABLE pages
    ADD COLUMN IF NOT EXISTS content_tsv tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(metadata->>'title', '')), 'A')
        || setweight(to_tsvector('english', coalesce(content, '')), 'B')
    ) STORED;

CREATE INDEX IF NOT EXISTS pages_content_tsv_idx
    ON pages USING GIN (content_tsv);

-- =============================================================
-- Vector search (pgvector)
--
-- One row per page in `embeddings`; the worker keeps it in sync via the
-- `embedding_jobs` queue. Dimension is fixed per deployment.
-- Default: 1024 (matches BGE-M3, mxbai-embed-large, snowflake-arctic-embed,
-- and most modern open-source multilingual encoders served via Ollama/TEI).
-- For OpenAI text-embedding-3-small (1536) or other models, change the
-- literal below and re-run migrate; pre-existing embeddings are dropped
-- by the migration block below if the column type doesn't match.
-- =============================================================

CREATE EXTENSION IF NOT EXISTS vector;

-- Migration: if the embeddings table already exists with a vector
-- dimension different from the literal in CREATE TABLE below, drop
-- both embeddings + embedding_jobs so the CREATE re-runs with the
-- new dim. Pre-1.0, schema is destructive across dim changes.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_attribute a
        JOIN pg_class c ON a.attrelid = c.oid
        JOIN pg_namespace n ON c.relnamespace = n.oid
        WHERE n.nspname = 'public'
          AND c.relname = 'embeddings'
          AND a.attname = 'embedding'
          AND format_type(a.atttypid, a.atttypmod) <> 'vector(1024)'
    ) THEN
        DROP TABLE IF EXISTS embeddings CASCADE;
        DROP TABLE IF EXISTS embedding_jobs CASCADE;
        RAISE NOTICE 'embeddings dimension changed; tables recreated';
    END IF;
END$$;

CREATE TABLE IF NOT EXISTS embeddings (
    path           TEXT PRIMARY KEY REFERENCES pages(path) ON DELETE CASCADE,
    version        BIGINT NOT NULL,             -- the page version this embedding reflects
    model          TEXT NOT NULL,               -- e.g. "openai-compat/bge-m3"
    dim            INT NOT NULL,                -- vector dimension; must match column type
    embedding      vector(1024) NOT NULL,
    computed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- HNSW index for cosine distance — works for both inner-product and
-- cosine queries. ivfflat is the alternative when row count > 1M.
CREATE INDEX IF NOT EXISTS embeddings_embedding_cos_idx
    ON embeddings USING hnsw (embedding vector_cosine_ops);

-- =============================================================
-- Embedding jobs queue
--
-- Master enqueues a row whenever a page is written; the worker pops the
-- oldest pending row, computes an embedding, upserts into `embeddings`,
-- and deletes the job. Inserts are idempotent on (path, version) — if
-- the worker is offline and the master writes the same page twice,
-- only one job survives.
-- =============================================================

CREATE TABLE IF NOT EXISTS embedding_jobs (
    id             BIGSERIAL PRIMARY KEY,
    path           TEXT NOT NULL,
    version        BIGINT NOT NULL,
    enqueued_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    attempts       INT NOT NULL DEFAULT 0,
    last_error     TEXT,
    UNIQUE (path, version)
);

CREATE INDEX IF NOT EXISTS embedding_jobs_pending_idx
    ON embedding_jobs (enqueued_at) WHERE attempts < 5;
