import asyncio
import hashlib

from rag import EMBEDDING_DIMENSIONS, RagStore, chunk_text, eligible_files
from embedding_providers import EmbeddingProfile


def embedding_for(text: str) -> list[float]:
    """Small deterministic stand-in for the provider embedding endpoint."""

    vector = [0.0] * EMBEDDING_DIMENSIONS
    lowered = text.lower()
    for token, position in (("oauth", 0), ("authentication", 0), ("invoice", 1), ("billing", 1)):
        if token in lowered:
            vector[position] += 1.0
    if not any(vector):
        vector[int(hashlib.sha256(text.encode()).hexdigest()[:4], 16) % EMBEDDING_DIMENSIONS] = 1.0
    return vector


async def fake_embed(texts: list[str]) -> list[list[float]]:
    return [embedding_for(text) for text in texts]


def test_eligible_files_excludes_secrets_ignored_binary_large_and_symlinks(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / ".gitignore").write_text("ignored.txt\nignored-dir/\n", encoding="utf-8")
    (root / ".ragignore").write_text("!ignored.txt\n", encoding="utf-8")
    (root / "app.py").write_text("print('safe')\n", encoding="utf-8")
    (root / ".env.local").write_text("TOKEN=secret\n", encoding="utf-8")
    (root / "ignored.txt").write_text("ignored\n", encoding="utf-8")
    (root / "ignored-dir").mkdir()
    (root / "ignored-dir" / "nested.py").write_text("ignored\n", encoding="utf-8")
    (root / "binary.dat").write_bytes(b"text\0binary")
    (root / "large.txt").write_bytes(b"x" * 128_001)
    (root / "node_modules").mkdir()
    (root / "node_modules" / "package.js").write_text("ignored\n", encoding="utf-8")
    (root / "maw").mkdir()
    (root / "maw" / "workspace-notes.md").write_text("internal state\n", encoding="utf-8")
    try:
        (root / "escape.txt").symlink_to(tmp_path / "outside.txt")
    except OSError:
        pass

    paths = [path.relative_to(root).as_posix() for path in eligible_files(root)]

    assert paths == [".gitignore", ".ragignore", "app.py"]


def test_hybrid_search_is_incremental_line_addressable_and_rejects_stale_chunks(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    source = root / "auth.py"
    source.write_text(
        "def authenticate(token):\n    # OAuth authentication checks the bearer token.\n    return bool(token)\n",
        encoding="utf-8",
    )
    (root / "billing.py").write_text("def invoice():\n    return 'billing invoice'\n", encoding="utf-8")
    store = RagStore(tmp_path / "rag.db")

    first = asyncio.run(store.index_project(root, 9, fake_embed))
    unchanged = asyncio.run(store.index_project(root, 9, fake_embed))
    results = asyncio.run(store.search(root, 9, "How does OAuth authentication work?", fake_embed))

    assert first["changed"] == 2
    assert first["embedded_chunks"] == 2
    assert unchanged["changed"] == 0
    assert results[0]["path"] == "auth.py"
    assert results[0]["source_id"] == "S1"
    assert results[0]["start_line"] == 1
    assert results[0]["end_line"] == 3
    assert results[0]["fresh"] is True

    source.write_text("def authenticate(token):\n    return False\n", encoding="utf-8")
    stale_filtered = asyncio.run(store.search(root, 9, "OAuth authentication", fake_embed))
    assert all(item["path"] != "auth.py" for item in stale_filtered)

    refreshed = asyncio.run(store.index_project(root, 9, fake_embed))
    assert refreshed["changed"] == 1
    assert store.documents(9)[0]["content_hash"]


def test_chunks_overlap_and_preserve_source_lines(monkeypatch):
    monkeypatch.setattr("rag.CHUNK_TOKENS", 10)
    monkeypatch.setattr("rag.CHUNK_OVERLAP_TOKENS", 3)
    chunks = chunk_text("one two three\nfour five six\nseven eight nine\nten eleven twelve\n")

    assert len(chunks) >= 2
    assert chunks[0].start_line == 1
    assert chunks[1].start_line <= chunks[0].end_line
    assert all(chunk.start_line <= chunk.end_line for chunk in chunks)

    long_line = chunk_text("token " * 2_000)
    assert len(long_line) > 1
    assert all(chunk.token_count <= 10 for chunk in long_line)
    assert all(chunk.start_line == chunk.end_line == 1 for chunk in long_line)


def test_embedding_dimensions_are_rejected_before_persistence(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "app.py").write_text("print('hello')\n", encoding="utf-8")
    store = RagStore(tmp_path / "rag.db")

    async def invalid_embed(texts):
        return [[1.0, 2.0] for _ in texts]

    result = asyncio.run(store.index_project(root, 1, invalid_embed))

    assert result["ok"] is False
    assert store.documents(1)[0]["status"] == "error"
    assert asyncio.run(store.search(root, 1, "hello", fake_embed)) == []


def test_embedding_profile_fingerprint_forces_reindex_and_isolates_search(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "app.py").write_text("OAuth authentication\n", encoding="utf-8")
    store = RagStore(tmp_path / "rag.db")
    first_profile = EmbeddingProfile("ollama", "nomic-embed-text:latest", 4, "http://localhost:11434", "v1")
    second_profile = EmbeddingProfile("ollama", "nomic-embed-text:latest", 4, "http://localhost:11434", "v2")

    async def four_dimensions(texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    first = asyncio.run(store.index_project(root, 3, four_dimensions, profile=first_profile))
    incompatible = asyncio.run(store.search(
        root, 3, "OAuth", four_dimensions, profile=second_profile,
    ))
    refreshed = asyncio.run(store.index_project(root, 3, four_dimensions, profile=second_profile))
    results = asyncio.run(store.search(root, 3, "OAuth", four_dimensions, profile=second_profile))

    assert first["changed"] == 1
    assert incompatible == []
    assert refreshed["changed"] == 1
    assert results[0]["path"] == "app.py"
