"""Unit tests for the embedding provider primitives."""

from __future__ import annotations

import math

import pytest

from vaultmcp.master.embeddings import NullEmbeddingProvider, get_provider


@pytest.mark.asyncio
async def test_null_embedding_is_deterministic() -> None:
    p = NullEmbeddingProvider()
    a = await p.embed("hello world")
    b = await p.embed("hello world")
    assert a == b


@pytest.mark.asyncio
async def test_null_embedding_returns_correct_dimension() -> None:
    p = NullEmbeddingProvider()
    v = await p.embed("anything")
    assert len(v) == p.dim == 1536


@pytest.mark.asyncio
async def test_null_embedding_is_unit_norm() -> None:
    p = NullEmbeddingProvider()
    v = await p.embed("hello world")
    norm = math.sqrt(sum(x * x for x in v))
    assert math.isclose(norm, 1.0, abs_tol=1e-6)


@pytest.mark.asyncio
async def test_different_inputs_yield_different_vectors() -> None:
    p = NullEmbeddingProvider()
    a = await p.embed("alpha")
    b = await p.embed("beta")
    assert a != b
    # And they're not trivially close (cosine < 0.99 is plenty for the
    # SHA-derived vectors — anything closer would be a hashing bug).
    cos = sum(x * y for x, y in zip(a, b, strict=True))
    assert cos < 0.99


def test_get_provider_resolves_known_name() -> None:
    p = get_provider("null/sha-1536")
    assert isinstance(p, NullEmbeddingProvider)


def test_get_provider_raises_on_unknown_name() -> None:
    with pytest.raises(KeyError):
        get_provider("not-a-real-provider")
