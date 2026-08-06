from pathlib import Path

import pytest

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


def test_relationship_enforcement_limits_prompt_roster_and_messages(tmp_path):
    team = AgentTeam(tmp_path)
    project_id = team.projects.create("Restricted workspace")["id"]
    for role in ("lead", "worker", "observer"):
        team.definitions.save(role.title(), f"{role} brief", f"{role} instructions", role, project_id)
    team.projects.save_edges(project_id, [{
        "source_role": "lead", "target_role": "worker", "relationship": "command",
    }], ["lead", "worker", "observer"])
    team.projects.set_relationship_enforcement(project_id, True)

    instructions = team._instructions("lead", project_id)
    assert "You command worker" in instructions
    assert "observer" not in instructions
    team.send_agent_message("lead", "worker", "Do the work", "command", project_id)
    with pytest.raises(ValueError, match="Relationship enforcement"):
        team.send_agent_message("lead", "observer", "Do the work", "command", project_id)


def test_agent_action_permissions_are_scoped_and_global_mode_overrides_them(tmp_path):
    team = AgentTeam(tmp_path)
    project_id = team.projects.create("Action permissions")['id']
    team.definitions.save("Editor", "Edits files", "Make focused changes", "editor", project_id)

    initial = team._action_permissions("editor", project_id)
    assert initial["effective_commands"] is False
    assert initial["effective_file_edits"] is False
    team.projects.set_agent_action_permissions(project_id, "editor", True, False)
    granted = team._action_permissions("editor", project_id)
    assert granted["effective_commands"] is True
    assert granted["effective_file_edits"] is False
    assert 'permission_request scope="workspace"' in team._instructions("editor", project_id)

    team.projects.set_auto_approve_agent_actions(project_id, True)
    automatic = team._action_permissions("editor", project_id)
    assert automatic["effective_commands"] is True
    assert automatic["effective_file_edits"] is True
    assert "authorized you to run commands and edit files" in team._instructions("editor", project_id)

    team.projects.set_action_policy(project_id, False, True)
    unrestricted = team._action_permissions("editor", project_id)
    assert unrestricted["full_system_access"] is True
    assert "unrestricted local system access" in team._instructions("editor", project_id)


def test_active_workflow_memory_is_included_in_every_agent_context(tmp_path):
    team = AgentTeam(tmp_path)
    project_id = team.projects.create("Memory workspace")["id"]
    team.definitions.save("Planner", "Plans work", "Plan the workflow", "planner", project_id)
    memory = team.projects.save_workflow_memory(project_id, "Sprint Alpha", "The API migration is the priority.")
    team.projects.set_active_workflow_memory(project_id, memory["id"])

    context = team._shared_context_text("planner", project_id)
    assert "Active workflow memory: Sprint Alpha" in context
    assert "API migration is the priority" in context
