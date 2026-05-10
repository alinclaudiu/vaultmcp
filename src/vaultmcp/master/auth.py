"""Bearer-token authentication for the master.

A registered server holds a hashed token in ``servers.token_hash``
(SHA-256, hex). Clients send the plaintext token in the standard
``Authorization: Bearer <token>`` header. The master hashes the
incoming token and looks it up.

Auth is **bypassed when the ``servers`` table is empty** so a fresh
install can be exercised on localhost without any setup. The moment
the first server is added (via ``vaultmcp-master add-server``), auth
becomes required for every request.

Tokens are generated with :func:`secrets.token_urlsafe`. They are
displayed once at creation time and never stored in plaintext.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass


def generate_token() -> str:
    """Return a fresh URL-safe bearer token (~43 chars, ~256 bits)."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Return the canonical hex SHA-256 of ``token``.

    Same hash on both sides of the wire — ``add_server`` stores the
    output of this function and ``verify_request`` re-runs it on the
    incoming header to compare.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AuthenticatedServer:
    """The result of a successful token verification."""

    id: str
    apps: tuple[str, ...]


def parse_bearer(header: str | None) -> str | None:
    """Pull the token out of an ``Authorization: Bearer <token>`` header.

    Returns ``None`` if the header is missing or malformed; never raises.
    Whitespace around the token is stripped so multi-line/edge cases
    don't sneak through.
    """
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) != 2:
        return None
    scheme, value = parts
    if scheme.lower() != "bearer":
        return None
    token = value.strip()
    return token or None
