"""Tool-level errors with HTTP-ish status codes.

Pulled out of :mod:`vaultmcp.master.tools` so that the validation
middleware can raise these without forcing a circular import.
"""

from __future__ import annotations

from typing import Final

from ..shared.types import WriteConflict


class ToolError(Exception):
    """Base class for tool-level errors with an HTTP-ish status code."""

    status: int = 500

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        if status is not None:
            self.status = status


class NotFoundError(ToolError):
    status: Final[int] = 404


class ForbiddenError(ToolError):
    """Raised when an authenticated server tries to act as a different one,
    or to write on behalf of an app it doesn't own.
    """

    status: Final[int] = 403


class ValidationFailed(ToolError):  # noqa: N818 — kept for API compatibility
    """Frontmatter / encoding / secret-pattern violations."""

    status: Final[int] = 422


class SizeLimitExceeded(ToolError):  # noqa: N818 — pairs with ValidationFailed
    """Content exceeded the configured per-page size limit."""

    status: Final[int] = 413

    def __init__(self, *, size: int, limit: int) -> None:
        super().__init__(
            f"Content size {size} bytes exceeds limit of {limit} bytes"
        )
        self.size = size
        self.limit = limit


class ConflictError(ToolError):
    """Raised on optimistic-concurrency mismatch.

    Carries the master's current state so the agent can resync.
    """

    status: Final[int] = 409

    def __init__(self, conflict: WriteConflict) -> None:
        super().__init__(f"Version conflict; current is {conflict.current_version}")
        self.conflict = conflict
