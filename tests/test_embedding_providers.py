import asyncio
import json

import httpx
import pytest

from embedding_providers import (
    EmbeddingProfile,
    EmbeddingProviderError,
    EmbeddingService,
    normalize_ollama_base_url,
)


def test_ollama_preflight_resolves_digest_dimensions_and_applies_query_prefix(monkeypatch):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            requests.append((request.url.path, {}))
            return httpx.Response(200, json={"models": [{
                "name": "nomic-embed-text:latest", "digest": "sha256:model-v1",
            }]})
        payload = json.loads(request.content)
        requests.append((request.url.path, payload))
        if request.url.path == "/api/show":
            return httpx.Response(200, json={
                "capabilities": ["embedding"],
                "model_info": {"nomic-bert.embedding_length": 768},
            })
        return httpx.Response(200, json={"embeddings": [[0.0] * 768]})

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs),
    )
    service = EmbeddingService(lambda: None)
    configured = EmbeddingProfile(
        "ollama", "nomic-embed-text:latest", 256, "http://127.0.0.1:11434",
    )

    resolved, vector = asyncio.run(service.preflight(configured))

    assert resolved.dimensions == 768
    assert resolved.digest == "sha256:model-v1"
    assert len(vector) == 768
    assert requests[2][0] == "/api/embed"
    assert requests[2][1]["input"] == ["search_query: embedding readiness check"]
    assert requests[2][1]["truncate"] is False


def test_ollama_document_prefix_and_invalid_vector_are_rejected(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"embeddings": [[1.0, 2.0]]})

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs),
    )
    service = EmbeddingService(lambda: None)
    profile = EmbeddingProfile("ollama", "nomic-embed-text:latest", 768, "http://localhost:11434")

    with pytest.raises(EmbeddingProviderError, match="768 finite values"):
        asyncio.run(service.embed(["File: app.py"], "document", profile))
    assert captured["input"] == ["search_document: File: app.py"]


def test_ollama_error_is_actionable_and_url_is_normalized(monkeypatch):
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "GGML_ASSERT(buf_dst) failed"})

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs),
    )
    service = EmbeddingService(lambda: None)
    profile = EmbeddingProfile("ollama", "nomic-embed-text:latest", 768, "http://localhost:11434")

    with pytest.raises(EmbeddingProviderError, match="GGML_ASSERT"):
        asyncio.run(service.embed(["hello"], "query", profile))
    assert normalize_ollama_base_url("http://localhost:11434/v1/") == "http://localhost:11434"


def test_local_ollama_is_started_and_retried_when_initially_offline(monkeypatch):
    started = False

    async def ensure_local(_profile):
        nonlocal started
        started = True
        return True

    def handler(request: httpx.Request) -> httpx.Response:
        if not started:
            raise httpx.ConnectError("offline", request=request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{
                "name": "nomic-embed-text:latest", "digest": "sha256:ready",
            }]})
        return httpx.Response(200, json={
            "capabilities": ["embedding"],
            "model_info": {"nomic-bert.embedding_length": 768},
        })

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs),
    )
    service = EmbeddingService(lambda: None, ensure_local)
    profile = EmbeddingProfile("ollama", "nomic-embed-text:latest", 768, "http://localhost:11434")

    resolved = asyncio.run(service.resolve(profile))

    assert started is True
    assert resolved.digest == "sha256:ready"
