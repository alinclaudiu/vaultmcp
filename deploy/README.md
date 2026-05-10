# Deploying VaultMCP

This directory contains the artifacts needed to run VaultMCP in
development and in production. v0.2 ships with two flows:

- **Local development** (`docker-compose.yml`) — Postgres in a container;
  master and agent run from the host so you can iterate.
- **Production** (`master.service`, `agent.service`) — systemd units that
  expect master and agent to be installed on the target hosts.

## Local development

```bash
# 1. Bring up Postgres
docker compose -f deploy/docker-compose.yml up -d

# 2. Install the package locally
make install

# 3. Configure environment for the master
export VAULTMCP_DATABASE_URL=postgres://vaultmcp:vaultmcp@localhost:5432/vaultmcp
export VAULTMCP_WIKI_DIR=/tmp/vaultmcp-wiki

# 4. Apply schema
vaultmcp-master migrate

# 5. Start the master (foreground)
vaultmcp-master serve
```

In another terminal:

```bash
# 6. Configure environment for an agent
export VAULTMCP_MASTER_URL=http://127.0.0.1:8080
export VAULTMCP_SERVER_ID=server-1
export VAULTMCP_APP=webstore
export VAULTMCP_VAULT_DIR=$HOME/vaultmcp-test/vault

# 7. Start the agent
vaultmcp-agent run
```

Now any markdown file edited under `$VAULTMCP_VAULT_DIR` will be pushed
to master, and any other agent's writes will appear in your local mirror.

## Production install (sketch)

1. Provision a master host with Postgres 15+ accessible.
2. Install Python 3.11 and the VaultMCP package (`pip install vaultmcp`
   once published, or `pip install /path/to/vaultmcp` from this repo).
3. Create the `vaultmcp` system user. Create `/srv/vaultmcp/wiki`
   owned by it (and `/srv/vaultmcp/` itself, mode 0750, owned by `vaultmcp:vaultmcp`).
4. Copy `master.env.example` to `/srv/vaultmcp/master.env` (chmod 0640,
   owned by `root:vaultmcp`) and edit.
5. Copy `master.service` to `/etc/systemd/system/`.
6. `systemctl daemon-reload && systemctl enable --now vaultmcp-master`.
7. On each agent host: install the package, copy `agent.env.example` to
   `~/.config/vaultmcp/agent.env`, edit, copy `agent.service` to
   `~/.config/systemd/user/`, then
   `systemctl --user enable --now vaultmcp-agent`.

## Notes

- v0.2 binds master to localhost. Put it behind a reverse proxy
  (nginx, Caddy) for cross-host access.
- Auth (bearer tokens) lands in v0.3. Until then, **don't expose master
  to untrusted networks**.
- Backup: `pg_dump` of the `vaultmcp` database + a snapshot of the
  rendered `wiki/` directory provides two independent recovery paths.
