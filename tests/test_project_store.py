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


def test_workflow_templates_save_and_apply_matching_agents(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = ProjectStore(tmp_path / "projects.db")
    source = store.create("Source")
    target = store.create("Target")
    roles = ["orchestrator", "researcher"]
    store.save_layout(source["id"], [
        {"role": "orchestrator", "x": 300, "y": 120},
        {"role": "researcher", "x": 620, "y": 240},
    ], roles)
    store.save_edges(source["id"], [
        {"source_role": "orchestrator", "target_role": "researcher", "relationship": "command"},
    ], roles)
    template = store.save_workflow_template(
        source["id"], "Research workflow", store.layout(source["id"], roles), store.edges(source["id"]), roles,
    )
    assert template["name"] == "Research workflow"
    assert len(template["edges"]) == 1
    applied = store.apply_workflow_template(target["id"], template["id"], ["orchestrator"])
    assert applied["edges"] == []
    assert applied["skipped_roles"] == ["researcher"]
    assert next(item for item in applied["layout"] if item["role"] == "orchestrator")["x"] == 300
    store.delete_workflow_template(template["id"])
    assert store.workflow_templates() == []


def test_relationship_enforcement_is_persisted_per_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = ProjectStore(tmp_path / "projects.db")
    project = store.create("Restricted workflow")
    assert project["enforce_relationships"] == 0
    updated = store.set_relationship_enforcement(project["id"], True)
    assert updated["enforce_relationships"] == 1
    assert store.get(project["id"])["enforce_relationships"] == 1


def test_agent_action_permissions_default_to_read_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = ProjectStore(tmp_path / "projects.db")
    project = store.create("Actions")
    default = store.agent_action_permissions(project["id"], "editor")
    assert default["effective_commands"] is False
    assert default["effective_file_edits"] is False
    saved = store.set_agent_action_permissions(project["id"], "editor", True, True)
    assert saved["allow_commands"] is True
    assert saved["allow_file_edits"] is True
    overview = store.action_permissions(project["id"], ["editor"])
    assert overview["agents"] == [saved]


def test_full_system_access_overrides_project_action_restrictions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = ProjectStore(tmp_path / "projects.db")
    project = store.create("Unrestricted actions")
    updated = store.set_action_policy(project["id"], False, True)
    assert updated["allow_full_system_access"] == 1
    permissions = store.agent_action_permissions(project["id"], "editor")
    assert permissions["full_system_access"] is True
    assert permissions["effective_commands"] is True
    assert permissions["effective_file_edits"] is True


def test_agent_can_receive_individual_full_system_access(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = ProjectStore(tmp_path / "projects.db")
    project = store.create("Single-agent external access")
    saved = store.set_agent_action_permissions(project["id"], "editor", False, False, True)
    assert saved["allow_full_system_access"] is True
    assert saved["full_system_access"] is True
    revoked = store.set_agent_action_permissions(project["id"], "editor", False, False, False)
    assert revoked["allow_full_system_access"] is False
    assert revoked["full_system_access"] is False
    assert store.agent_action_permissions(project["id"], "reviewer")["full_system_access"] is False


def test_permission_requests_are_resolved_once(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = ProjectStore(tmp_path / "projects.db")
    project = store.create("Approval requests")
    request = store.record_permission_request(
        project["id"], "editor", 42, "external", "Read a sibling folder", ["Get-ChildItem C:\\sibling"],
    )
    assert request["status"] == "pending"
    assert request["commands"] == ["Get-ChildItem C:\\sibling"]
    resolved = store.resolve_permission_request(project["id"], "editor", 42, True)
    assert resolved["status"] == "approved"
    with pytest.raises(ValueError, match="already approved"):
        store.resolve_permission_request(project["id"], "editor", 42, True)
    store.record_permission_request(project["id"], "editor", 43, "workspace", "Edit one file", [])
    with pytest.raises(ValueError, match="did not include any commands"):
        store.resolve_permission_request(project["id"], "editor", 43, True)


def test_workflow_memories_can_be_activated_and_switched(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = ProjectStore(tmp_path / "projects.db")
    project = store.create("Memories")
    first = store.save_workflow_memory(project["id"], "Feature A", "Implement the first feature.")
    second = store.save_workflow_memory(project["id"], "Feature B", "Investigate the second feature.")
    active = store.set_active_workflow_memory(project["id"], first["id"])
    assert active["active_memory_id"] == first["id"]
    assert store.workflow_memory(project["id"], first["id"])["content"] == "Implement the first feature."
    active = store.set_active_workflow_memory(project["id"], second["id"])
    assert active["active_memory_id"] == second["id"]
    store.delete_workflow_memory(project["id"], second["id"])
    assert store.workflow_memories(project["id"])["active_memory_id"] == 0
