"""VaultMCP master — Postgres-backed MCP server.

The master process owns the canonical wiki (rows in `pages`), validates writes,
broadcasts changes to subscribed agents via SSE, and renders markdown files
to disk for human inspection and git history.

Entry point: ``vaultmcp-master`` (or ``python -m vaultmcp.master``).
"""
