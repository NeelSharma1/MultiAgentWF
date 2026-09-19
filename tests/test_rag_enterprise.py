import asyncio
import hashlib
import subprocess

from embedding_providers import EmbeddingProfile
from rag import RagStore, structured_chunk_text
from rag_enterprise import (
    GitConnector, GroundingVerifier, PrincipalResolver, RagEnterpriseStore,
    RagSecurityPolicy, RequestPrincipal, SourceDocument, deterministic_rerank,
    diversify_results,
)
from rag_eval import evaluate
from rag_runtime import LocalRagIndexCoordinator


def test_structured_chunking_preserves_python_symbols_and_markdown_sections():
    python_chunks = structured_chunk_text("import os\n\ndef build():\n    return 1\n", "app.py")
    markdown_chunks = structured_chunk_text("# Intro\nHello\n## Setup\nInstall it\n", "README.md")
    assert any(chunk.symbol == "build" and chunk.start_line == 3 for chunk in python_chunks)
    assert [chunk.symbol for chunk in markdown_chunks] == ["Intro", "Setup"]


def test_security_policy_detects_content_independent_of_filename():
    policy = RagSecurityPolicy()
    content, findings = policy.apply("notes\napi_key = abcdefghijklmnop\n")
    assert content == ""
    assert findings[0].category == "password_assignment"


def test_principal_resolution_and_fail_closed_acl(monkeypatch):
    monkeypatch.setenv("RAG_ENTERPRISE_MODE", "0")
    principal = PrincipalResolver().resolve({}, "acme")
    assert principal.tenant_id == "acme"
    assert RagSecurityPolicy.authorized(principal, "acme", ["group:project-members"])
    assert not RagSecurityPolicy.authorized(principal, "other", ["group:project-members"])


def test_control_plane_sources_traces_metrics_retention_and_receipts(tmp_path):
    store = RagEnterpriseStore(tmp_path / "workspace.db")
    source = store.create_source("acme", 7, "git", "Handbook", {"repository": "/repo"})
    source = store.update_source(source["id"], "acme", name="Runbook", config={"repository": "/new"})
    assert source["name"] == "Runbook" and source["config"]["repository"] == "/new"
    principal = RequestPrincipal("acme", "u1", ("eng",))
    trace_id = store.start_trace("acme", 7, "researcher", principal, "auth", {"limit": 8})
    claims = GroundingVerifier.verify("Auth is in auth.py [S1].", [
        {"source_id": "S1", "path": "auth.py", "excerpt": "auth implementation"},
    ])
    store.finish_trace(trace_id, "acme", status="validated", results=[{"path": "auth.py"}],
                       claims=claims, timings={"retrieval_ms": 12.5})
    assert store.metrics("acme", 7)["trace_count"] == 1
    assert store.set_retention("acme", 7, trace_days=10)["trace_days"] == 10
    assert store.delete_source(source["id"], "acme")
    receipt = store.deletion_receipt("acme", 7, source["id"], "completed", {"chunks": 2})
    assert receipt["details"]["chunks"] == 2


def test_git_connector_reads_revision_without_checkout(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    (repository / "guide.md").write_text("# Guide\nHello\n")
    subprocess.run(["git", "add", "guide.md"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "guide"], cwd=repository, check=True)
    connector = GitConnector("docs", repository, acl_subjects=("group:eng",))
    documents = list(connector.documents())
    assert connector.validate()["ok"]
    assert documents[0].path == "guide.md"
    assert documents[0].acl_subjects == ("group:eng",)


def test_connector_documents_are_searchable_with_acl(tmp_path):
    store = RagStore(tmp_path / "rag.db")
    profile = EmbeddingProfile("ollama", "test", 3, "http://local")
    document = SourceDocument(
        "docs", "git://docs/guide.md", "guide.md", "abc", "authentication guide",
        hashlib.sha256(b"authentication guide").hexdigest(), acl_subjects=("group:eng",),
    )

    async def embed(texts):
        return [[1.0, 0.0, 0.0] for _ in texts]

    asyncio.run(store.index_source_documents(1, "docs", [document], embed, profile=profile))
    allowed = asyncio.run(store.search(
        tmp_path, 1, "authentication", embed, profile=profile, allowed_subjects=("group:eng",),
    ))
    denied = asyncio.run(store.search(
        tmp_path, 1, "authentication", embed, profile=profile, allowed_subjects=("group:sales",),
    ))
    assert allowed[0]["connector_source_id"] == "docs"
    assert denied == []


def test_rerank_and_diversity_limit_repeated_paths():
    results = [
        {"id": "a", "path": "a.py", "start_line": 1, "end_line": 3,
         "excerpt": "auth token", "scores": {"fused": .01}},
        {"id": "b", "path": "a.py", "start_line": 2, "end_line": 4,
         "excerpt": "auth", "scores": {"fused": .02}},
        {"id": "c", "path": "b.py", "start_line": 1, "end_line": 2,
         "excerpt": "auth", "scores": {"fused": .01}},
    ]
    selected = diversify_results(deterministic_rerank("auth", results), 3)
    assert len(selected) == 2
    assert [item["source_id"] for item in selected] == ["S1", "S2"]


def test_background_coordinator_deduplicates_and_runs_jobs(tmp_path):
    store = RagStore(tmp_path / "rag.db")
    calls = []

    async def indexer(project_id, **kwargs):
        calls.append((project_id, kwargs["job_id"]))
        store._update_job(kwargs["job_id"], status="completed")
        return {"ok": True}

    coordinator = LocalRagIndexCoordinator(store, indexer, lambda: [], lambda _project: tmp_path)

    async def run():
        await coordinator.start()
        first = await coordinator.schedule(1)
        second = await coordinator.schedule(1)
        await asyncio.wait_for(coordinator.queue.join(), 2)
        await coordinator.stop()
        return first, second

    first, second = asyncio.run(run())
    assert first["id"] == second["id"]
    assert len(calls) == 1


def test_eval_harness_reports_recall_and_mrr():
    async def retrieve(_query, _limit):
        return [{"source_id": "S1", "path": "target.py", "excerpt": "target behavior"}]

    report = asyncio.run(evaluate([
        {"query": "target", "expected_paths": ["target.py"], "answer": "Target behavior [S1]."},
    ], retrieve, limit=8))
    assert report["recall_at_8"] == 1
    assert report["mean_reciprocal_rank"] == 1
