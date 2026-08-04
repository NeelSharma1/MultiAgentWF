from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from shared_context import ContextStore
from project_store import ProjectStore
from agent_definitions import AgentDefinitionStore
from runtime_config import RuntimeConfigStore
from credentials import LocalCredentialStore
from skills import SkillStore, run_skill_script, skill_secret_names
from toolsets import ToolsetStore


DB_PATH = Path(os.getenv("WORKSPACE_DB", Path(__file__).parent / "data" / "workspace.db"))
store = ContextStore(DB_PATH)
projects = ProjectStore(DB_PATH)
definitions = AgentDefinitionStore(DB_PATH)
messages = RuntimeConfigStore(DB_PATH)
skills = SkillStore(DB_PATH)
toolsets = ToolsetStore()
credentials = LocalCredentialStore(Path(__file__).parent / ".env.local")
skill_credentials = LocalCredentialStore(Path(__file__).parent / "data" / ".skill-secrets.local")
mcp = FastMCP("Shared Team Context")


@mcp.tool()
def send_agent_message(sender_role: str, recipient_role: str, relationship: str,
                       content: str, project_id: int = 1) -> dict:
    """Send a durable command or report to another agent's project-scoped chat.

    Use relationship='command' for actionable work and relationship='report' for
    findings, status, or decisions. The recipient sees it in its groupchat
    transcript, and it is synthesized into that agent's next provider prompt.
    When the recipient is idle, the main app automatically starts its own
    provider run rather than executing an in-process handoff.
    """
    projects.get(project_id)
    definitions.get(sender_role, project_id)
    definitions.get(recipient_role, project_id)
    message = messages.send_agent_message(sender_role, recipient_role, content, relationship, project_id)
    active = messages.active_chat_run(recipient_role, project_id)
    message["recipient_active"] = bool(active)
    message["queued_for_next_prompt"] = True
    message["active_run_id"] = active["id"] if active else None
    return message


@mcp.tool()
def list_agent_messages(role: str, project_id: int = 1, include_delivered: bool = True) -> list[dict]:
    """List queued and delivered commands/reports addressed to this agent."""
    projects.get(project_id)
    definitions.get(role, project_id)
    return messages.agent_inbox(role, project_id, include_delivered=include_delivered)


@mcp.tool()
def list_shared_context(role: str, project_id: int = 1) -> list[dict]:
    """List context visible to a role. Items with no assigned roles are visible to everyone."""
    return store.list(role, project_id)


@mcp.tool()
def get_shared_context(item_id: int, project_id: int = 1) -> dict:
    """Read one shared context item by numeric ID."""
    item = store.get(item_id, project_id)
    return item or {"error": f"Context item {item_id} was not found"}


@mcp.tool()
def publish_shared_context(title: str, content: str, roles: list[str], project_id: int = 1) -> dict:
    """Publish useful findings for selected roles. Use an empty roles list to share with the whole team."""
    return store.save(title, content, roles, project_id=project_id)


@mcp.tool()
def list_assigned_skills(role: str, project_id: int = 1) -> list[dict]:
    """List ACP/Agent-Skills discovery metadata assigned to this agent.

    This is the progressive-disclosure L1 step. Use load_assigned_skill to
    retrieve the full SKILL.md body only after a skill matches the task.
    """
    return skills.summaries(project_id, role)


@mcp.tool()
def load_assigned_skill(skill_id: int, role: str, project_id: int = 1) -> dict:
    """Load an assigned skill's ACP-compatible SKILL.md instructions."""
    skill = skills.load_assigned(skill_id, project_id, role)
    return skill or {"ok": False, "error": f"Skill {skill_id} is not assigned to {role} in workspace {project_id}"}


@mcp.tool()
def read_skill_resource(skill_id: int, role: str, path: str, project_id: int = 1) -> dict:
    """Read a relative references/, assets/, or other file from an assigned skill package."""
    return skills.read_resource(skill_id, project_id, role, path)


@mcp.tool()
def run_assigned_skill(skill_id: int, role: str, inputs: dict, project_id: int = 1) -> dict:
    """Run an assigned skill's selected OS/version helper with JSON inputs."""
    if not skills.is_assigned(skill_id, project_id, role):
        return {"ok": False, "error": f"Skill {skill_id} is not assigned to {role} in workspace {project_id}"}
    skill = skills.get(skill_id)
    if not skill:
        return {"ok": False, "error": f"Skill {skill_id} was not found"}
    try:
        project = projects.get(project_id)
        cwd = Path(str(project.get("root_path") or Path(__file__).parent)).expanduser().resolve()
        if not cwd.is_dir():
            return {"ok": False, "error": f"Project folder does not exist: {cwd}"}
        names = skill_secret_names(skill)
        secret_values = credentials.values_for(names)
        secret_values.update(skill_credentials.values_for(names))
        return run_skill_script(skill, inputs, cwd, secret_values=secret_values)
    except (KeyError, ValueError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def list_assigned_toolsets(role: str, project_id: int = 1) -> list[dict]:
    """List the short descriptions and TOOLSET.md filenames assigned to an agent."""
    project = projects.get(project_id)
    definitions.get(role, project_id)
    root = Path(str(project.get("root_path") or Path(__file__).parent)).expanduser().resolve()
    return toolsets.list(root, project_id, role)


@mcp.tool()
def load_toolset_summary(slug: str, role: str, project_id: int = 1) -> dict:
    """Load only the summary section of an assigned local command-line toolset."""
    project = projects.get(project_id)
    definitions.get(role, project_id)
    root = Path(str(project.get("root_path") or Path(__file__).parent)).expanduser().resolve()
    try:
        return toolsets.summary(root, project_id, role, slug)
    except (KeyError, PermissionError, ValueError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


if __name__ == "__main__":
    mcp.run(transport="stdio")
