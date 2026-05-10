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
from dataclasses import dataclass, field
from typing import Final, Protocol

import httpx

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


@dataclass
class OpenAICompatibleEmbeddingProvider:
    """Embeddings via the OpenAI-style ``POST /embeddings`` shape.

    Works with:

    - OpenAI directly (``base_url=https://api.openai.com/v1``).
    - litellm proxy (any base URL exposing an OpenAI-compatible
      ``/embeddings`` endpoint).
    - Self-hosted gateways that mimic the same shape (vLLM,
      LocalAI, etc.).

    The same ``EmbeddingProvider`` is used both at write time (worker)
    and at query time (semantic / hybrid search), so swapping models
    later is a config change.
    """

    base_url: str
    model: str
    api_key: str | None = None
    dim: int = 1536
    timeout_seconds: float = 30.0
    name: str = field(init=False)
    _client: httpx.AsyncClient | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        # Surface the model name in audit / dashboard rows so it's
        # obvious which embedder a given vector came from.
        self.name = f"openai-compat/{self.model}"

    async def embed(self, text: str) -> list[float]:
        client = self._get_client()
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        url = f"{self.base_url.rstrip('/')}/embeddings"
        resp = await client.post(
            url,
            headers=headers,
            json={"model": self.model, "input": text},
        )
        resp.raise_for_status()
        payload = resp.json()
        try:
            vec = payload["data"][0]["embedding"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                f"Unexpected embeddings response shape from {url}: {payload!r}"
            ) from exc

        if len(vec) != self.dim:
            raise RuntimeError(
                f"Model {self.model!r} returned {len(vec)}-d vectors, "
                f"expected {self.dim}. Either pick a model with that "
                "dimension, or update the embeddings.embedding column "
                "(and the schema constant) and rebuild."
            )
        return [float(x) for x in vec]

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_seconds, connect=10.0)
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# Lookup table for the provider-name string in ``MasterConfig``.
# Each entry decides what `get_provider(config)` returns; some entries
# instantiate without arguments (Null), others read additional config
# fields (OpenAI-compatible).
_NULL_PROVIDER_NAME: Final[str] = "null/sha-1536"
_OPENAI_COMPAT_NAME: Final[str] = "openai-compat"


def get_provider_from_name(name: str) -> EmbeddingProvider:
    """Resolve a parameter-less provider by name (back-compat for tests)."""
    if name == _NULL_PROVIDER_NAME:
        return NullEmbeddingProvider()  # type: ignore[return-value]
    raise KeyError(name)


# Kept under the old name so existing code continues to work; the
# resolver uses the slim parameter-less form. Real providers go
# through :func:`build_provider`.
get_provider = get_provider_from_name


def build_provider(
    *,
    name: str,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    dim: int = 1536,
) -> EmbeddingProvider:
    """Construct a provider from config-shaped fields.

    Centralising instantiation here keeps server.py free of
    provider-specific knobs and lets us add new providers (Cohere,
    Voyage, local Sentence-Transformers) by extending the dispatch
    below.
    """
    if name == _NULL_PROVIDER_NAME:
        return NullEmbeddingProvider()  # type: ignore[return-value]
    if name == _OPENAI_COMPAT_NAME:
        if not base_url or not model:
            raise ValueError(
                f"Provider {name!r} requires VAULTMCP_EMBEDDING_BASE_URL "
                "and VAULTMCP_EMBEDDING_MODEL"
            )
        return OpenAICompatibleEmbeddingProvider(
            base_url=base_url, model=model, api_key=api_key, dim=dim
        )
    raise KeyError(f"Unknown embedding provider: {name!r}")


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
