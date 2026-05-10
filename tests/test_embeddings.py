"""Unit tests for the embedding provider primitives."""

from __future__ import annotations

import json
import math

import httpx
import pytest

from vaultmcp.master.embeddings import (
    NullEmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
    build_provider,
    get_provider,
)


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


# =============================================================
# OpenAI-compatible provider
# =============================================================


def _vec(dim: int = 1536, fill: float = 0.001) -> list[float]:
    return [fill] * dim


def _make_provider_with_transport(
    handler,
    *,
    api_key: str | None = None,
    dim: int = 1536,
) -> OpenAICompatibleEmbeddingProvider:
    """Build a provider whose internal httpx.AsyncClient routes through ``handler``.

    Lets each test inspect the outgoing request and craft a fake response
    without spinning up a real HTTP server.
    """
    transport = httpx.MockTransport(handler)
    provider = OpenAICompatibleEmbeddingProvider(
        base_url="https://litellm.example.com/v1",
        model="text-embedding-3-small",
        api_key=api_key,
        dim=dim,
    )
    # Inject the mock-transport client before the lazy one gets built.
    provider._client = httpx.AsyncClient(transport=transport)
    return provider


@pytest.mark.asyncio
async def test_openai_compat_returns_embedding_from_litellm_shape() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["json"] = json.loads(request.content)
        return httpx.Response(
            200, json={"data": [{"embedding": _vec()}], "model": "text-embedding-3-small"}
        )

    p = _make_provider_with_transport(handler)
    try:
        v = await p.embed("hello world")
    finally:
        await p.aclose()

    assert len(v) == 1536
    assert seen["method"] == "POST"
    assert seen["url"] == "https://litellm.example.com/v1/embeddings"
    assert seen["json"] == {"model": "text-embedding-3-small", "input": "hello world"}


@pytest.mark.asyncio
async def test_openai_compat_sends_bearer_token_when_configured() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"data": [{"embedding": _vec()}]})

    p = _make_provider_with_transport(handler, api_key="sk-test")
    try:
        await p.embed("x")
    finally:
        await p.aclose()
    assert seen["auth"] == "Bearer sk-test"


@pytest.mark.asyncio
async def test_openai_compat_omits_auth_header_when_no_key() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"data": [{"embedding": _vec()}]})

    p = _make_provider_with_transport(handler, api_key=None)
    try:
        await p.embed("x")
    finally:
        await p.aclose()
    assert seen["auth"] == ""


@pytest.mark.asyncio
async def test_openai_compat_raises_on_dimension_mismatch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Server returns the wrong number of dims.
        return httpx.Response(200, json={"data": [{"embedding": _vec(dim=384)}]})

    p = _make_provider_with_transport(handler, dim=1536)
    try:
        with pytest.raises(RuntimeError, match="384-d"):
            await p.embed("x")
    finally:
        await p.aclose()


@pytest.mark.asyncio
async def test_openai_compat_raises_on_unexpected_payload_shape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "no data field"})

    p = _make_provider_with_transport(handler)
    try:
        with pytest.raises(RuntimeError, match="Unexpected embeddings response shape"):
            await p.embed("x")
    finally:
        await p.aclose()


@pytest.mark.asyncio
async def test_openai_compat_propagates_http_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "Invalid API key"}})

    p = _make_provider_with_transport(handler)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await p.embed("x")
    finally:
        await p.aclose()


def test_provider_name_includes_model() -> None:
    p = OpenAICompatibleEmbeddingProvider(
        base_url="x", model="text-embedding-3-small"
    )
    assert p.name == "openai-compat/text-embedding-3-small"


# =============================================================
# build_provider dispatch
# =============================================================


def test_build_provider_returns_null_for_null_name() -> None:
    p = build_provider(name="null/sha-1536")
    assert isinstance(p, NullEmbeddingProvider)


def test_build_provider_returns_openai_compat_when_configured() -> None:
    p = build_provider(
        name="openai-compat",
        base_url="https://example.com/v1",
        model="my-model",
        api_key="sk-x",
        dim=1536,
    )
    assert isinstance(p, OpenAICompatibleEmbeddingProvider)
    assert p.name == "openai-compat/my-model"


def test_build_provider_rejects_openai_compat_without_required_fields() -> None:
    with pytest.raises(ValueError, match="VAULTMCP_EMBEDDING_BASE_URL"):
        build_provider(name="openai-compat")
    with pytest.raises(ValueError):
        build_provider(name="openai-compat", base_url="https://x")  # missing model


def test_build_provider_raises_on_unknown_name() -> None:
    with pytest.raises(KeyError, match="totally-fake"):
        build_provider(name="totally-fake")
