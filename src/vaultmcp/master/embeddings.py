"""Embedding worker + providers.

Each ``wiki.write`` enqueues one row in ``embedding_jobs`` (inside the
same transaction as the page insert). The worker polls the queue,
computes an embedding via the configured provider, upserts into the
``embeddings`` table, and deletes the job.

Providers
---------

This module ships exactly one provider out of the box:

- :class:`NullEmbeddingProvider` — deterministic SHA-256-derived vector.
  Used in tests and as a sensible default when no real embedder is
  configured. Cosine-self-similarity is 1.0; cosine across different
  pages is unrelated to semantic similarity (it only proves the
  pipeline works).

A real embedder (OpenAI, local Sentence-Transformers, etc.) plugs in
by implementing :class:`EmbeddingProvider`. We intentionally don't
ship one yet because picking the model + API-key story belongs in a
deployment commit, not the worker scaffolding.

Lifecycle
---------

The worker is started inside the master's FastAPI lifespan when
``MasterConfig.embedding_provider`` is set, and stopped on shutdown.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from typing import Final, Protocol

from .db import Database

LOG = logging.getLogger("vaultmcp.master.embeddings")


# =============================================================
# Provider protocol + implementations
# =============================================================


class EmbeddingProvider(Protocol):
    """Async producer of fixed-dimension embeddings.

    The protocol is intentionally tiny — one async ``embed(text)``
    method plus two metadata properties — so test fakes and real
    HTTP/gRPC clients alike can satisfy it.
    """

    name: str
    dim: int

    async def embed(self, text: str) -> list[float]:
        ...


@dataclass(frozen=True)
class NullEmbeddingProvider:
    """Deterministic SHA-256-derived vectors.

    ``embed(t1) == embed(t2)`` iff ``t1 == t2``. Vectors are unit-norm
    so cosine distance maps to angle. Useful as a default and in tests
    that need *some* embedding to flow through the pipeline; not useful
    for actual semantic search.
    """

    name: str = "null/sha-1536"
    dim: int = 1536

    async def embed(self, text: str) -> list[float]:
        out: list[float] = []
        seed = text.encode("utf-8")
        counter = 0
        while len(out) < self.dim:
            h = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
            for k in range(0, len(h), 2):
                if len(out) >= self.dim:
                    break
                # Two bytes -> signed-ish float in [-1, 1].
                raw = int.from_bytes(h[k : k + 2], "big")
                out.append(raw / 32767.5 - 1.0)
            counter += 1
        # Unit-normalize.
        norm = sum(x * x for x in out) ** 0.5
        if norm > 0:
            out = [x / norm for x in out]
        return out


# Registry for ``MasterConfig.embedding_provider`` strings -> class.
_PROVIDERS: Final[dict[str, type]] = {
    "null/sha-1536": NullEmbeddingProvider,
}


def get_provider(name: str) -> EmbeddingProvider:
    """Resolve a provider by string. Raises :class:`KeyError` if unknown."""
    cls = _PROVIDERS[name]
    instance = cls()
    return instance  # type: ignore[return-value]


# =============================================================
# Worker
# =============================================================


class EmbeddingWorker:
    """Polls ``embedding_jobs`` and writes results into ``embeddings``."""

    POLL_INTERVAL_SECONDS: Final[float] = 1.0
    MAX_ATTEMPTS: Final[int] = 5

    def __init__(self, *, db: Database, provider: EmbeddingProvider) -> None:
        self.db = db
        self.provider = provider
        self._stopping = False
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="embedding-worker")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self) -> None:
        while not self._stopping:
            try:
                handled = await self._drain_one()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("embedding worker loop crashed; backing off")
                handled = False
            if not handled:
                # Idle: wait a bit before polling again. Cancellable.
                try:
                    await asyncio.sleep(self.POLL_INTERVAL_SECONDS)
                except asyncio.CancelledError:
                    raise

    async def _drain_one(self) -> bool:
        """Process one pending job; return True iff anything was done."""
        job = await self.db.pop_pending_embedding_job(max_attempts=self.MAX_ATTEMPTS)
        if job is None:
            return False
        job_id, path, version, content = job
        try:
            vector = await self.provider.embed(content or "")
            if len(vector) != self.provider.dim:
                raise RuntimeError(
                    f"provider {self.provider.name!r} returned {len(vector)} dims, "
                    f"expected {self.provider.dim}"
                )
            await self.db.complete_embedding_job(
                job_id=job_id,
                path=path,
                version=version,
                model=self.provider.name,
                dim=self.provider.dim,
                vector=vector,
            )
            LOG.debug("Embedded %s @ v%d via %s", path, version, self.provider.name)
        except Exception as exc:
            LOG.warning("Embedding %s @ v%d failed: %s", path, version, exc)
            await self.db.fail_embedding_job(job_id=job_id, error=str(exc)[:500])
        return True
