# Backup & restore runbook

VaultMCP keeps everything in two places: **Postgres** (the canonical
state — pages, embeddings, audit, events, extensions, …) and the
**rendered wiki directory** (`/srv/vaultmcp/wiki/`, the on-disk
projection). A complete backup needs both.

The Postgres dump is the only thing that's strictly required to recover
— the wiki directory is regenerated on the next write, and operators
can run `vaultmcp-master render-all` (TBD) when one is added in v1.x to
re-render every page after a restore.

## What to back up

| Item | Source of truth | Recovery cost if lost |
|---|---|---|
| `pages`, `log_entries`, `audit`, `events`, `subscriptions`, `servers`, `extensions`, `embeddings`, `embedding_jobs` | Postgres | None — restore the dump and you're back. |
| `/srv/vaultmcp/wiki/*.md` | rendered from `pages` | Recover from dump (regenerate via re-write or a full re-render tool). |
| `/srv/vaultmcp/master.env` | operator-provided | Re-create from `master.env.example` + your secrets vault. |
| `/srv/vaultmcp/config.yaml` (ownership rules) | operator-provided | Restore from your config repo. |
| Extension Postgres roles (`vaultmcp_ext_*`) | derived from `extensions` table | Re-created automatically on first `ext.register` after a clean restore. |

## Daily backup script

The Postgres dump uses the custom format (`-Fc`) so `pg_restore` can
parallelise + selectively restore tables:

```bash
#!/usr/bin/env bash
set -euo pipefail

ts=$(date -u +%Y%m%dT%H%M%SZ)
dest=/srv/backups/vaultmcp
mkdir -p "$dest"

# 1. Postgres dump (custom format, max compression).
sudo -u postgres pg_dump \
  --format=custom --compress=9 \
  --jobs=4 \
  vaultmcp \
  > "$dest/vaultmcp-$ts.dump"

# 2. Wiki tree snapshot. Use --link if backups live on the same FS for
#    de-dup; otherwise tar+zstd.
tar --use-compress-program=zstd \
    -cf "$dest/wiki-$ts.tar.zst" \
    -C /srv/vaultmcp wiki

# 3. Sanity: if either step produces zero bytes, fail loud.
test -s "$dest/vaultmcp-$ts.dump"
test -s "$dest/wiki-$ts.tar.zst"

# 4. Retain the last 14 dumps + 14 wiki snapshots.
ls -1t "$dest"/vaultmcp-*.dump   | tail -n +15 | xargs -r rm --
ls -1t "$dest"/wiki-*.tar.zst    | tail -n +15 | xargs -r rm --

# 5. Off-site copy. Replace with rsync to a backup host, restic, S3,
#    or whatever your org uses.
# rsync -aP "$dest/" backup-host:/backups/vaultmcp/
```

Drop this in `/etc/cron.daily/vaultmcp-backup` (or a systemd timer)
and ensure `/srv/backups/vaultmcp/` is owned by `root:root`, mode
`0750`.

## Restoring to a fresh master

```bash
# 0. Stop the master so nothing tries to write while we restore.
systemctl stop vaultmcp-master

# 1. Drop and recreate the database. Pre-1.0 we restore into an empty
#    DB rather than trying to merge into an existing one — there's no
#    upsert path that handles every table cleanly.
sudo -u postgres psql -c "DROP DATABASE IF EXISTS vaultmcp;"
sudo -u postgres psql -c "CREATE DATABASE vaultmcp OWNER vaultmcp;"

# 2. Re-create the pgvector extension (superuser; not in the dump).
sudo -u postgres psql -d vaultmcp -c "CREATE EXTENSION vector;"

# 3. Restore. --jobs runs in parallel; --no-owner / --role keep
#    everything owned by the live `vaultmcp` role rather than whatever
#    role names lived on the dump host.
sudo -u postgres pg_restore \
  --dbname=vaultmcp \
  --jobs=4 \
  --no-owner --role=vaultmcp \
  /path/to/vaultmcp-<ts>.dump

# 4. Restore the wiki directory.
mkdir -p /srv/vaultmcp
tar --use-compress-program=zstd \
    -xf /path/to/wiki-<ts>.tar.zst \
    -C /srv/vaultmcp

# 5. Restart and verify.
systemctl start vaultmcp-master
curl -s http://127.0.0.1:8080/healthz   # -> {"status":"ok"}

# 6. Spot-check from the dashboard or wiki.list:
#    - count of pages matches what you backed up
#    - latest audit entry has the expected timestamp
#    - embedding_jobs is empty (worker drained pre-backup) or
#      drains within a few seconds.
```

## Verifying a backup without a full restore

The fastest check: `pg_restore --list` enumerates every object in
the dump — useful in CI to assert that none have been silently
dropped:

```bash
pg_restore --list /srv/backups/vaultmcp/vaultmcp-latest.dump \
  | grep -cE 'TABLE DATA public (pages|audit|events|embeddings)'
# expect 4
```

For a deeper check, restore into a throw-away DB on the same host:

```bash
sudo -u postgres createdb vaultmcp_verify
sudo -u postgres pg_restore --dbname=vaultmcp_verify --no-owner --role=postgres /path/to/dump
sudo -u postgres psql -d vaultmcp_verify -c "SELECT count(*) FROM pages;"
sudo -u postgres dropdb vaultmcp_verify
```

## What's intentionally not in this runbook

- **Continuous WAL archiving / PITR.** Out of scope for pre-1.0. If
  your VaultMCP instance has audit-loss tolerance below "yesterday at
  midnight", set up Postgres physical replication separately
  (`pg_basebackup` + `wal_g` or similar).
- **Encrypted off-site copies.** Decision lies with whoever owns the
  backup host. The runbook above produces unencrypted dumps; tighten
  via `gpg` / `age` / `restic` per your org's policy.
- **mTLS for backup transport.** If backups travel between hosts,
  put them on an internal network or use SSH (rsync over SSH is the
  easy default).

## TLS / cross-host hardening

The master binds to `127.0.0.1` by default. For agents on remote
hosts the recommended path is **a reverse proxy in front** (nginx,
Caddy, Traefik) terminating TLS on port 443 and forwarding to
`127.0.0.1:8080`. The bearer-token auth in `master` is the actual
authentication boundary; TLS at the proxy layer is the transport
boundary.

In-process mTLS in `uvicorn` is supported but not recommended:
- you'd duplicate cert lifecycle management already handled by the
  proxy (`certbot`, ACME, etc.);
- log/auth tooling expects HTTP requests on `localhost`, which the
  proxy preserves;
- per-host bearer tokens are simpler to rotate than mTLS client
  certs at our scale.

If you do want in-process TLS, set:

```env
VAULTMCP_TLS_CERT=/etc/ssl/private/vaultmcp.crt
VAULTMCP_TLS_KEY=/etc/ssl/private/vaultmcp.key
```

(both under `master.env`) and uvicorn picks them up automatically
when running via `vaultmcp-master serve`. Note: this is an
operator-side configuration; the project doesn't add a code-level
TLS feature in v0.6.
