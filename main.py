from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import os
import signal
import shutil
import socket
import sys
import uuid
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Any

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env.local")


def bind_available_port(host: str = "127.0.0.1", start_port: int = 8000) -> tuple[socket.socket, int]:
    """Reserve the first free TCP port at or above ``start_port``.

    Returning the bound socket closes the small race between checking a port
    and asking Uvicorn to listen on it. Uvicorn receives this socket directly
    when the application starts.
    """
    if not 1 <= start_port <= 65535:
        raise ValueError("start_port must be between 1 and 65535")
    for port in range(start_port, 65536):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((host, port))
        except OSError:
            listener.close()
            continue
        return listener, port
    raise OSError(f"No available TCP port found from {start_port} through 65535")

from shared_context import ContextStore, ROLES  # noqa: E402
from team import AgentTeam, ROLE_BRIEFS  # noqa: E402
from credentials import LocalCredentialStore  # noqa: E402
from project_store import ProjectStore  # noqa: E402
from github_status import GitReportError, collect_git_report, format_git_report  # noqa: E402
from skills import normalize_skill_secret_refs, run_skill_script_async, skill_secret_names  # noqa: E402
from git_workflow import GitWorkflowError  # noqa: E402

store = ContextStore(ROOT / "data" / "workspace.db")
team = AgentTeam(ROOT)
credentials = LocalCredentialStore(ROOT / ".env.local")
skill_credentials = LocalCredentialStore(ROOT / "data" / ".skill-secrets.local")
projects = ProjectStore(ROOT / "data" / "workspace.db")
chat_tasks: set[asyncio.Task] = set()
git_run_locks: dict[int, asyncio.Lock] = {}
agent_dispatch_task: asyncio.Task | None = None
AUTOMATIC_AGENT_PROMPT = (
    "Process the queued team messages now. Follow each command, use reports as context, "
    "and send a concise report to the requesting agent when the work is complete."
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global agent_dispatch_task
    await team.start()
    agent_dispatch_task = asyncio.create_task(
        dispatch_pending_agent_messages(), name="agent-message-dispatcher"
    )
    try:
        yield
    finally:
        if agent_dispatch_task:
            agent_dispatch_task.cancel()
            await asyncio.gather(agent_dispatch_task, return_exceptions=True)
            agent_dispatch_task = None
        for task in chat_tasks:
            task.cancel()
        if chat_tasks:
            await asyncio.gather(*chat_tasks, return_exceptions=True)
        await team.stop()


app = FastAPI(title="Agent Team Workspace", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


class ChatInput(BaseModel):
    message: str = Field(min_length=1, max_length=50_000)
    model: str = ""
    reasoning_effort: str = ""
    project_id: int = 1
    reply_to_id: int | None = None
    attachment_ids: list[str] = Field(default_factory=list, max_length=6)
    # Internal automatic handoff prompts are sent to the provider but should
    # not become a synthetic user bubble in the recipient's transcript.
    record_user_message: bool = True
    user_message_id: int | None = None

class NativeCommandInput(BaseModel):
    command: str = Field(min_length=1, max_length=1000)
    project_id: int = 1


class AppMessageInput(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)
    project_id: int = 1


class AgentMessageInput(BaseModel):
    recipient_role: str = Field(min_length=2, max_length=80)
    relationship: str = Field(pattern="^(command|report)$")
    content: str = Field(min_length=1, max_length=100_000)
    project_id: int = 1


class ContextInput(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=100_000)
    roles: list[str] = Field(default_factory=list)
    project_id: int = 1


class RuntimeInput(BaseModel):
    provider: str
    model: str = ""
    base_url: str = ""
    api_key_env: str = ""
    reasoning_effort: str = ""
    project_id: int = 1

class AgentInput(BaseModel):
    name: str
    role: str = ""
    brief: str
    instructions: str
    project_id: int = 1
    git_enabled: bool = False

class GenerateAgentInput(BaseModel):
    prompt: str = Field(min_length=3, max_length=5000)


class GitWorkflowInput(BaseModel):
    main_branch: str = Field(min_length=1, max_length=120)
    initialize: bool = False
    remote: str = Field(default="", max_length=120)
    remote_url: str = Field(default="", max_length=2000)


class GitAgentInput(BaseModel):
    enabled: bool


class GitDiffOpenInput(BaseModel):
    path: str = Field(min_length=1, max_length=2000)
    editor: str = Field(pattern="^(pycharm|vscode)$")


class GitPushInput(BaseModel):
    remote: str = Field(default="", max_length=120)

class CredentialInput(BaseModel):
    credential: str = Field(min_length=1, max_length=10000)

class ProjectInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    root_path: str = Field(default="", max_length=2000)

class LayoutItem(BaseModel):
    role: str
    x: float
    y: float

class LayoutInput(BaseModel):
    items: list[LayoutItem]

class EdgeItem(BaseModel):
    source_role: str
    target_role: str
    relationship: str

class EdgeInput(BaseModel):
    edges: list[EdgeItem]

class TemplateInput(BaseModel):
    name: str = Field(default="", max_length=120)
    project_id: int = 1


class CodeExecutionInput(BaseModel):
    code: str = Field(min_length=1, max_length=100_000)
    language: str = Field(default="", max_length=32)
    project_id: int = 1


class SkillInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(default="", max_length=80)
    summary: str = Field(min_length=1, max_length=2000)
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)
    language: str = Field(default="none", max_length=32)
    script: str = Field(default="", max_length=100_000)
    created_by: str = Field(default="human", max_length=40)
    version: str = Field(default="1.0.0", max_length=64)
    platform: str = Field(default="any", max_length=20)
    platform_variants: dict[str, dict[str, Any]] = Field(default_factory=dict)
    skill_type: str = Field(default="general", max_length=40)
    body: str = Field(default="", max_length=100_000)
    output_format: str = Field(default="text", max_length=40)
    license: str = Field(default="", max_length=200)
    compatibility: str = Field(default="", max_length=500)
    allowed_tools: str = Field(default="", max_length=2000)
    source: str = Field(default="local", max_length=40)
    source_url: str = Field(default="", max_length=2000)
    author: str = Field(default="", max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)
    required_secrets: list[dict[str, Any]] = Field(default_factory=list, max_length=100)


class SkillGenerateInput(BaseModel):
    prompt: str = Field(min_length=3, max_length=5000)


class SkillMarketplaceInstallInput(BaseModel):
    source_url: str = Field(min_length=1, max_length=2000)
    marketplace_id: str = Field(default="", max_length=200)
    expected_name: str = Field(default="", max_length=120)


class SkillAssignmentInput(BaseModel):
    project_id: int = 1
    roles: list[str] = Field(default_factory=list, max_length=100)


class SkillRunInput(BaseModel):
    project_id: int = 1
    role: str
    inputs: dict[str, Any] = Field(default_factory=dict)


class ToolDefinitionInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=2000)
    inputs: str = Field(default="No arguments.", max_length=4000)
    outputs: str = Field(default="Text output.", max_length=4000)
    filename: str = Field(min_length=1, max_length=500)
    output_format: str = Field(default="text", pattern="^(text|markdown|json|code)$")
    result_template: str = Field(default="{stdout}", max_length=20_000)
    env_vars: list[str] = Field(default_factory=list, max_length=100)
    source: str = Field(default="", max_length=500_000)


class ToolsetInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(default="", max_length=80)
    description: str = Field(min_length=1, max_length=2000)
    details: str = Field(default="", max_length=50_000)
    tools: list[ToolDefinitionInput] = Field(min_length=1, max_length=50)
    roles: list[str] = Field(default_factory=list, max_length=100)
    project_id: int = 1


class ToolsetGenerateInput(BaseModel):
    prompt: str = Field(min_length=3, max_length=10_000)


CODE_LANGUAGE_ALIASES = {
    "py": "python",
    "python3": "python",
    "js": "javascript",
    "mjs": "javascript",
    "cjs": "javascript",
    "node": "javascript",
    "sh": "shell",
    "bash": "shell",
    "zsh": "shell",
    "rb": "ruby",
}
CODE_RUNNERS = {"python", "javascript", "shell", "ruby"}
CODE_RUN_TIMEOUT = 10
CODE_RUN_OUTPUT_LIMIT = 200_000


def normalize_code_language(value: str) -> str:
    raw = str(value or "").strip().lower().removeprefix("language-")
    return CODE_LANGUAGE_ALIASES.get(raw, raw or "")


def code_execution_command(language: str, code: str) -> list[str]:
    if language == "python":
        return [sys.executable, "-c", code]
    if language == "javascript":
        executable = shutil.which("node")
        if not executable:
            raise ValueError("Node.js is not installed or is not available on the server PATH")
        return [executable, "-e", code]
    if language == "shell":
        executable = shutil.which("sh")
        if not executable:
            raise ValueError("A POSIX shell is not available on the server PATH")
        return [executable, "-c", code]
    if language == "ruby":
        executable = shutil.which("ruby")
        if not executable:
            raise ValueError("Ruby is not installed or is not available on the server PATH")
        return [executable, "-e", code]
    raise ValueError(f"Running {language or 'plain text'} code is not supported")


async def read_limited(stream: asyncio.StreamReader, limit: int) -> tuple[str, bool]:
    chunks: list[bytes] = []
    total = 0
    truncated = False
    while chunk := await stream.read(8192):
        if total < limit:
            remaining = limit - total
            chunks.append(chunk[:remaining])
        total += len(chunk)
        if total > limit:
            truncated = True
    return b"".join(chunks).decode(errors="replace"), truncated


def kill_code_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if os.name == "nt":
        process.kill()
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()


async def run_code_in_project(code: str, language: str, cwd: Path) -> dict[str, object]:
    command = code_execution_command(language, code)
    environment = os.environ.copy()
    environment["PYTHONUNBUFFERED"] = "1"
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd),
        env=environment,
        start_new_session=os.name != "nt",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None and process.stderr is not None
    stdout_task = asyncio.create_task(read_limited(process.stdout, CODE_RUN_OUTPUT_LIMIT))
    stderr_task = asyncio.create_task(read_limited(process.stderr, CODE_RUN_OUTPUT_LIMIT))
    timed_out = False
    try:
        await asyncio.wait_for(process.wait(), timeout=CODE_RUN_TIMEOUT)
    except asyncio.TimeoutError:
        timed_out = True
        kill_code_process(process)
        await process.wait()
    except asyncio.CancelledError:
        kill_code_process(process)
        await process.wait()
        raise
    stdout, stdout_truncated = await stdout_task
    stderr, stderr_truncated = await stderr_task
    if timed_out:
        stderr = f"{stderr}\nProcess timed out after {CODE_RUN_TIMEOUT} seconds.".strip()
    if stdout_truncated:
        stdout = f"{stdout}\n[stdout truncated after {CODE_RUN_OUTPUT_LIMIT:,} bytes]".strip()
    if stderr_truncated:
        stderr = f"{stderr}\n[stderr truncated after {CODE_RUN_OUTPUT_LIMIT:,} bytes]".strip()
    return {
        "ok": process.returncode == 0 and not timed_out,
        "language": language,
        "cwd": str(cwd),
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "stdout": stdout,
        "stderr": stderr,
    }


@app.get("/")
async def index():
    return FileResponse(ROOT / "static" / "index.html", headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/health")
async def health(project_id: int = 1):
    try:
        projects.get(project_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"status": "ok", "agents": len(team.definitions.list(project_id)), "mcp": team.mcp is not None}


@app.get("/api/agents")
async def agents(project_id: int = 1):
    try:
        projects.get(project_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    definitions = team.definitions.list(project_id)
    configs = {item["role"]: item for item in team.configs.list([item["role"] for item in definitions], project_id)}
    return [{"id": item["role"], "name": item["name"], "brief": item["brief"], "instructions": item["instructions"], "built_in": bool(item["built_in"]), "runtime": configs[item["role"]], "git_enabled": team.git.agent_enabled(project_id, item["role"])} for item in definitions]

@app.get("/api/projects")
async def list_projects():
    return projects.list()

@app.post("/api/projects", status_code=201)
async def create_project(payload: ProjectInput):
    try: return projects.create(payload.name, payload.description, payload.root_path)
    except ValueError as exc: raise HTTPException(422, str(exc)) from exc

@app.delete("/api/projects/{project_id}", status_code=204)
async def delete_project(project_id: int):
    try:
        projects.get(project_id)
        for agent in team.definitions.list(project_id):
            for attachment in team.configs.attachments_for(agent["role"], project_id):
                Path(attachment["path"]).unlink(missing_ok=True)
        team.configs.remove_project(project_id)
        team.context.delete_project(project_id)
        team.definitions.delete_project(project_id)
        team.git.remove_project(project_id)
        projects.delete(project_id)
    except KeyError as exc: raise HTTPException(404, str(exc)) from exc

@app.get("/api/projects/{project_id}/layout")
async def get_project_layout(project_id: int):
    try: return projects.layout(project_id, [item["role"] for item in team.definitions.list(project_id)])
    except KeyError as exc: raise HTTPException(404, str(exc)) from exc

@app.put("/api/projects/{project_id}/layout")
async def update_project_layout(project_id: int, payload: LayoutInput):
    roles = [item["role"] for item in team.definitions.list(project_id)]
    try: return projects.save_layout(project_id, [item.model_dump() for item in payload.items], roles)
    except KeyError as exc: raise HTTPException(404, str(exc)) from exc
    except ValueError as exc: raise HTTPException(422, str(exc)) from exc

@app.get("/api/projects/{project_id}/edges")
async def get_project_edges(project_id: int):
    try: return projects.edges(project_id)
    except KeyError as exc: raise HTTPException(404, str(exc)) from exc

@app.put("/api/projects/{project_id}/edges")
async def update_project_edges(project_id: int, payload: EdgeInput):
    roles = [item["role"] for item in team.definitions.list(project_id)]
    try: return projects.save_edges(project_id, [item.model_dump() for item in payload.edges], roles)
    except KeyError as exc: raise HTTPException(404, str(exc)) from exc
    except ValueError as exc: raise HTTPException(422, str(exc)) from exc


def _git_project_root(project_id: int) -> Path:
    projects.get(project_id)
    return team._project_root(project_id)


@app.get("/api/projects/{project_id}/git")
async def git_status(project_id: int):
    try:
        root = _git_project_root(project_id)
        status = await asyncio.to_thread(team.git.status, project_id, root)
        status["commits"] = team.git.agent_commits(project_id)
        return status
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except GitWorkflowError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.put("/api/projects/{project_id}/git")
async def configure_git(project_id: int, payload: GitWorkflowInput):
    try:
        return await asyncio.to_thread(
            team.git.configure, project_id, _git_project_root(project_id), payload.main_branch,
            initialize=payload.initialize, remote=payload.remote, remote_url=payload.remote_url,
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except GitWorkflowError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.put("/api/projects/{project_id}/git/agents/{role}")
async def configure_agent_git(project_id: int, role: str, payload: GitAgentInput):
    try:
        team.definitions.get(role, project_id)
        projects.get(project_id)
        return await asyncio.to_thread(
            team.git.set_agent_enabled, project_id, role, payload.enabled, _git_project_root(project_id),
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except GitWorkflowError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/projects/{project_id}/git/agents/{role}/changes")
async def agent_git_changes(project_id: int, role: str):
    try:
        team.definitions.get(role, project_id)
        projects.get(project_id)
        return {"role": role, "commits": team.git.agent_commits(project_id, role)}
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/projects/{project_id}/git/commits/{commit_hash}/diff")
async def git_file_diff(project_id: int, commit_hash: str, path: str):
    try:
        return await asyncio.to_thread(team.git.file_diff, project_id, _git_project_root(project_id), commit_hash, path)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except GitWorkflowError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/projects/{project_id}/git/commits/{commit_hash}/open-diff")
async def open_git_diff(project_id: int, commit_hash: str, payload: GitDiffOpenInput):
    try:
        return await asyncio.to_thread(
            team.git.open_diff, project_id, _git_project_root(project_id), commit_hash, payload.path, payload.editor,
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except GitWorkflowError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/projects/{project_id}/git/commits/{commit_hash}/revert")
async def revert_git_commit(project_id: int, commit_hash: str):
    try:
        return await asyncio.to_thread(team.git.revert, project_id, _git_project_root(project_id), commit_hash)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except GitWorkflowError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/projects/{project_id}/git/commits/{commit_hash}/rollback")
async def rollback_git_commit(project_id: int, commit_hash: str):
    try:
        return await asyncio.to_thread(team.git.rollback, project_id, _git_project_root(project_id), commit_hash)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except GitWorkflowError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/projects/{project_id}/git/commits/{commit_hash}/push")
async def push_git_commit(project_id: int, commit_hash: str, payload: GitPushInput):
    try:
        return await asyncio.to_thread(
            team.git.push, project_id, _git_project_root(project_id), commit_hash, payload.remote,
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except GitWorkflowError as exc:
        raise HTTPException(422, str(exc)) from exc

@app.post("/api/agents", status_code=201)
async def create_agent(payload: AgentInput):
    try:
        projects.get(payload.project_id)
        if payload.git_enabled and not team.git.configuration(payload.project_id):
            raise ValueError("Configure a shared Git branch before enabling Git for this agent")
        created = team.definitions.save(payload.name, payload.brief, payload.instructions, payload.role, payload.project_id)
        await asyncio.to_thread(
            team.git.set_agent_enabled, payload.project_id, created["role"], payload.git_enabled,
            _git_project_root(payload.project_id),
        )
        created["git_enabled"] = payload.git_enabled
        return created
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ValueError, GitWorkflowError) as exc: raise HTTPException(422, str(exc)) from exc

@app.get("/api/agent-templates")
async def list_agent_templates():
    return team.definitions.templates()

@app.post("/api/agents/{role}/template", status_code=201)
async def save_agent_template(role: str, payload: TemplateInput):
    try: return team.definitions.save_template(role, payload.name, payload.project_id)
    except KeyError as exc: raise HTTPException(404, str(exc)) from exc

@app.post("/api/agents/generate")
async def generate_agent(payload: GenerateAgentInput):
    try: return await team.generate_agent_definition(payload.prompt)
    except Exception as exc: raise HTTPException(502, f"Codex generation failed: {exc}") from exc

@app.delete("/api/agents/{role}", status_code=204)
async def delete_agent(role: str, project_id: int = 1):
    try:
        projects.get(project_id)
        attachments = team.configs.attachments_for(role, project_id)
        team.definitions.delete(role, project_id)
        team.git.remove_agent(project_id, role)
        for attachment in attachments:
            Path(attachment["path"]).unlink(missing_ok=True)
        team.configs.remove_agent(role, project_id)
        team.skills.remove_agent(project_id, role)
        projects.remove_agent(role, project_id)
    except KeyError as exc: raise HTTPException(404, str(exc)) from exc
    except ValueError as exc: raise HTTPException(422, str(exc)) from exc


@app.get("/api/providers")
async def providers():
    return team.provider_status()

@app.get("/api/connections")
async def connections():
    codex = await team.codex_login_status()
    env_names = {"openai": "OPENAI_API_KEY", "google": "GEMINI_API_KEY",
                 "anthropic": "ANTHROPIC_API_KEY", "compatible": "LOCAL_API_KEY"}
    return {"codex": codex, **{
        provider: {"connected": bool(os.getenv(env_name)), "env": env_name}
        for provider, env_name in env_names.items()
    }}

@app.post("/api/connections/codex")
async def connect_codex():
    try: return await team.start_codex_login()
    except Exception as exc: raise HTTPException(502, str(exc)) from exc

@app.delete("/api/connections/codex", status_code=204)
async def disconnect_codex():
    try: await team.codex_logout()
    except Exception as exc: raise HTTPException(502, str(exc)) from exc

@app.put("/api/connections/{provider}", status_code=204)
async def connect_provider(provider: str, payload: CredentialInput):
    env_names = {"openai": "OPENAI_API_KEY", "google": "GEMINI_API_KEY",
                 "anthropic": "ANTHROPIC_API_KEY", "compatible": "LOCAL_API_KEY"}
    if provider not in env_names: raise HTTPException(404, "Unknown credential provider")
    try: credentials.save(env_names[provider], payload.credential)
    except ValueError as exc: raise HTTPException(422, str(exc)) from exc

@app.delete("/api/connections/{provider}", status_code=204)
async def disconnect_provider(provider: str):
    env_names = {"openai": "OPENAI_API_KEY", "google": "GEMINI_API_KEY",
                 "anthropic": "ANTHROPIC_API_KEY", "compatible": "LOCAL_API_KEY"}
    if provider not in env_names: raise HTTPException(404, "Unknown credential provider")
    credentials.remove(env_names[provider])

@app.get("/api/providers/{provider}/models")
async def provider_models(provider: str, base_url: str = "", api_key_env: str = ""):
    try: return {"models": await team.list_models(provider, base_url, api_key_env)}
    except Exception as exc: raise HTTPException(502, f"Could not load models: {exc}") from exc

@app.get("/api/providers/{provider}/commands")
async def provider_commands(provider: str):
    try:
        return {"provider": provider, "commands": team.provider_commands(provider)}
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/code/execute")
async def execute_code(payload: CodeExecutionInput):
    """Run a user-confirmed, fenced code block in the selected project folder.

    This endpoint deliberately uses an argument-vector runner (never a shell
    wrapper except when the selected language itself is shell) and only allows
    a small set of explicitly executable languages. Runtime failures are
    returned as normal data so the terminal pane can show stdout and stderr.
    """
    language = normalize_code_language(payload.language)
    if language not in CODE_RUNNERS:
        raise HTTPException(422, f"Running {payload.language or 'plain text'} code is not supported")
    if "\x00" in payload.code:
        raise HTTPException(422, "Code cannot contain NUL characters")
    try:
        project = projects.get(payload.project_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    requested_root = str(project.get("root_path") or "").strip()
    cwd = Path(requested_root).expanduser() if requested_root else ROOT
    try:
        cwd = cwd.resolve()
    except OSError as exc:
        raise HTTPException(422, f"Could not resolve the project folder: {exc}") from exc
    if not cwd.is_dir():
        raise HTTPException(422, f"Project folder does not exist or is not a directory: {cwd}")
    try:
        return await run_code_in_project(payload.code, language, cwd)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(502, f"Could not start the code runner: {exc}") from exc


def _toolset_project(project_id: int) -> Path:
    projects.get(project_id)
    return team._project_root(project_id)


@app.get("/api/toolsets")
async def list_toolsets(project_id: int = 1, role: str = ""):
    try:
        project_root = _toolset_project(project_id)
        if role:
            team.definitions.get(role, project_id)
        return team.toolsets.list(project_root, project_id, role or None)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/toolsets/{slug}")
async def get_toolset(slug: str, project_id: int = 1):
    try:
        project_root = _toolset_project(project_id)
        toolset = team.toolsets.get(project_root, slug)
        if not toolset:
            raise HTTPException(404, f"Toolset '{slug}' was not found")
        assigned = next(
            (item for item in team.toolsets.list(project_root, project_id) if item["slug"] == toolset["slug"]),
            None,
        )
        toolset["assigned_roles"] = assigned.get("assigned_roles", []) if assigned else []
        return toolset
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _save_toolset_payload(payload: ToolsetInput, existing_slug: str = "") -> dict[str, Any]:
    project_root = _toolset_project(payload.project_id)
    data = payload.model_dump()
    if existing_slug:
        data["slug"] = existing_slug
    valid_roles = {item["role"] for item in team.definitions.list(payload.project_id)}
    unknown = [role for role in payload.roles if role not in valid_roles]
    if unknown:
        raise ValueError(f"Unknown agent role(s): {', '.join(unknown)}")
    saved = team.toolsets.save(project_root, data)
    assignment = team.toolsets.assign(project_root, payload.project_id, saved["slug"], payload.roles)
    saved["assigned_roles"] = assignment["roles"]
    return saved


@app.post("/api/toolsets", status_code=201)
async def create_toolset(payload: ToolsetInput):
    try:
        return _save_toolset_payload(payload)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/toolsets/generate")
async def generate_toolset(payload: ToolsetGenerateInput):
    try:
        return await team.generate_toolset_definition(payload.prompt)
    except Exception as exc:
        raise HTTPException(502, f"Codex toolset generation failed: {exc}") from exc


@app.put("/api/toolsets/{slug}")
async def update_toolset(slug: str, payload: ToolsetInput):
    try:
        if not team.toolsets.get(_toolset_project(payload.project_id), slug, include_source=False):
            raise HTTPException(404, f"Toolset '{slug}' was not found")
        return _save_toolset_payload(payload, slug)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.delete("/api/toolsets/{slug}", status_code=204)
async def delete_toolset(slug: str, project_id: int = 1):
    try:
        team.toolsets.delete(_toolset_project(project_id), slug)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/skills")
async def list_skills(project_id: int = 1, role: str = "", q: str = "", type: str = "", sort: str = "name", platform: str = ""):
    try:
        projects.get(project_id)
        if role:
            team.definitions.get(role, project_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    skills = team.skills.list(project_id, role or None, query=q, skill_type=type, sort=sort, platform=platform)
    for skill in skills:
        skill["secret_status"] = _skill_secret_status(skill)
    return skills


def _skill_secret_status(skill: dict[str, Any]) -> list[dict[str, Any]]:
    """Return declaration/status metadata only; never return credential values."""
    status = []
    for declaration in skill.get("required_secrets", []):
        name = str(declaration.get("name", "")).strip().upper()
        if not name:
            continue
        status.append({
            "name": name,
            "label": declaration.get("label") or name,
            "description": declaration.get("description") or "",
            "required": bool(declaration.get("required", True)),
            "configured": skill_credentials.configured(name) or credentials.configured(name),
        })
    return status


def _save_skill_payload(payload: SkillInput, skill_id: int | None = None) -> dict[str, Any]:
    """Persist the ACP package and any explicitly supplied OS variants."""
    saved = team.skills.save(
        payload.name, payload.summary, payload.inputs, payload.outputs,
        payload.language, payload.script, payload.slug, skill_id=skill_id,
        created_by=payload.created_by, version=payload.version, platform=payload.platform,
        skill_type=payload.skill_type, body=payload.body, output_format=payload.output_format,
        license_name=payload.license, compatibility=payload.compatibility, allowed_tools=payload.allowed_tools,
        source=payload.source, source_url=payload.source_url,
        author=payload.author, metadata=payload.metadata, required_secrets=payload.required_secrets,
    )
    for variant_platform, variant in payload.platform_variants.items():
        if not isinstance(variant, dict):
            raise ValueError(f"Platform variant '{variant_platform}' must be a JSON object")
        team.skills.save(
            variant.get("name", payload.name), variant.get("summary", payload.summary),
            variant.get("inputs", payload.inputs), variant.get("outputs", payload.outputs),
            variant.get("language", payload.language), variant.get("script", payload.script),
            payload.slug or saved["slug"], skill_id=saved["id"], created_by=payload.created_by,
            version=variant.get("version", payload.version), platform=variant_platform,
            skill_type=variant.get("skill_type", payload.skill_type), body=variant.get("body", payload.body),
            output_format=variant.get("output_format", payload.output_format), license_name=payload.license,
            compatibility=payload.compatibility, allowed_tools=payload.allowed_tools, source=payload.source,
            source_url=payload.source_url, author=payload.author, metadata=payload.metadata,
            required_secrets=variant.get("required_secrets", payload.required_secrets),
        )
    return team.skills.get(saved["id"]) or saved


@app.post("/api/skills", status_code=201)
async def create_skill(payload: SkillInput):
    try:
        return _save_skill_payload(payload)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/skills/generate")
async def generate_skill(payload: SkillGenerateInput):
    try:
        return await team.generate_skill_definition(payload.prompt)
    except Exception as exc:
        raise HTTPException(502, f"Codex skill generation failed: {exc}") from exc


@app.get("/api/skills/{skill_id}")
async def get_skill(skill_id: int, project_id: int = 1, role: str = "", platform: str = ""):
    try:
        projects.get(project_id)
        if role:
            team.definitions.get(role, project_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    skill = team.skills.get(skill_id, platform=platform, project_id=project_id, role=role or None)
    if not skill:
        raise HTTPException(404, f"Skill {skill_id} not found")
    skill["secret_status"] = _skill_secret_status(skill)
    return skill


@app.put("/api/skills/{skill_id}")
async def update_skill(skill_id: int, payload: SkillInput):
    try:
        return _save_skill_payload(payload, skill_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.delete("/api/skills/{skill_id}", status_code=204)
async def delete_skill(skill_id: int):
    try:
        team.skills.delete(skill_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/skills/{skill_id}/secrets")
async def skill_secret_status(skill_id: int):
    skill = team.skills.get(skill_id, include_body=False)
    if not skill:
        raise HTTPException(404, f"Skill {skill_id} not found")
    return {"skill_id": skill_id, "secrets": _skill_secret_status(skill)}


@app.put("/api/skills/{skill_id}/secrets/{secret_name}", status_code=204)
async def set_skill_secret(skill_id: int, secret_name: str, payload: CredentialInput):
    skill = team.skills.get(skill_id, include_body=False)
    if not skill:
        raise HTTPException(404, f"Skill {skill_id} not found")
    try:
        normalized = normalize_skill_secret_refs([secret_name])[0]["name"]
    except (IndexError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    if normalized not in skill_secret_names(skill):
        raise HTTPException(404, f"Secret '{normalized}' is not declared by this skill")
    try:
        skill_credentials.save(normalized, payload.credential, export_env=False)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.delete("/api/skills/{skill_id}/secrets/{secret_name}", status_code=204)
async def remove_skill_secret(skill_id: int, secret_name: str):
    skill = team.skills.get(skill_id, include_body=False)
    if not skill:
        raise HTTPException(404, f"Skill {skill_id} not found")
    try:
        normalized = normalize_skill_secret_refs([secret_name])[0]["name"]
    except (IndexError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    if normalized not in skill_secret_names(skill):
        raise HTTPException(404, f"Secret '{normalized}' is not declared by this skill")
    skill_credentials.remove(normalized, clear_env=False)


@app.get("/api/skills/{skill_id}/package")
async def download_skill_package(skill_id: int, platform: str = ""):
    try:
        archive = team.skills.package_archive(skill_id, platform=platform)
        skill = team.skills.get(skill_id, platform=platform, include_body=False)
        name = skill["acp_name"] if skill else f"skill-{skill_id}"
        return Response(
            content=archive, media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{name}.zip"'},
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/skills/marketplace/search")
async def marketplace_search(q: str, category: str = "", sort: str = "stars", page: int = 1, limit: int = 20):
    try:
        return await team.skills.search_marketplace(q, category=category, sort=sort, page=page, limit=limit)
    except ValueError as exc:
        raise HTTPException(502, str(exc)) from exc
    except (httpx.HTTPError, OSError) as exc:
        raise HTTPException(502, f"Could not reach the ACP skill marketplace: {exc}") from exc


@app.post("/api/skills/marketplace/install", status_code=201)
async def marketplace_install(payload: SkillMarketplaceInstallInput):
    try:
        return await team.skills.install_marketplace_skill(
            payload.source_url, marketplace_id=payload.marketplace_id, expected_name=payload.expected_name,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (httpx.HTTPError, OSError) as exc:
        raise HTTPException(502, f"Could not download ACP skill package: {exc}") from exc


@app.post("/api/skills/marketplace/install-stream")
async def marketplace_install_stream(payload: SkillMarketplaceInstallInput):
    """Stream marketplace install phases as newline-delimited JSON events."""
    async def events():
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def report(update: dict[str, Any]) -> None:
            await queue.put({"event": "progress", **update})

        task = asyncio.create_task(team.skills.install_marketplace_skill(
            payload.source_url, marketplace_id=payload.marketplace_id,
            expected_name=payload.expected_name, progress=report,
        ))
        try:
            while not task.done() or not queue.empty():
                try:
                    update = await asyncio.wait_for(queue.get(), timeout=0.25)
                except asyncio.TimeoutError:
                    continue
                yield json.dumps(update, ensure_ascii=False) + "\n"
            try:
                installed = task.result()
            except ValueError as exc:
                yield json.dumps({"event": "error", "detail": str(exc)}, ensure_ascii=False) + "\n"
            except (httpx.HTTPError, OSError) as exc:
                yield json.dumps({"event": "error", "detail": f"Could not download ACP skill package: {exc}"}, ensure_ascii=False) + "\n"
            except Exception as exc:
                yield json.dumps({"event": "error", "detail": str(exc)}, ensure_ascii=False) + "\n"
            else:
                yield json.dumps({"event": "complete", "skill": installed}, ensure_ascii=False) + "\n"
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    return StreamingResponse(
        events(), media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.put("/api/skills/{skill_id}/assignments")
async def assign_skill(skill_id: int, payload: SkillAssignmentInput):
    try:
        projects.get(payload.project_id)
        for role in payload.roles:
            team.definitions.get(role, payload.project_id)
        return team.skills.assign(skill_id, payload.project_id, payload.roles)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/skills/{skill_id}/run")
async def run_skill(skill_id: int, payload: SkillRunInput):
    try:
        project = projects.get(payload.project_id)
        team.definitions.get(payload.role, payload.project_id)
        if not team.skills.is_assigned(skill_id, payload.project_id, payload.role):
            raise HTTPException(403, "This skill is not assigned to that agent in this workspace")
        skill = team.skills.get(skill_id)
        if not skill:
            raise HTTPException(404, f"Skill {skill_id} not found")
        requested_root = str(project.get("root_path") or "").strip()
        cwd = (Path(requested_root).expanduser() if requested_root else ROOT).resolve()
        if not cwd.is_dir():
            raise HTTPException(422, f"Project folder does not exist or is not a directory: {cwd}")
        names = skill_secret_names(skill)
        secret_values = credentials.values_for(names)
        secret_values.update(skill_credentials.values_for(names))
        return await run_skill_script_async(skill, payload.inputs, cwd, secret_values=secret_values)
    except HTTPException:
        raise
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(502, f"Could not start the skill runner: {exc}") from exc


@app.put("/api/agents/{role}/runtime")
async def update_runtime(role: str, payload: RuntimeInput):
    try:
        team.definitions.get(role, payload.project_id)
        return team.configs.save(
            role, payload.provider, payload.model, payload.base_url, payload.api_key_env,
            payload.reasoning_effort, payload.project_id,
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.delete("/api/agents/{role}/history", status_code=204)
async def clear_history(role: str, project_id: int = 1):
    try: team.definitions.get(role, project_id)
    except KeyError as exc: raise HTTPException(404, str(exc)) from exc
    for attachment in team.configs.attachments_for(role, project_id):
        Path(attachment["path"]).unlink(missing_ok=True)
    team.configs.clear_history(role, project_id)


@app.get("/api/agents/{role}/history")
async def get_history(role: str, project_id: int = 1):
    try:
        team.definitions.get(role, project_id)
        projects.get(project_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"messages": team.configs.history(role, limit=200, project_id=project_id)}


@app.get("/api/agents/{role}/inbox")
async def get_agent_inbox(role: str, project_id: int = 1, include_delivered: bool = True):
    """Return durable commands/reports addressed to an agent in this workspace."""
    try:
        team.definitions.get(role, project_id)
        projects.get(project_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {
        "messages": team.configs.agent_inbox(
            role, project_id, include_delivered=include_delivered,
        )
    }


@app.post("/api/agents/{role}/messages", status_code=201)
async def send_agent_message(role: str, payload: AgentMessageInput):
    """Post a command/report from one agent into another agent's chat."""
    try:
        if role != role.strip():
            raise ValueError("Sender role cannot contain surrounding whitespace")
        return team.send_agent_message(
            role, payload.recipient_role, payload.content, payload.relationship, payload.project_id,
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/agents/{role}/attachment-capabilities")
async def attachment_capabilities(role: str, project_id: int = 1):
    try:
        team.definitions.get(role, project_id)
        provider = team.configs.get(role, project_id)["provider"]
    except KeyError as exc: raise HTTPException(404, str(exc)) from exc
    accepted = {
        "openai": ["image/*", "application/pdf", "text/*", "application/json"],
        "codex": ["image/*", "application/pdf", "text/*", "application/json"],
        "google": ["image/*", "text/*", "application/json"],
        "anthropic": ["image/*", "text/*", "application/json"],
        "compatible": ["image/*", "text/*", "application/json"],
    }.get(provider, [])
    return {"enabled": bool(accepted), "accept": accepted, "max_files": 6, "max_bytes": 10_000_000,
            "note": "Image support ultimately depends on the selected model."}


@app.post("/api/agents/{role}/attachments", status_code=201)
async def upload_attachment(role: str, project_id: int, attachment: UploadFile = File(...)):
    try:
        team.definitions.get(role, project_id)
        projects.get(project_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    name = Path(attachment.filename or "attachment").name[:240]
    mime_type = (attachment.content_type or "application/octet-stream").lower()
    content = await attachment.read(10_000_001)
    if not content:
        raise HTTPException(422, "The attachment is empty")
    if len(content) > 10_000_000:
        raise HTTPException(413, "Attachments are limited to 10 MB each")
    capabilities = await attachment_capabilities(role, project_id)
    allowed = any(
        mime_type == pattern or (pattern.endswith("/*") and mime_type.startswith(pattern[:-1]))
        for pattern in capabilities["accept"]
    )
    if not allowed:
        raise HTTPException(415, f"{mime_type} attachments are not supported by this runtime")
    uploads = ROOT / "data" / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    stored_path = uploads / f"{uuid.uuid4().hex}{Path(name).suffix[:12]}"
    stored_path.write_bytes(content)
    item = team.configs.create_attachment(project_id, role, name, mime_type, len(content), str(stored_path))
    return {key: item[key] for key in ("id", "name", "mime_type", "size")}


@app.get("/api/attachments/{attachment_id}")
async def download_attachment(attachment_id: str):
    item = team.configs.attachment(attachment_id)
    if not item or not item["message_id"]:
        raise HTTPException(404, "Attachment not found")
    path = Path(item["path"])
    if not path.is_file():
        raise HTTPException(404, "Attachment file not found")
    return FileResponse(path, media_type=item["mime_type"], filename=item["name"])


@app.delete("/api/attachments/{attachment_id}", status_code=204)
async def delete_pending_attachment(attachment_id: str):
    item = team.configs.delete_pending_attachment(attachment_id)
    if not item:
        raise HTTPException(404, "Pending attachment not found")
    Path(item["path"]).unlink(missing_ok=True)


def _timestamp(value: Any) -> datetime:
    """Normalize SQLite and ISO timestamps for dispatcher retry decisions."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _start_chat_task(run: dict[str, Any], role: str, payload: ChatInput) -> dict[str, Any]:
    """Attach an in-process task to a run that has already been claimed."""
    task = asyncio.create_task(
        run_chat(run["id"], role, payload), name=f"chat-{role}-{run['id']}"
    )
    chat_tasks.add(task)
    task.add_done_callback(chat_tasks.discard)
    print(
        f"[chat-run] scheduled role={role} project={payload.project_id} run={run['id']}",
        flush=True,
    )
    return run


def _schedule_chat_run(role: str, payload: ChatInput) -> dict[str, Any]:
    """Create a run immediately or persist it behind the current run."""
    if payload.record_user_message and not payload.user_message_id:
        config = team.configs.get(role, payload.project_id)
        user_message = team.configs.add_message(
            role,
            "user",
            payload.message.strip(),
            config["provider"],
            payload.model or config["model"],
            payload.project_id,
            payload.reply_to_id,
        )
        payload = payload.model_copy(update={
            "record_user_message": False,
            "user_message_id": user_message["id"],
        })
    active = team.configs.active_chat_run(role, payload.project_id)
    run = team.configs.create_chat_run(role, payload.project_id, payload.model_dump())
    if active:
        run["queued"] = True
        print(
            f"[chat-run] queued behind {active['id']} role={role} "
            f"project={payload.project_id} run={run['id']}",
            flush=True,
        )
        return run
    claimed = team.configs.claim_queued_chat_run(run["id"])
    if claimed is None:
        return run
    return _start_chat_task(claimed, role, payload)


def _dispatch_queued_chat_runs() -> None:
    """Start the oldest queued request for each idle agent."""
    started_targets: set[tuple[int, str]] = set()
    for queued in team.configs.queued_chat_runs():
        role = str(queued["role"])
        project_id = int(queued["project_id"])
        target = (project_id, role)
        if target in started_targets or team.configs.running_chat_run(role, project_id):
            continue
        request = queued.get("request") or {}
        try:
            payload = ChatInput.model_validate(request)
        except (TypeError, ValueError) as exc:
            team.configs.update_chat_run(
                queued["id"], "error", error=f"Queued prompt could not be restored: {exc}"
            )
            continue
        claimed = team.configs.claim_queued_chat_run(queued["id"])
        if claimed is None:
            continue
        _start_chat_task(claimed, role, payload)
        started_targets.add(target)


async def dispatch_pending_agent_messages() -> None:
    """Turn durable inter-agent messages into provider runs when recipients are idle.

    The MCP server writes to the same SQLite database from a separate process,
    so a small polling loop is the reliable bridge back into this process. A
    failed automatic run leaves its messages pending for inspection/manual
    retry, but is not retried in a tight loop until a newer message arrives.
    """
    while True:
        try:
            _dispatch_queued_chat_runs()
            for target in team.configs.pending_agent_recipients():
                role = str(target["role"])
                project_id = int(target["project_id"])
                try:
                    team.definitions.get(role, project_id)
                    projects.get(project_id)
                except KeyError:
                    # A deleted workspace/agent can leave historical rows in
                    # the database; do not create a run that can never finish.
                    continue
                if team.configs.active_chat_run(role, project_id):
                    continue
                pending = team.configs.pending_agent_messages(role, project_id)
                if not pending:
                    continue
                latest = team.configs.latest_chat_run(role, project_id)
                restart_error = "server restarted" in str(latest.get("error") or "").lower() if latest else False
                if latest and latest["status"] == "error" and not restart_error:
                    latest_time = _timestamp(latest.get("updated_at"))
                    if not any(_timestamp(item.get("created_at")) > latest_time for item in pending):
                        continue
                payload = ChatInput(
                    message=AUTOMATIC_AGENT_PROMPT,
                    project_id=project_id,
                    record_user_message=False,
                )
                _schedule_chat_run(role, payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The dispatcher must stay alive if a transient SQLite/provider
            # issue occurs; the next pass will retry the read-only scan.
            print(f"[chat-run] dispatcher error: {exc}", flush=True)
        await asyncio.sleep(0.5)


async def run_chat(run_id: str, role: str, payload: ChatInput) -> None:
    try:
        print(
            f"[chat-run] started role={role} project={payload.project_id} run={run_id}",
            flush=True,
        )
        team.configs.update_chat_run(run_id, "running")
        async def call_provider() -> dict[str, Any]:
            return await team.chat(
                role, payload.message.strip(), payload.model, payload.reasoning_effort, payload.project_id,
                payload.reply_to_id, payload.attachment_ids, payload.record_user_message,
                payload.user_message_id,
            )

        if team.git.agent_enabled(payload.project_id, role):
            lock = git_run_locks.setdefault(payload.project_id, asyncio.Lock())
            async with lock:
                root = team._project_root(payload.project_id)
                await asyncio.to_thread(team.git.begin_agent_run, payload.project_id, role, root)
                result = await call_provider()
                try:
                    commit = await asyncio.to_thread(
                        team.git.finish_agent_run, payload.project_id, role, run_id, root, payload.message,
                    )
                except GitWorkflowError as exc:
                    message = f"Git workflow could not commit this agent run: {exc}"
                    team.configs.add_message(role, "error", message, "git", "", payload.project_id)
                    result["ok"] = False
                    result["response"] = message
                else:
                    if commit:
                        result["git_commit"] = commit
                        team.configs.add_message(
                            role, "app",
                            f"Committed {commit['commit_hash'][:12]} on agent branch '{commit['agent_branch']}' and "
                            f"merged it into the main branch '{commit['main_branch']}': {commit['message']} "
                            f"({len(commit['files'])} changed file{'s' if len(commit['files']) != 1 else ''}).",
                            "git", commit["commit_hash"], payload.project_id,
                        )
        else:
            result = await call_provider()
        status = "error" if result.get("ok") is False else "completed"
        team.configs.update_chat_run(run_id, status, result=result,
                                     error=result.get("response", "") if status == "error" else "")
        print(
            f"[chat-run] {status} role={role} project={payload.project_id} run={run_id}",
            flush=True,
        )
    except asyncio.CancelledError:
        team.configs.update_chat_run(
            run_id, "error", error="The server stopped before this agent run finished. Send the prompt again."
        )
        print(f"[chat-run] cancelled role={role} project={payload.project_id} run={run_id}", flush=True)
        raise
    except Exception as exc:
        team.configs.update_chat_run(run_id, "error", error=f"Agent run failed: {exc}")
        print(
            f"[chat-run] error role={role} project={payload.project_id} run={run_id}: {exc}",
            flush=True,
        )


@app.post("/api/chat/{role}", status_code=202)
async def chat(role: str, payload: ChatInput):
    try:
        team.definitions.get(role, payload.project_id)
        projects.get(payload.project_id)
        # Human/API-submitted prompts always remain visible. Only the internal
        # dispatcher is allowed to suppress its continuation prompt bubble.
        payload.record_user_message = True
        run = _schedule_chat_run(role, payload)
        return {"run": run}
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Agent run failed: {exc}") from exc


@app.get("/api/chat-runs/{run_id}")
async def chat_run(run_id: str):
    run = team.configs.chat_run(run_id)
    if not run:
        raise HTTPException(404, "Chat run not found")
    return run


@app.get("/api/agents/{role}/active-run")
async def active_chat_run(role: str, project_id: int = 1):
    try:
        team.definitions.get(role, project_id)
        projects.get(project_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"run": team.configs.active_chat_run(role, project_id)}


@app.get("/api/projects/{project_id}/github")
async def github_report(project_id: int):
    try:
        project = projects.get(project_id)
        report = collect_git_report(project.get("root_path") or ROOT)
        return {"ok": True, "report": format_git_report(report), "details": report}
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except GitReportError as exc:
        return {"ok": False, "report": f"GitHub report unavailable\n\n{exc}"}

@app.post("/api/native-command/{role}")
async def native_command(role: str, payload: NativeCommandInput):
    try:
        team.definitions.get(role, payload.project_id)
        projects.get(payload.project_id)
        return await team.native_command(role, payload.command.strip(), payload.project_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Native command failed: {exc}") from exc


@app.post("/api/agents/{role}/app-message", status_code=201)
async def app_message(role: str, payload: AppMessageInput):
    print("Sent a message through the app")
    print(payload, flush=True)
    try:
        team.definitions.get(role, payload.project_id)
        projects.get(payload.project_id)
        return team.configs.add_message(role, "app", payload.content, "app", "", payload.project_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/context")
async def list_context(project_id: int = 1):
    try:
        projects.get(project_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return store.list(project_id=project_id)


@app.post("/api/context", status_code=201)
async def create_context(payload: ContextInput):
    try:
        projects.get(payload.project_id)
        return store.save(payload.title, payload.content, payload.roles, project_id=payload.project_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.put("/api/context/{item_id}")
async def update_context(item_id: int, payload: ContextInput):
    try:
        projects.get(payload.project_id)
        return store.save(payload.title, payload.content, payload.roles, item_id, payload.project_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.delete("/api/context/{item_id}", status_code=204)
async def delete_context(item_id: int, project_id: int = 1):
    if not store.delete(item_id, project_id):
        raise HTTPException(404, "Context item not found")


if __name__ == "__main__":
    import uvicorn

    host = "127.0.0.1"
    start_port = int(os.getenv("PORT_START", "8000"))
    listener, port = bind_available_port(host, start_port)
    print(f"Agent Team Workspace listening at http://{host}:{port}", flush=True)
    config = uvicorn.Config(app, host=host, port=port, reload=False)
    server = uvicorn.Server(config)
    try:
        server.run(sockets=[listener])
    except KeyboardInterrupt:
        pass
    finally:
        listener.close()
