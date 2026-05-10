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
