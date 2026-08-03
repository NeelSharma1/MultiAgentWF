from pathlib import Path

from shared_context import ContextStore
from team import AgentTeam


def test_new_workspaces_are_empty_and_agent_deletion_is_scoped(tmp_path):
    team = AgentTeam(tmp_path)
    existing = team.projects.list()[0]["id"]
    fresh = team.projects.create("Fresh workspace")["id"]

    assert team.definitions.list(existing)
    assert team.definitions.list(fresh) == []

    team.definitions.save("Local Specialist", "Finds local risks", "Review local risks", "local_specialist", fresh)
    team.configs.save("local_specialist", "google", "gemini-test", "", "", project_id=fresh)
    assert team.configs.get("local_specialist", fresh)["provider"] == "google"

    team.definitions.delete("local_specialist", fresh)
    assert team.definitions.list(fresh) == []
    assert all(item["role"] != "local_specialist" for item in team.definitions.list(existing))


def test_shared_context_is_scoped_to_workspace(tmp_path):
    team = AgentTeam(tmp_path)
    existing = team.projects.list()[0]["id"]
    fresh = team.projects.create("Fresh workspace")["id"]
    store = ContextStore(Path(tmp_path) / "data" / "workspace.db")

    store.save("Fresh-only", "Keep this private to the new workspace", [], project_id=fresh)
    assert store.list(project_id=existing) == []
    assert [item["title"] for item in store.list(project_id=fresh)] == ["Fresh-only"]
