"""Embedding providers shared by application and MCP retrieval paths."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, replace
from typing import Awaitable, Callable, Literal

import httpx
from openai import AsyncOpenAI


EmbeddingPurpose = Literal["document", "query"]
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "nomic-embed-text:latest"
DEFAULT_OLLAMA_DIMENSIONS = 768
DEFAULT_OPENAI_MODEL = "text-embedding-3-small"
DEFAULT_OPENAI_DIMENSIONS = 256


class EmbeddingProviderError(RuntimeError):
    """Raised when an embedding provider is unavailable or returns invalid data."""


def normalize_ollama_base_url(value: str) -> str:
    url = str(value or DEFAULT_OLLAMA_URL).strip().rstrip("/")
    for suffix in ("/v1", "/api"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    if not url.startswith(("http://", "https://")):
        raise EmbeddingProviderError("Ollama URL must start with http:// or https://")
    return url


@dataclass(frozen=True)
class EmbeddingProfile:
    provider: Literal["ollama", "openai"]
    model: str
    dimensions: int
    base_url: str = ""
    digest: str = ""

    @property
    def fingerprint(self) -> str:
        payload = json.dumps({
            "provider": self.provider,
            "model": self.model,
            "dimensions": self.dimensions,
            "base_url": self.base_url,
            "digest": self.digest,
            "prefix_version": 1 if self.provider == "ollama" else 0,
        }, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    @property
    def locality(self) -> str:
        if self.provider == "openai":
            return "cloud"
        host = httpx.URL(self.base_url).host
        return "local" if host in {"127.0.0.1", "localhost", "::1"} else "remote"

    def public(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "model": self.model,
            "dimensions": self.dimensions,
            "base_url": self.base_url,
            "digest": self.digest,
            "fingerprint": self.fingerprint,
            "locality": self.locality,
        }


def profile_from_project(project: dict) -> EmbeddingProfile:
    provider = str(project.get("rag_embedding_provider") or "ollama").strip().lower()
    if provider == "openai":
        return EmbeddingProfile(
            "openai",
            str(project.get("rag_embedding_model") or DEFAULT_OPENAI_MODEL).strip(),
            int(project.get("rag_embedding_dimensions") or DEFAULT_OPENAI_DIMENSIONS),
        )
    if provider != "ollama":
        raise EmbeddingProviderError(f"Unsupported embedding provider: {provider}")
    return EmbeddingProfile(
        "ollama",
        str(project.get("rag_embedding_model") or DEFAULT_OLLAMA_MODEL).strip(),
        int(project.get("rag_embedding_dimensions") or DEFAULT_OLLAMA_DIMENSIONS),
        normalize_ollama_base_url(str(project.get("rag_embedding_base_url") or DEFAULT_OLLAMA_URL)),
    )


class EmbeddingService:
    """Resolve provider identity and create purpose-specific embeddings."""

    def __init__(
        self,
        openai_key: Callable[[], str | None],
        ensure_local_ollama: Callable[[EmbeddingProfile], Awaitable[bool]] | None = None,
    ):
        self._openai_key = openai_key
        self._ensure_local_ollama = ensure_local_ollama

    async def resolve(self, profile: EmbeddingProfile) -> EmbeddingProfile:
        if profile.provider == "openai":
            if not self._openai_key():
                raise EmbeddingProviderError("Connect an OpenAI API key in Settings → Accounts")
            return replace(profile, base_url="", digest="")
        try:
            tags_payload, payload = await self._inspect_ollama(profile)
        except httpx.ConnectError as first_error:
            started = bool(
                self._ensure_local_ollama
                and profile.locality == "local"
                and await self._ensure_local_ollama(profile)
            )
            if not started:
                raise EmbeddingProviderError(
                    f"Cannot connect to Ollama at {profile.base_url}. Start Ollama and try again."
                ) from first_error
            try:
                tags_payload, payload = await self._inspect_ollama(profile)
            except httpx.ConnectError as exc:
                raise EmbeddingProviderError(
                    f"Ollama was started but is not responding at {profile.base_url}."
                ) from exc
            except httpx.HTTPStatusError as exc:
                detail = self._error_detail(exc.response)
                raise EmbeddingProviderError(
                    f"Ollama model {profile.model!r} is unavailable: {detail}. "
                    f"Run `ollama pull {profile.model}`."
                ) from exc
            except (httpx.HTTPError, ValueError) as exc:
                raise EmbeddingProviderError(
                    f"Could not inspect Ollama model {profile.model!r}: {exc}"
                ) from exc
        except httpx.HTTPStatusError as exc:
            detail = self._error_detail(exc.response)
            raise EmbeddingProviderError(
                f"Ollama model {profile.model!r} is unavailable: {detail}. Run `ollama pull {profile.model}`."
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise EmbeddingProviderError(f"Could not inspect Ollama model {profile.model!r}: {exc}") from exc
        info = payload.get("model_info") if isinstance(payload, dict) else {}
        dimensions = self._metadata_int(info, "embedding_length") or profile.dimensions
        digest = self._model_digest(tags_payload, profile.model)
        capabilities = payload.get("capabilities", []) if isinstance(payload, dict) else []
        if capabilities and "embedding" not in capabilities:
            raise EmbeddingProviderError(f"Ollama model {profile.model!r} does not support embeddings")
        return replace(profile, dimensions=dimensions, digest=digest)

    @staticmethod
    async def _inspect_ollama(profile: EmbeddingProfile) -> tuple[object, object]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=3.0)) as client:
            tags_response = await client.get(f"{profile.base_url}/api/tags")
            tags_response.raise_for_status()
            tags_payload = tags_response.json()
            response = await client.post(
                f"{profile.base_url}/api/show", json={"model": profile.model},
            )
            response.raise_for_status()
            return tags_payload, response.json()

    async def preflight(self, profile: EmbeddingProfile) -> tuple[EmbeddingProfile, list[float]]:
        resolved = await self.resolve(profile)
        vectors = await self.embed(["embedding readiness check"], "query", resolved)
        return resolved, vectors[0]

    async def embed(
        self, texts: list[str], purpose: EmbeddingPurpose, profile: EmbeddingProfile,
    ) -> list[list[float]]:
        if not texts:
            return []
        if profile.provider == "openai":
            vectors = await self._embed_openai(texts, profile)
        else:
            prefix = "search_document: " if purpose == "document" else "search_query: "
            vectors = await self._embed_ollama([prefix + text for text in texts], profile)
        self._validate(vectors, len(texts), profile.dimensions)
        return vectors

    async def _embed_openai(self, texts: list[str], profile: EmbeddingProfile) -> list[list[float]]:
        key = self._openai_key()
        if not key:
            raise EmbeddingProviderError("Connect an OpenAI API key in Settings → Accounts")
        try:
            async with AsyncOpenAI(api_key=key) as client:
                response = await client.embeddings.create(
                    model=profile.model, input=texts, dimensions=profile.dimensions,
                    encoding_format="float",
                )
        except Exception as exc:
            raise EmbeddingProviderError(f"OpenAI embedding request failed: {exc}") from exc
        return [
            [float(value) for value in item.embedding]
            for item in sorted(response.data, key=lambda item: item.index)
        ]

    async def _embed_ollama(self, texts: list[str], profile: EmbeddingProfile) -> list[list[float]]:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=3.0)) as client:
                response = await client.post(
                    f"{profile.base_url}/api/embed",
                    json={"model": profile.model, "input": texts, "truncate": False},
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.ConnectError as exc:
            raise EmbeddingProviderError(
                f"Cannot connect to Ollama at {profile.base_url}. Start Ollama and try again."
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise EmbeddingProviderError(
                f"Ollama embedding failed: {self._error_detail(exc.response)}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise EmbeddingProviderError(f"Ollama embedding failed: {exc}") from exc
        values = payload.get("embeddings") if isinstance(payload, dict) else None
        if not isinstance(values, list):
            raise EmbeddingProviderError("Ollama returned no embeddings")
        return [[float(value) for value in vector] for vector in values]

    @staticmethod
    def _metadata_int(info: object, suffix: str) -> int:
        if not isinstance(info, dict):
            return 0
        for key, value in info.items():
            if str(key).endswith(suffix):
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return 0
        return 0

    @staticmethod
    def _model_digest(payload: object, model: str) -> str:
        if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
            return ""
        requested = model if ":" in model else f"{model}:latest"
        for item in payload["models"]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("model") or "")
            if name == requested or name == model:
                return str(item.get("digest") or "")
        return ""

    @staticmethod
    def _error_detail(response: httpx.Response) -> str:
        try:
            payload = response.json()
            return str(payload.get("error") or payload.get("detail") or response.text)[:500]
        except ValueError:
            return response.text.strip()[:500] or f"HTTP {response.status_code}"

    @staticmethod
    def _validate(vectors: list[list[float]], expected_count: int, dimensions: int) -> None:
        if len(vectors) != expected_count:
            raise EmbeddingProviderError(
                f"Embedding provider returned {len(vectors)} vectors; expected {expected_count}"
            )
        for vector in vectors:
            if len(vector) != dimensions or not all(math.isfinite(float(value)) for value in vector):
                raise EmbeddingProviderError(
                    f"Embedding must contain {dimensions} finite values; received {len(vector)}"
                )
