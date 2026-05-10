---
title: "VaultMCP — Operations manual"
type: guide
status: current
updated: 2026-05-10
---

# VaultMCP operations

Day-to-day tasks an operator does on a live deploy. Pairs with
[`deploy/README.md`](../deploy/README.md) (initial install) and
[`deploy/BACKUP.md`](../deploy/BACKUP.md) (recovery).

This file lives next to the source so it ages well; copy-paste
commands target the canonical paths from `deploy/master.env.example`.

## Service control

### Master (system-wide systemd)

```bash
sudo systemctl start    vaultmcp-master
sudo systemctl stop     vaultmcp-master
sudo systemctl restart  vaultmcp-master
sudo systemctl status   vaultmcp-master --no-pager
sudo systemctl is-active vaultmcp-master   # → "active"

# Logs
sudo journalctl -u vaultmcp-master -f             # follow live
sudo journalctl -u vaultmcp-master -n 50 --no-pager
```

After a code change (`git pull` + reinstall):

```bash
sudo /opt/vaultmcp/venv/bin/pip install --quiet --force-reinstall --no-deps \
     /home/<user>/projects/vaultmcp
sudo systemctl restart vaultmcp-master
```

`--force-reinstall --no-deps` re-copies the source files even when
the version in `pyproject.toml` hasn't changed. Skip `--no-deps` only
when pyproject.toml's deps actually changed.

### Agent (per-user systemd)

The agent runs as `systemd --user`, so commands take `--user` and
are run as the host user (NOT through `sudo`):

```bash
systemctl --user start    vaultmcp-agent
systemctl --user stop     vaultmcp-agent
systemctl --user restart  vaultmcp-agent
systemctl --user status   vaultmcp-agent

journalctl --user -u vaultmcp-agent -f
journalctl --user -u vaultmcp-agent -n 50 --no-pager
```

If `vaultmcp-agent status` (the CLI subcommand, not systemd) is run
from a shell, it auto-sources `~/.config/vaultmcp/agent.env` (added
in v0.6.1). No need to manually `set -a; source …`.

## Token lifecycle

The `servers` table is empty initially → master runs in dev mode
(no auth). The moment the **first** server is registered, every
`/mcp/*` request must carry a valid bearer token.

### Issue a token

```bash
sudo -u vaultmcp env $(sudo cat /srv/vaultmcp/master.env | grep -v '^#' | xargs) \
  /opt/vaultmcp/venv/bin/vaultmcp-master add-server \
    --id BcDev \
    --apps bcdev,shared
```

Output prints the plaintext token **once** — copy it immediately into
the agent host's `~/.config/vaultmcp/agent.env` (`VAULTMCP_TOKEN=…`).
Master only stores the SHA-256 hash; if you lose the token you
rotate, you don't recover.

The same `env $(…)` dance is needed for every CLI subcommand below
because `sudo -u vaultmcp` strips `EnvironmentFile=` semantics — only
systemd's unit honors it. Alternative: just `source` the env into
your shell once and drop the dance.

### Rotate

```bash
vaultmcp-master rotate-token --id BcDev
# Prints a new plaintext token; the previous one stops working immediately.
# Update the agent's VAULTMCP_TOKEN before the agent next reconnects.
```

The agent retries on auth failure with exponential backoff up to ~60s,
so the new token must land within that window or you get sync gaps.

### Remove

```bash
vaultmcp-master remove-server --id BcDev
# Confirmation prompt; the row + token_hash are deleted. Existing
# audit rows that reference this server_id stay (foreign-key-less).
```

### List

```bash
vaultmcp-master list-servers
# server-1   apps=['webstore', 'admin']   rotated=2026-05-10T15:25:18+00:00
# BcDev      apps=['bcdev', 'shared']     rotated=2026-05-10T18:34:12+00:00
```

## Adding a new agent host

Full walkthrough in [README.md "Quick start"](../README.md#quick-start).
Short version, after `add-server` issues a token:

```bash
ssh admin@<new-host>

# Clone + venv
git clone https://github.com/alinclaudiu/vaultmcp ~/projects/vaultmcp
python3 -m venv ~/.local/share/vaultmcp-venv
~/.local/share/vaultmcp-venv/bin/pip install -e ~/projects/vaultmcp

# Configure
mkdir -p ~/vault ~/.config/vaultmcp ~/.config/systemd/user
cat > ~/.config/vaultmcp/agent.env <<EOF
VAULTMCP_MASTER_URL=http://<master-lan-ip>:8080
VAULTMCP_SERVER_ID=<id-from-add-server>
VAULTMCP_APP=<one-of-the-registered-apps>
VAULTMCP_TOKEN=<the-printed-token>
VAULTMCP_VAULT_DIR=$HOME/vault
EOF
chmod 600 ~/.config/vaultmcp/agent.env

# systemd --user unit
cp ~/projects/vaultmcp/deploy/agent.service ~/.config/systemd/user/
sed -i "s|ExecStart=.*|ExecStart=$HOME/.local/share/vaultmcp-venv/bin/vaultmcp-agent run|" \
    ~/.config/systemd/user/agent.service

systemctl --user daemon-reload
systemctl --user enable --now vaultmcp-agent
systemctl --user status vaultmcp-agent
```

If the agent host is on a different subnet than the master and the
ping works but the agent shows backoff retries → check ufw on master:

```bash
sudo ufw status verbose | grep 8080
# expect: 8080/tcp ALLOW IN <agent-subnet-or-broader>
sudo ufw allow from <agent-subnet> to any port 8080 proto tcp
```

One agent runs as one app. To have a single host write under
multiple apps (e.g. both `apps/bcdev/**` and `shared/**`), run a
second agent with a different `VAULTMCP_VAULT_DIR` and
`VAULTMCP_APP=shared`, on the same `VAULTMCP_SERVER_ID`/token.

## Dashboard

- URL: `http://<master-lan-ip>:8080/`
- Username: from `VAULTMCP_DASHBOARD_USERNAME` (default `admin`)
- Password: from `VAULTMCP_DASHBOARD_PASSWORD` (no default; without
  it, dashboard is wide-open on the LAN)

To rotate the password:

```bash
sudoedit /srv/vaultmcp/master.env
# change VAULTMCP_DASHBOARD_PASSWORD=…
sudo systemctl restart vaultmcp-master
```

## Embeddings

The embedding worker stays dormant until `VAULTMCP_EMBEDDING_PROVIDER`
is set in `/srv/vaultmcp/master.env`.

### Activate

```bash
sudoedit /srv/vaultmcp/master.env
# Uncomment + fill the embedding block:
#   VAULTMCP_EMBEDDING_PROVIDER=openai-compat
#   VAULTMCP_EMBEDDING_BASE_URL=https://<your-litellm-or-openai>/v1
#   VAULTMCP_EMBEDDING_API_KEY=<token>
#   VAULTMCP_EMBEDDING_MODEL=<model-name>
#   VAULTMCP_EMBEDDING_DIM=1024   # must match the model and schema

sudo systemctl restart vaultmcp-master
```

The worker drains `embedding_jobs` on its own. Watch progress on
`/servers` (queue depth + last error) or via `/metrics`:

```bash
curl -s -u admin:$DASH_PASS http://<master>:8080/metrics | grep embedding
```

### Switch model / change dim

If the new model returns vectors of a different dimension, the schema
needs to change too. The schema's migration block detects the
mismatch on next `vaultmcp-master migrate` and **drops** `embeddings`
+ `embedding_jobs`; the worker re-embeds from scratch on the next
master start.

```bash
# 1. Edit the literal in src/vaultmcp/master/schema.sql:
#      vector(1024)  ->  vector(<new-dim>)
# 2. Update master.env's VAULTMCP_EMBEDDING_DIM accordingly.
# 3. Reinstall + restart.
sudo /opt/vaultmcp/venv/bin/pip install --quiet --force-reinstall --no-deps \
     /home/<user>/projects/vaultmcp
sudo systemctl restart vaultmcp-master
```

### Probe a litellm/OpenAI-compat endpoint before wiring

```bash
curl -s -X POST <base-url>/embeddings \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"model":"<model>","input":"hello"}' \
  | jq '.data[0].embedding | length'
# expect: a positive integer matching VAULTMCP_EMBEDDING_DIM
```

## Extensions

### Register

```bash
# Through MCP (HTTP), since ext.* run on /mcp/call:
curl -s -X POST http://<master>:8080/mcp/call \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "tool": "ext.register",
    "args": {
      "name": "crm",
      "owners": ["webstore", "admin"],
      "policy": {
        "can_read_pages": true,
        "can_use_embeddings": true,
        "can_subscribe_events": true
      }
    }
  }'
```

Master creates a NOLOGIN Postgres role `vaultmcp_ext_crm` and grants
it the policy-mapped privileges.

### Declare a table

```bash
curl -s -X POST http://<master>:8080/mcp/call -H "Authorization: Bearer <token>" \
  -d '{
    "tool": "ext.declare_table",
    "args": {
      "extension": "crm",
      "name": "contacts",
      "columns": [
        {"name":"id",   "type":"uuid",        "primary_key": true},
        {"name":"name", "type":"text",        "not_null":   true},
        {"name":"email","type":"text"},
        {"name":"created_at","type":"timestamptz","not_null":true,"default":"NOW()"}
      ]
    }
  }'
# Master runs: CREATE TABLE ext_crm_contacts (...) ;
#              GRANT ALL ON ext_crm_contacts TO vaultmcp_ext_crm;
```

### Query / mutate

```bash
# Read (SELECT only; lexical reject of write keywords + READ ONLY txn)
ext.query  { extension: "crm", sql: "SELECT * FROM ext_crm_contacts WHERE …" }

# Write (the Postgres role's GRANTs are the security boundary)
ext.exec   { extension: "crm", sql: "INSERT INTO ext_crm_contacts …", params: [...] }

# Index ext content into the shared embeddings table; path is forced
# to ext/<name>/<rel> so wiki pages can't collide.
ext.embed  { extension: "crm", rel_path: "contacts/123", content: "..." }

# Push an event into the live activity feed; event_type is namespaced
# to ext_<name>_<type> so dashboard subscribers see who emitted it.
ext.emit_event { extension: "crm", event_type: "ContactCreated", payload: {...} }
```

### Deregister

```bash
ext.deregister  { name: "crm" }
# Drops every ext_crm_* table, every ext/crm/* page (cascades to
# embeddings + embedding_jobs via FK), the role vaultmcp_ext_crm,
# and the extensions row. Idempotent.
```

## Bulk import (VaultMesh → VaultMCP)

```bash
sudo -u vaultmcp env $(sudo cat /srv/vaultmcp/master.env | grep -v '^#' | xargs) \
  /opt/vaultmcp/venv/bin/vaultmcp-master ingest \
    --from /path/to/vaultmesh-tree \
    --server-id mesh-source-1 \
    --dry-run
# review the report, then drop --dry-run

sudo -u vaultmcp ... vaultmcp-master ingest --from ... --server-id mesh-source-1
```

For multi-repo migrations, run once per source with a different
`--server-id`. `--skip-existing` (the default) makes re-runs
idempotent. `--overwrite` bumps the version on already-imported
pages — useful when the source has changed since the last import.

## Backup / restore

See [`deploy/BACKUP.md`](../deploy/BACKUP.md) for the runbook.
Daily script: pg_dump + tar of `/srv/vaultmcp/wiki/`.
After a restore: `vaultmcp-master render-all` to rebuild the
on-disk projection.

## Releases

Cut a tag when a meaningful chunk lands.

```bash
# 1. Update CHANGELOG.md (top of file). Use the existing format.
$EDITOR CHANGELOG.md

# 2. Bump version in three places (a small script could do this; for
#    now they're cross-checked manually):
sed -i 's/version = "X.Y.Z"/version = "<new>"/' pyproject.toml
sed -i 's/__version__ = "X.Y.Z"/__version__ = "<new>"/' src/vaultmcp/__init__.py
sed -i 's/version="X.Y.Z"/version="<new>"/' src/vaultmcp/master/server.py

# 3. Verify nothing else has a stale version string
grep -rn "X\.Y\.Z" --include="*.py" --include="*.toml" --include="*.md"

# 4. Commit + tag + push
git add CHANGELOG.md pyproject.toml src/vaultmcp/__init__.py src/vaultmcp/master/server.py
git commit -m "release: v<new>"
git tag -a v<new> -m "v<new> — <summary>"
git push origin main
git push origin v<new>
```

A failing CI on `main` should block tag pushing — fix forward, don't
delete tags. (Tags can be deleted with `git tag -d <name>` locally
+ `git push origin :refs/tags/<name>` remotely, but that's destructive
once others have fetched.)

## Troubleshooting

### "PermissionError: /home/vaultmcp/.postgresql/postgresql.key"

asyncpg looks up the user's default SSL config; the `vaultmcp` system
user has no home directory (`useradd --no-create-home`). Fix is in
`master.env`:

```
VAULTMCP_DATABASE_URL=postgres://vaultmcp:…@localhost:5432/vaultmcp?sslmode=disable
```

`?sslmode=disable` tells asyncpg to skip the SSL config lookup
entirely. PG is on localhost so we don't need TLS to begin with.

### "permission denied to create extension vector"

`CREATE EXTENSION vector` requires superuser; the `vaultmcp` role
isn't one. Run **once** as `postgres`:

```bash
sudo -u postgres psql -d vaultmcp -c "CREATE EXTENSION vector;"
```

After that, the master's `vaultmcp-master migrate` (which uses
`CREATE EXTENSION IF NOT EXISTS`) is a no-op.

### "permission denied to set role 'vaultmcp_ext_…'"

The master role isn't a member of the extension role. Fix:

```bash
sudo -u postgres psql -d vaultmcp -c "GRANT vaultmcp_ext_<name> TO vaultmcp;"
```

This is normally done automatically inside `ext.register`'s
transaction, but if the registration crashed mid-way the GRANT might
not be there. The test fixture's cleanup also defensively re-grants
before `DROP OWNED BY` for the same reason.

### "permission denied to drop objects"

DROP OWNED needs membership of the target role. Same fix as above.
For test environments, `DROP OWNED BY <role>` followed by
`DROP ROLE IF EXISTS <role>` is the standard cleanup.

### `systemctl stop vaultmcp-master` hangs ~90s + SIGKILL

Was a real bug in v0.6.0; fixed in v0.6.1. SSE subscribers were
parked in `await queue.get()` and uvicorn's graceful shutdown waited
indefinitely for the SSE request to close. Two-part fix:

- `EventBroadcaster.subscribe()` polls the queue with
  `asyncio.wait_for(timeout=1.0)` so it notices `_stopping` itself.
- `uvicorn.run(..., timeout_graceful_shutdown=5)` as a backstop.

If you observe this on `main`, it's a regression — bisect against
v0.6.1+ (`git log --oneline v0.6.0..v0.6.1`).

### CI fails with "refusing to allow a Personal Access Token to create or update workflow"

Your fine-grained PAT lacks the `workflow` scope. Edit the FGT in
GitHub settings → Repository permissions → "Workflows: Read and
write". The same token starts working immediately; no `gh auth
login` needed.

### CI fails with `concurrent` shadow / circular import

Don't name a Python file inside `bench/` (or anywhere on `sys.path`)
`concurrent.py` — `asyncio` imports `concurrent.futures` at startup
and gets your file instead. Real bug from this codebase: the
concurrent-load benchmark was originally `bench/concurrent.py`; it's
now `bench/parallel.py`.

### Tests fail randomly when master service is running

The e2e tests operate on the same DB as the live master. The
`clean_database` fixture drops tables; the running master then
queries vanished tables and crashes. Stop the service before
running e2e:

```bash
sudo systemctl stop vaultmcp-master
VAULTMCP_TEST_DATABASE_URL=… pytest -m e2e
sudo systemctl start vaultmcp-master
```

CI doesn't have this problem — its Postgres service is fresh per run.

### Agent log: "Subscribing since global_version=0" but master shows no inbound connection

The log line is printed *before* the HTTP request. Network or auth
failure isn't visible from this line alone. Check on the agent:

```bash
journalctl --user -u vaultmcp-agent -n 100 --no-pager | grep -E "HTTP|error"
curl -v --max-time 5 http://<master-ip>:8080/healthz
```

If `curl` hangs → ufw on master, or routing.
If `curl` returns 200 but the agent still doesn't connect → token
mismatch. Look for `401` lines in the agent log and rotate the token.
