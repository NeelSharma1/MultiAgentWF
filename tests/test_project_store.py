import pytest

from project_store import ProjectStore


def test_projects_and_agent_layout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = ProjectStore(tmp_path / "projects.db")
    project = store.create("Apollo", "Moonshot", "~/code/apollo")
    assert project["name"] == "Apollo"
    layout = store.layout(project["id"], ["orchestrator", "researcher"])
    saved = store.save_layout(project["id"], [
        {"role": "researcher", "x": 440, "y": 220}
    ], ["orchestrator", "researcher"])
    assert next(item for item in saved if item["role"] == "researcher")["x"] == 440
    edges = store.save_edges(project["id"], [
        {"source_role": "orchestrator", "target_role": "researcher", "relationship": "command"},
        {"source_role": "researcher", "target_role": "orchestrator", "relationship": "report"},
    ], ["orchestrator", "researcher"])
    assert {edge["relationship"] for edge in edges} == {"command", "report"}
    store.remove_agent("researcher")
    assert store.edges(project["id"]) == []
    store.delete(project["id"])
    with pytest.raises(KeyError):
        store.get(project["id"])


def test_edges_reject_invalid_relationship(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = ProjectStore(tmp_path / "projects.db")
    project = store.list()[0]
    with pytest.raises(ValueError, match="itself"):
        store.save_edges(project["id"], [
            {"source_role": "reviewer", "target_role": "reviewer", "relationship": "command"}
        ], ["reviewer"])
