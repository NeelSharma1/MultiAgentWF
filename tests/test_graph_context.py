import math
import sqlite3

import pytest

from graph_context import GraphContextError, GraphContextStore


def test_scan_builds_persisted_project_folder_file_graph(tmp_path):
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("def hello_world():\n    return 'hello'\n")
    (root / ".env.local").write_text("SECRET=never-index\n")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "ignored.js").write_text("ignored")
    store = GraphContextStore(tmp_path / "graph.db")

    result = store.scan(root, project_id=7)
    nodes = store.list(project_id=7)

    assert result["files"] == 1
    assert [node["path"] for node in nodes] == ["", "src", "src/app.py"]
    file_node = store.get_by_path("src/app.py", project_id=7)
    assert file_node is not None
    assert file_node["node_type"] == "file"
    assert file_node["keywords"]
    assert len(file_node["vector"]) == GraphContextStore.VECTOR_DIMENSIONS
    assert all(math.isfinite(value) for value in file_node["vector"])

    reopened = GraphContextStore(tmp_path / "graph.db")
    assert reopened.get_by_path("src/app.py", project_id=7)["content_hash"] == file_node["content_hash"]


def test_scan_rejects_unsafe_paths_and_skips_symlink_escape(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    link = root / "escape.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")
    store = GraphContextStore(tmp_path / "graph.db")

    result = store.scan(root)

    assert result["files"] == 0
    for value in ("../outside.txt", "/tmp/outside.txt", "src\\file.py", "%2e%2e/secret"):
        with pytest.raises(GraphContextError):
            GraphContextStore.normalize_path(value)
    with pytest.raises(GraphContextError):
        store.scan(tmp_path / "missing")


def test_metadata_is_deterministic_isolated_and_guarded_against_stale_updates(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "readme.md").write_text("Graph metadata metadata reliability")
    store = GraphContextStore(tmp_path / "graph.db")
    store.scan(root, project_id=1)
    store.scan(root, project_id=2)
    node = store.get_by_path("readme.md", project_id=1)
    assert node is not None

    first = GraphContextStore.fallback_metadata("Graph metadata metadata reliability")
    second = GraphContextStore.fallback_metadata("Graph metadata metadata reliability")
    assert first == second
    assert store.apply_generated_metadata(node["id"], 1, "stale", first) is None
    updated = store.apply_generated_metadata(node["id"], 1, node["content_hash"], first)
    assert updated is not None
    assert store.list(project_id=2)
    store.delete_project(1)
    assert not store.list(project_id=1)
    assert store.list(project_id=2)


def test_scan_preserves_generated_metadata_for_unchanged_content(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "readme.md").write_text("stable graph context")
    store = GraphContextStore(tmp_path / "graph.db")
    store.scan(root)
    node = store.get_by_path("readme.md")
    assert node is not None

    generated = {"keywords": ["generated", "metadata"], "vector": [0.1] * 16, "source": "provider", "model": "test"}
    assert store.apply_generated_metadata(node["id"], 1, node["content_hash"], generated)
    store.scan(root)

    refreshed = store.get_by_path("readme.md")
    assert refreshed["keywords"] == ["generated", "metadata"]
    assert refreshed["source"] == "provider"
    assert refreshed["model"] == "test"


def test_legacy_context_table_is_archived_idempotently(tmp_path):
    db_path = tmp_path / "graph.db"
    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE context_items(id INTEGER PRIMARY KEY, title TEXT)")
    db.execute("INSERT INTO context_items(title) VALUES('legacy')")
    db.commit()
    db.close()

    GraphContextStore(db_path)
    GraphContextStore(db_path)
    db = sqlite3.connect(db_path)
    names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    db.close()
    assert "context_items_legacy" in names
    assert "graph_nodes" in names
