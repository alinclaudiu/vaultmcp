"""VaultMCP agent — per-server file watcher + MCP client.

Runs on each client server. Watches the local wiki mirror, syncs local edits
to master via MCP, applies incoming changes from master's SSE stream, queues
writes when offline.

Entry point: ``vaultmcp-agent`` (or ``python -m vaultmcp.agent``).
"""
