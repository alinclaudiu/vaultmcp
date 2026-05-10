"""HTTP client for the master.

For v0.2 the master exposes its tools at ``POST /mcp/call`` and the SSE
stream at ``GET /mcp/subscribe``. This module wraps both as async methods
on a single :class:`MasterClient`.

The client is intentionally lightweight — no MCP SDK coupling here. When
the official MCP transport is wired in (later issue), this client gets a
new backend and the rest of the agent doesn't change.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import httpx


class MasterClientError(Exception):
    """Base for all client-side errors."""


class ConflictError(MasterClientError):
    """Raised when master returns 409. Carries the conflict payload."""

    def __init__(self, current_version: int, current_content: str, last_writer: str) -> None:
        super().__init__(
            f"Conflict: master version is {current_version}, last writer {last_writer}"
        )
        self.current_version = current_version
        self.current_content = current_content
        self.last_writer = last_writer


class NotFoundError(MasterClientError):
    """Raised when master returns 404."""


class ValidationError(MasterClientError):
    """Raised when master returns 422."""


class MasterClient:
    """Async HTTP client; one per agent process."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout_seconds: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {token}"} if token else None
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=10.0),
            headers=headers,
        )

    async def close(self) -> None:
        await self._client.aclose()

    # ---------- tool calls ----------

    async def read(self, path: str) -> dict[str, Any]:
        return await self._call("wiki.read", {"path": path})

    async def write(
        self,
        *,
        path: str,
        content: str,
        base_version: int | None,
        session: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._call(
            "wiki.write",
            {
                "path": path,
                "content": content,
                "base_version": base_version,
                "session": session,
            },
        )

    async def list_pages(
        self,
        *,
        prefix: str | None = None,
        type_filter: str | None = None,
        updated_since: datetime | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"limit": limit}
        if prefix:
            args["prefix"] = prefix
        if type_filter:
            args["type"] = type_filter
        if updated_since:
            args["updated_since"] = updated_since.isoformat()
        if cursor:
            args["cursor"] = cursor
        return await self._call("wiki.list", args)

    async def append_log(self, entry: dict[str, Any]) -> dict[str, Any]:
        return await self._call("wiki.append_log", entry)

    async def search(
        self,
        query: str,
        *,
        prefix: str | None = None,
        type_filter: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"query": query, "limit": limit}
        if prefix:
            args["prefix"] = prefix
        if type_filter:
            args["type"] = type_filter
        return await self._call("wiki.search", args)

    async def audit(
        self,
        *,
        path: str | None = None,
        since: datetime | None = None,
        server_id: str | None = None,
        app: str | None = None,
        operation: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"limit": limit}
        if path is not None:
            args["path"] = path
        if since is not None:
            args["since"] = since.isoformat()
        if server_id is not None:
            args["server_id"] = server_id
        if app is not None:
            args["app"] = app
        if operation is not None:
            args["operation"] = operation
        return await self._call("wiki.audit", args)

    # ---------- subscribe (SSE) ----------

    async def subscribe(
        self,
        *,
        since_global_version: int | None = None,
        prefix: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield events from master's SSE stream until the connection drops."""
        params: dict[str, str] = {}
        if since_global_version is not None:
            params["since_global_version"] = str(since_global_version)
        if prefix:
            params["prefix"] = prefix

        url = f"{self.base_url}/mcp/subscribe"
        async with self._client.stream("GET", url, params=params) as resp:
            resp.raise_for_status()
            async for chunk in _parse_sse(resp):
                yield chunk

    # ---------- internals ----------

    async def _call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}/mcp/call"
        try:
            resp = await self._client.post(url, json={"tool": tool, "args": args})
        except httpx.HTTPError as exc:
            raise MasterClientError(f"HTTP error: {exc}") from exc

        if resp.status_code == 200:
            data = resp.json()
            assert isinstance(data, dict)
            return data
        if resp.status_code == 404:
            raise NotFoundError(resp.json().get("detail", "not found"))
        if resp.status_code == 422:
            raise ValidationError(resp.text)
        if resp.status_code == 409:
            detail = resp.json().get("detail", {})
            raise ConflictError(
                current_version=detail.get("current_version", 0),
                current_content=detail.get("current_content", ""),
                last_writer=detail.get("last_writer", ""),
            )
        raise MasterClientError(f"Unexpected {resp.status_code}: {resp.text}")


async def _parse_sse(resp: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """Parse text/event-stream chunks into event dicts."""
    buf = ""
    async for chunk in resp.aiter_text():
        buf += chunk
        while "\n\n" in buf:
            block, buf = buf.split("\n\n", 1)
            event_type: str | None = None
            data_lines: list[str] = []
            for line in block.splitlines():
                if line.startswith("event:"):
                    event_type = line[len("event:") :].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[len("data:") :].strip())
                # ignore "id:", "retry:", etc.
            if not data_lines:
                continue
            try:
                payload = json.loads("\n".join(data_lines))
            except json.JSONDecodeError:
                continue
            if event_type and "event_type" not in payload:
                payload["event_type"] = event_type
            yield payload
