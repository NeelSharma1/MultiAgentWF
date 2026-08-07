from __future__ import annotations

import asyncio
import json
import os
import subprocess
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

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
# This process is an auxiliary MCP tool host, not the application owner.  It
# shares the runtime database with the app while a provider run is active, so
# it must not perform startup recovery and mark that live run as interrupted.
messages = RuntimeConfigStore(DB_PATH, recover_interrupted_runs=False)
skills = SkillStore(DB_PATH)
toolsets = ToolsetStore()
credentials = LocalCredentialStore(Path(__file__).parent / ".env.local")
skill_credentials = LocalCredentialStore(Path(__file__).parent / "data" / ".skill-secrets.local")
# The application mounts this server below ``/mcp``.  Using ``/`` here keeps
# the public endpoint at ``/mcp`` instead of producing the confusing
# ``/mcp/mcp`` URL.  The stdio entry point at the bottom of this file remains
# available for the Agents SDK path.
mcp = FastMCP("Shared Team Context", streamable_http_path="/")

# Codex uses MCP annotations when deciding whether a tool is safe to invoke in
# a non-interactive run.  These operations do not intentionally modify source
# files, the workspace database, or external services.  Explicit annotations
# prevent a safe read/test request from being treated as an unclassified write
# and cancelled before FastMCP receives a CallToolRequest.
READ_ONLY_TOOL_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


def project_python_executable(project_root: Path) -> Path:
    """Find the project's virtual-environment interpreter, never system Python."""
    root = Path(project_root).expanduser().resolve()
    executable_name = "python.exe" if os.name == "nt" else "python"
    bin_name = "Scripts" if os.name == "nt" else "bin"
    expected: list[str] = []
    for environment_name in ("venv", ".venv", "env"):
        environment_root = root / environment_name
        candidate = environment_root / bin_name / executable_name
        expected.append(str(candidate))
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(
        "No project virtual environment was found. Create a venv in the project "
        f"before running tests. Checked: {', '.join(expected)}"
    )


def run_project_tests_via_app(role: str, project_id: int, pytest_args: list[str]) -> dict | None:
    """Ask the local app to run tests outside Codex's process sandbox.

    Windows venv launchers can depend on a base interpreter outside the
    workspace. The app already runs on the host and can invoke that same venv
    without granting the provider direct access to the base installation.
    """
    app_url = os.getenv("WORKSPACE_APP_URL", "").strip().rstrip("/")
    if not app_url:
        return None
    request = Request(
        f"{app_url}/api/internal/project-tests",
        data=json.dumps({
            "role": role,
            "project_id": project_id,
            "pytest_args": pytest_args,
        }).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=610) as response:
            result = json.loads(response.read().decode("utf-8"))
        return result if isinstance(result, dict) else {"ok": False, "error": "Invalid response from the app runner"}
    except HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
            detail = body.get("detail") if isinstance(body, dict) else ""
        except (OSError, ValueError):
            detail = ""
        return {"ok": False, "error": str(detail or f"The app test runner returned HTTP {exc.code}")}
    except (OSError, URLError) as exc:
        return {"ok": False, "error": f"The local app test runner could not be reached: {exc}"}


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
    project = projects.get(project_id)
    if project.get("enforce_relationships") and not any(
        edge["source_role"] == sender_role
        and edge["target_role"] == recipient_role
        and edge["relationship"] == relationship
        for edge in projects.edges(project_id)
    ):
        raise ValueError("Relationship enforcement blocks this inter-agent message")
    message = messages.send_agent_message(sender_role, recipient_role, content, relationship, project_id)
    active = messages.active_chat_run(recipient_role, project_id)
    message["recipient_active"] = bool(active)
    message["queued_for_next_prompt"] = True
    message["active_run_id"] = active["id"] if active else None
    return message


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
def list_agent_messages(role: str, project_id: int = 1, include_delivered: bool = True) -> list[dict]:
    """List queued and delivered commands/reports addressed to this agent."""
    projects.get(project_id)
    definitions.get(role, project_id)
    return messages.agent_inbox(role, project_id, include_delivered=include_delivered)


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
def list_shared_context(role: str, project_id: int = 1) -> list[dict]:
    """List context visible to a role. Items with no assigned roles are visible to everyone."""
    return store.list(role, project_id)


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
def get_shared_context(item_id: int, project_id: int = 1) -> dict:
    """Read one shared context item by numeric ID."""
    item = store.get(item_id, project_id)
    return item or {"error": f"Context item {item_id} was not found"}


@mcp.tool()
def publish_shared_context(title: str, content: str, roles: list[str], project_id: int = 1) -> dict:
    """Publish useful findings for selected roles. Use an empty roles list to share with the whole team."""
    return store.save(title, content, roles, project_id=project_id)


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
def list_assigned_skills(role: str, project_id: int = 1) -> list[dict]:
    """List ACP/Agent-Skills discovery metadata assigned to this agent.

    This is the progressive-disclosure L1 step. Use load_assigned_skill to
    retrieve the full SKILL.md body only after a skill matches the task.
    """
    return skills.summaries(project_id, role)


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
def load_assigned_skill(skill_id: int, role: str, project_id: int = 1) -> dict:
    """Load an assigned skill's ACP-compatible SKILL.md instructions."""
    skill = skills.load_assigned(skill_id, project_id, role)
    return skill or {"ok": False, "error": f"Skill {skill_id} is not assigned to {role} in workspace {project_id}"}


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
def read_skill_resource(skill_id: int, role: str, path: str, project_id: int = 1) -> dict:
    """Read a relative references/, assets/, or other file from an assigned skill package."""
    return skills.read_resource(skill_id, project_id, role, path)


@mcp.tool()
def run_assigned_skill(skill_id: int, role: str, inputs: dict, project_id: int = 1) -> dict:
    """Run an assigned skill's selected OS/version helper with JSON inputs."""
    permissions = projects.agent_action_permissions(project_id, role)
    projects.get(project_id)
    if not permissions["effective_commands"]:
        return {
            "ok": False,
            "error": "This agent must request in-chat approval before executing a local command",
        }
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


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
async def run_project_tests(role: str, project_id: int = 1, pytest_args: list[str] | None = None) -> dict:
    """Run pytest through the project's virtual-environment Python.

    This is the reliable fallback when Codex's own sandbox cannot launch the
    project's virtual-environment command directly. When the app bridge is
    available, the host application runs the venv outside that sandbox. It
    never uses a shell and requires the same command permission as any other
    local tool.
    """
    project = projects.get(project_id)
    definitions.get(role, project_id)
    permissions = projects.agent_action_permissions(project_id, role)
    if not permissions["effective_commands"]:
        return {
            "ok": False,
            "error": "This agent is not authorized to run project tests; request command permission first.",
        }
    root = Path(str(project.get("root_path") or Path(__file__).parent)).expanduser().resolve()
    if not root.is_dir():
        return {"ok": False, "error": f"Project folder does not exist: {root}"}
    try:
        interpreter = project_python_executable(root)
    except FileNotFoundError as exc:
        return {"ok": False, "error": str(exc)}
    arguments = [str(value) for value in (pytest_args or ["-q"])]
    if any("\x00" in value for value in arguments):
        return {"ok": False, "error": "pytest arguments cannot contain NUL characters"}
    # FastMCP invokes synchronous tools on its event loop in this version of
    # the SDK.  The app-backed bridge is a blocking urllib request; run it in
    # a worker thread so the mounted MCP server can service the callback.  A
    # direct HTTP MCP call otherwise deadlocks here and the client reports the
    # misleading "user cancelled MCP tool call" message.
    bridged = await asyncio.to_thread(run_project_tests_via_app, role, project_id, arguments)
    if bridged is not None:
        return bridged
    environment = {
        **os.environ,
        "VIRTUAL_ENV": str(interpreter.parent.parent),
        "PYTHONNOUSERSITE": "1",
        "PYTHONUNBUFFERED": "1",
    }
    environment.pop("PYTHONPATH", None)
    environment["PATH"] = os.pathsep.join([str(interpreter.parent), environment.get("PATH", "")])
    command = [str(interpreter), "-m", "pytest", *arguments]
    try:
        completed = await asyncio.to_thread(
            subprocess.run,
            command,
            cwd=str(root),
            env=environment,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        return {
            "ok": completed.returncode == 0,
            "command": command,
            "cwd": str(root),
            "python": str(interpreter),
            "exit_code": completed.returncode,
            "stdout": completed.stdout[-20000:],
            "stderr": completed.stderr[-20000:],
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "command": command,
            "cwd": str(root),
            "python": str(interpreter),
            "exit_code": None,
            "stdout": str(exc.stdout or "")[-20000:],
            "stderr": str(exc.stderr or "")[-20000:],
            "error": "pytest exceeded the 600-second timeout",
        }
    except OSError as exc:
        return {"ok": False, "command": command, "cwd": str(root), "python": str(interpreter), "error": str(exc)}


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
def list_assigned_toolsets(role: str, project_id: int = 1) -> list[dict]:
    """List the short descriptions and TOOLSET.md filenames assigned to an agent."""
    project = projects.get(project_id)
    definitions.get(role, project_id)
    root = Path(str(project.get("root_path") or Path(__file__).parent)).expanduser().resolve()
    return toolsets.list(root, project_id, role)


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
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
