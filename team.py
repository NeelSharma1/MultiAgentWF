from __future__ import annotations

import asyncio
import base64
import os
import shutil
import tempfile
import json
import re
import uuid
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from agents import Agent, OpenAIChatCompletionsModel, Runner, RunConfig
from agents.mcp import MCPServerStdio
from openai import AsyncOpenAI
import httpx
import pyte

from terminal import create_terminal
from runtime_config import PROVIDERS, RuntimeConfigStore
from agent_definitions import AgentDefinitionStore
from shared_context import ContextStore, ROLES
from project_store import ProjectStore
from skills import (
    SkillStore, normalize_skill_language, normalize_skill_platform, normalize_skill_secret_refs,
    normalize_skill_type, skill_slug,
)
from toolsets import ToolsetStore, resolve_tool_calls, toolset_slug
from git_workflow import GitWorkflowStore


ROLE_BRIEFS = {
    "orchestrator": "Clarify goals, plan work, coordinate specialists, reconcile results, and give the user a decisive next action.",
    "researcher": "Investigate questions, distinguish evidence from inference, identify gaps, and publish concise findings.",
    "programmer": "Design and implement robust software, explain tradeoffs, and produce testable technical work.",
    "reviewer": "Critically inspect plans and outputs for correctness, risk, missing cases, and unsupported claims.",
    "formatter": "Transform material into a clear requested structure while preserving meaning and consistency.",
    "documenter": "Create maintainable documentation, examples, decisions, and handoff notes for future readers.",
}

# Provider-native slash commands are intentionally kept separate from the
# workspace's `/app …` commands.  Codex is the only configured runtime with a
# documented interactive command surface; API-backed providers receive slash
# text as ordinary prompts, so their catalog is empty rather than inventing
# commands the provider does not expose.
PROVIDER_COMMANDS = {
    "codex": [
        {"name": "/status", "args": "", "description": "Show the exact Codex session, model, permissions, account, and usage status."},
        {"name": "/mcp", "args": "", "description": "Show the MCP servers and tools available to this Codex session."},
        {"name": "/skills", "args": "", "description": "Browse the skills available to the Codex session."},
        {"name": "/review", "args": "", "description": "Ask Codex to review the current changes."},
        {"name": "/review-branch", "args": "", "description": "Review the current branch against its base branch."},
        {"name": "/review-commit", "args": "", "description": "Review a commit in the current repository."},
        {"name": "/compact", "args": "", "description": "Compact the current Codex conversation."},
        {"name": "/model", "args": "", "description": "Switch the model or reasoning effort for the Codex session."},
        {"name": "/permissions", "args": "", "description": "Configure when Codex asks for confirmation."},
        {"name": "/usage", "args": "", "description": "Show Codex usage and remaining limits."},
        {"name": "/goal", "args": "", "description": "Set or inspect the current Codex goal when supported by the session."},
        {"name": "/ps", "args": "", "description": "List active Codex subagent tasks."},
        {"name": "/stop", "args": "", "description": "Stop an active Codex subagent task."},
        {"name": "/logout", "args": "", "description": "Log the Codex CLI account out."},
    ],
    "openai": [],
    "google": [],
    "anthropic": [],
    "compatible": [],
}

GOOGLE_TEXT_ONLY_INSTRUCTION = (
    "This is a direct Gemini API bridge. The only function tools available are "
    "send_agent_message and list_agent_messages. Use them only when inter-agent coordination is needed; "
    "list_shared_context, publish_shared_context, and skill tools are not available on this bridge. "
    "Do not emit tool calls or function calls other than these explicitly supplied provider tools. A textual TOOLCALL marker "
    "described by an assigned local toolset is allowed and will be handled after this response. Answer the user directly using the "
    "conversation and shared context included here."
)

GOOGLE_INTER_AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "send_agent_message",
            "description": "Send a durable command or report to another agent in this workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "recipient_role": {"type": "string", "description": "Target agent role ID."},
                    "relationship": {"type": "string", "enum": ["command", "report"]},
                    "content": {"type": "string", "description": "The actionable command or report."},
                },
                "required": ["recipient_role", "relationship", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_agent_messages",
            "description": "List the current agent's queued and delivered inter-agent messages.",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
]


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, provider: str = "", status_code: int | None = None,
                 code: str = "", request_id: str = "", body: Any = None) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.code = code
        self.request_id = request_id
        self.body = body

    def details(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "type": type(self).__name__,
            "message": str(self),
            "status_code": self.status_code,
            "code": self.code,
            "request_id": self.request_id,
            "body": self.body,
        }


def _codex_diagnostic(stdout: bytes | str = b"", stderr: bytes | str = b"") -> str:
    """Return a useful, bounded diagnostic from a non-interactive Codex run.

    Codex can emit a structured error over several lines.  Taking only the
    final line (which is often just ``}``) made the skills endpoint look like
    an unexplained 502.  Keep both streams, while limiting the response so a
    failed CLI invocation cannot flood the API response or browser alert.
    """
    def decode(value: bytes | str) -> str:
        return value.decode(errors="replace") if isinstance(value, bytes) else str(value)

    parts = []
    for value in (stderr, stdout):
        text = decode(value).strip()
        if text:
            parts.append(text)
    diagnostic = "\n".join(parts)
    if not diagnostic:
        return "Codex returned no diagnostic output."
    return diagnostic[-4000:]


def _json_from_codex_output(raw: str) -> dict[str, Any]:
    """Parse a structured Codex response, tolerating accidental code fences."""
    candidate = raw.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(candidate[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("Codex returned a JSON value instead of an object")
    return parsed


def _prefer_windows_native_codex(command: str) -> str:
    """Prefer the native Codex binary beside an npm launcher shim."""
    if os.name != "nt":
        return command
    command_path = Path(command)
    if command_path.suffix.lower() not in {".cmd", ".bat", ".ps1"}:
        return command
    node_modules = command_path.parent.parent
    candidates = sorted(
        node_modules.glob("@openai/codex-win32-*/vendor/*/bin/codex.exe"),
        reverse=True,
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    return command


def resolve_codex_command() -> str | None:
    """Find Codex even when a GUI-launched server has a minimal PATH."""
    configured = os.getenv("CODEX_COMMAND", "").strip()
    if configured:
        configured_path = Path(configured).expanduser()
        if configured_path.is_file() and os.access(configured_path, os.X_OK):
            return _prefer_windows_native_codex(str(configured_path.resolve()))
        configured_command = shutil.which(configured)
        if configured_command:
            return _prefer_windows_native_codex(str(Path(configured_command).resolve()))

    command_names = ("codex.exe", "codex.cmd", "codex.bat", "codex") if os.name == "nt" else ("codex",)
    for command_name in command_names:
        discovered = shutil.which(command_name)
        if discovered:
            return _prefer_windows_native_codex(str(Path(discovered).resolve()))

    if os.name == "nt":
        jetbrains_windows_root = Path.home() / "AppData" / "Local" / "JetBrains"
        patterns = (
            "*/acp-agents/.runtimes/node/*/npm-cache/_npx/*/node_modules/@openai/"
            "codex-win32-*/vendor/*/bin/codex.exe",
            "*/acp-agents/.runtimes/node/*/npm-cache/_npx/*/node_modules/.bin/codex.cmd",
        )
        for pattern in patterns:
            for candidate in sorted(jetbrains_windows_root.glob(pattern), reverse=True):
                if candidate.is_file():
                    return _prefer_windows_native_codex(str(candidate.resolve()))

    # JetBrains launches Python with a smaller PATH than an interactive shell.
    # Include conventional install locations before checking its bundled runtime.
    search_path = os.pathsep.join([
        str(Path.home() / ".local" / "bin"),
        str(Path.home() / ".npm-global" / "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ])
    discovered = shutil.which("codex", path=search_path)
    if discovered:
        return str(Path(discovered).resolve())

    jetbrains_root = Path.home() / "Library" / "Caches" / "JetBrains"
    patterns = (
        "*/acp-agents/.runtimes/node/*/npm-cache/_npx/*/node_modules/.bin/codex",
        "*/aia/agents/.runtimes/node/*/npm-cache/_npx/*/node_modules/.bin/codex",
    )
    for pattern in patterns:
        for candidate in sorted(jetbrains_root.glob(pattern), reverse=True):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate.resolve())

    # Last resort: ask the user's login shell, whose startup files may add Codex.
    shell = os.getenv("SHELL", "/bin/zsh")
    try:
        result = subprocess.run(
            [shell, "-lic", "command -v codex"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        candidate = Path(result.stdout.strip().splitlines()[-1]).expanduser()
        if result.returncode == 0 and candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    except (IndexError, OSError, subprocess.SubprocessError):
        pass
    return None


def codex_process_args(args: list[str]) -> list[str]:
    """Return an executable argument vector that works with Windows shims."""
    if not args or os.name != "nt":
        return args
    command = Path(args[0])
    suffix = command.suffix.lower()
    if suffix in {".cmd", ".bat"}:
        shell = os.getenv("COMSPEC") or shutil.which("cmd.exe") or "cmd.exe"
        return [shell, "/d", "/s", "/c", subprocess.list2cmdline(args)]
    if suffix == ".ps1":
        powershell = shutil.which("powershell.exe") or "powershell.exe"
        return [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", *args]
    return args


def codex_process_env() -> dict[str, str]:
    """Give every Codex subprocess the same ChatGPT login profile."""
    env = os.environ.copy()
    user_home = str(Path.home())
    env.setdefault("HOME", user_home)
    env.setdefault("USERPROFILE", user_home)
    if os.name == "nt":
        env.setdefault("HOMEDRIVE", Path(user_home).drive)
        env.setdefault("HOMEPATH", str(Path(user_home).relative_to(Path(user_home).anchor)))
    env.setdefault("CODEX_HOME", str(Path(user_home) / ".codex"))
    return env


CODEX_AUTH_MESSAGE = (
    "Codex is not authenticated with your ChatGPT account. "
    "Open Connections, choose Connect Codex, complete the OpenAI sign-in, and try again."
)


def _codex_auth_failure(diagnostic: str) -> bool:
    """Recognize the CLI's missing or expired ChatGPT credential diagnostics."""
    lowered = diagnostic.lower()
    return "401 unauthorized" in lowered and any(
        marker in lowered
        for marker in ("missing bearer", "basic authentication", "not authenticated", "invalid api key")
    )


class AgentTeam:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.db_path = root / "data" / "workspace.db"
        # Create workspaces before the project-scoped agent/config stores so
        # their migrations can copy legacy global records into existing projects.
        self.projects = ProjectStore(self.db_path)
        self.configs = RuntimeConfigStore(self.db_path)
        self.definitions = AgentDefinitionStore(self.db_path)
        self.context = ContextStore(self.db_path)
        self.skills = SkillStore(self.db_path)
        self.toolsets = ToolsetStore()
        self.git = GitWorkflowStore(self.db_path)
        self.mcp: MCPServerStdio | None = None
        self.codex_login_process: asyncio.subprocess.Process | None = None
        self.codex_login_output = ""
        self._codex_chat_locks: dict[tuple[int, str], asyncio.Lock] = {}

    def _codex_command(self) -> str | None:
        return resolve_codex_command()

    def _project_root(self, project_id: int) -> Path:
        try:
            project = self.projects.get(project_id)
            requested = str(project.get("root_path") or "").strip()
            candidate = Path(requested).expanduser().resolve() if requested else self.root.resolve()
            return candidate if candidate.is_dir() else self.root.resolve()
        except (KeyError, OSError, RuntimeError):
            return self.root.resolve()

    async def start(self) -> None:
        python_command = os.getenv("PYTHON", "").strip() or sys.executable
        self.mcp = MCPServerStdio(
            name="shared-context",
            params={
                "command": python_command,
                "args": [str(self.root / "mcp_server.py")],
                "env": {"WORKSPACE_DB": str(self.db_path)},
            },
            cache_tools_list=True,
        )
        await self.mcp.connect()

    async def stop(self) -> None:
        if self.codex_login_process and self.codex_login_process.returncode is None:
            self.codex_login_process.terminate()
        if self.mcp:
            await self.mcp.cleanup()

    async def codex_login_status(self) -> dict:
        command = self._codex_command()
        if not command:
            return {"connected": False, "detail": "Codex CLI not found", "login_output": self.codex_login_output}
        process = await asyncio.create_subprocess_exec(
            *codex_process_args([command, "login", "status"]),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=codex_process_env(),
        )
        output, _ = await process.communicate()
        detail = output.decode(errors="replace").strip()
        connected = process.returncode == 0
        return {"connected": connected, "detail": detail, "login_output": self.codex_login_output,
                "command": command}

    async def start_codex_login(self) -> dict:
        command = self._codex_command()
        if not command:
            raise ProviderError("Codex CLI not found")
        if self.codex_login_process and self.codex_login_process.returncode is None:
            return await self.codex_login_status()
        self.codex_login_output = "Starting Codex device login…"
        self.codex_login_process = await asyncio.create_subprocess_exec(
            *codex_process_args([command, "login", "--device-auth"]),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=codex_process_env(),
        )
        asyncio.create_task(self._capture_codex_login())
        await asyncio.sleep(0.5)
        return await self.codex_login_status()

    async def _capture_codex_login(self) -> None:
        assert self.codex_login_process and self.codex_login_process.stdout
        chunks = []
        while line := await self.codex_login_process.stdout.readline():
            chunks.append(line.decode(errors="replace"))
            self.codex_login_output = "".join(chunks)[-5000:]
        await self.codex_login_process.wait()

    async def codex_logout(self) -> None:
        command = self._codex_command()
        if not command:
            raise ProviderError("Codex CLI not found")
        process = await asyncio.create_subprocess_exec(
            *codex_process_args([command, "logout"]),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=codex_process_env(),
        )
        await process.communicate()
        if process.returncode:
            raise ProviderError("Codex logout failed")
        self.codex_login_output = ""

    def provider_status(self) -> list[dict[str, Any]]:
        codex_command = self._codex_command()
        result = []
        for provider_id, provider in PROVIDERS.items():
            env_name = provider["api_key_env"]
            available = bool(codex_command) if provider_id == "codex" else bool(os.getenv(env_name))
            if provider_id == "compatible":
                available = True  # many local servers accept a placeholder key
            result.append({"id": provider_id, **provider, "available": available})
        return result

    def _skill_guidance(self, role: str, project_id: int = 1) -> str:
        assigned_skills = self.skills.summaries(project_id, role)
        if assigned_skills:
            try:
                project = self.projects.get(project_id)
                project_root = Path(str(project.get("root_path") or self.root)).expanduser().resolve()
                self.skills.materialize(project_root, project_id, role)
            except (KeyError, OSError, ValueError):
                # Skill metadata remains usable through MCP even when a
                # project folder is unavailable for native ACP materialization.
                pass
            skill_lines = "\n".join(
                f"- {item['name']} (skill_id={item['id']}, slug={item['slug']}, "
                f"version={item.get('version', '1.0.0')}, platform={item.get('platform', 'any')}, "
                f"type={item.get('type', 'general')}): {item['description']} "
                f"Inputs: {json.dumps(item['inputs'], ensure_ascii=False)}. "
                f"Outputs: {json.dumps(item['outputs'], ensure_ascii=False)}. "
                f"Output format: {item.get('output_format', 'text')}. "
                f"Required secret references (names only): {', '.join(ref['name'] for ref in item.get('required_secrets', [])) or 'none'}."
                for item in assigned_skills
            )
            skill_guidance = (
                "\n\nAssigned ACP-compatible Agent Skills (discovery metadata only; load the SKILL.md body on demand):\n"
                f"{skill_lines}\n"
                "Use list_assigned_skills for the catalog, load_assigned_skill before following a skill, "
                "read_skill_resource for references/assets, and run_assigned_skill with the skill_id and a JSON "
                "inputs object when an executable helper is appropriate. Do not recreate a skill's script in your response."
            )
        else:
            return "\n\nNo reusable skills are assigned to you in this workspace."
        return skill_guidance

    def _tool_guidance(self, role: str, project_id: int = 1) -> str:
        project_root = self._project_root(project_id)
        assigned = self.toolsets.list(project_root, project_id, role)
        if not assigned:
            return "\n\nNo local command-line toolsets are assigned to you in this workspace."
        lines = "\n".join(
            f"- {item['name']}: {item['description']} (file: {item['filename']})"
            for item in assigned
        )
        return (
            "\n\nYou have access to the following tools. When applicable, ALWAYS use the tools below to "
            "complete a task. Each entry is a short description of one toolset and gives its filename in "
            "the working directory's .agents/tools folder:\n"
            f"{lines}\n\n"
            "If a toolset is needed, load its listed TOOLSET.md file and inspect only the Tool summary section. "
            "If direct file access is unavailable, use load_toolset_summary. That section contains each tool's "
            "name, short description, inputs, outputs, and executable filename. If a tool is useful, emit this "
            "exact marker on its own line:\n\n"
            "TOOLCALL - <toolset>/<tool name> - [arguments].\n\n"
            "Arguments must be one valid JSON list in positional order and the marker must end with a period. "
            "Do not wrap the marker in Markdown. The local workspace will execute the configured file and replace "
            "the marker with the tool's formatted result in the chat. Never invent a toolset or tool name."
        )

    def _git_guidance(self, role: str, project_id: int = 1) -> str:
        if not self.git.agent_enabled(project_id, role):
            return ""
        configuration = self.git.configuration(project_id)
        if not configuration:
            return ""
        main_branch = configuration.get("main_branch") or configuration["branch"]
        return (
            f"\n\nThis is a Git-enabled agent workflow. The app checks out your dedicated '{role}' branch from "
            f"the main branch '{main_branch}' before you work, then commits and merges it into '{main_branch}' "
            "after your response. "
            "Work only in the current working tree. Never create, switch, merge, rebase, reset, or delete Git branches, "
            "and do not run git commit, git add, git push, git pull, git revert, or git reset. The workspace captures "
            "the completed series of file changes as one commit and handles the merge automatically."
        )

    def _instructions(self, role: str, project_id: int = 1) -> str:
        definition = self.definitions.get(role, project_id)
        roster = ", ".join(
            f"{item['role']} ({item['name']})" for item in self.definitions.list(project_id)
        ) or "no other agents"
        graph = "; ".join(
            f"{edge['source_role']} {'commands' if edge['relationship'] == 'command' else 'reports to'} {edge['target_role']}"
            for edge in self.projects.edges(project_id)
        ) or "no explicit graph relationships"
        return (
            "You are one member of the user's agent team. Use list_shared_context at the start of substantive work "
            "to incorporate relevant team knowledge. Publish durable findings or decisions with publish_shared_context. "
            f"You can communicate directly with other agents using send_agent_message with sender_role='{role}', "
            "a recipient_role, relationship='command' or 'report', and concise content. Use "
            "list_agent_messages to inspect your inbox. Commands are actionable work requests; reports are findings, "
            "status, or decisions. Messages are durable, automatically start an idle recipient run, and are "
            "synthesized into its next prompt. Always use send_agent_message for delegation so the recipient has "
            "its own chat transcript and run history. "
            f"Available recipient role IDs in this workspace: {roster}. "
            f"Current graph relationships: {graph}. "
            f"This conversation belongs to workspace {project_id}. Always pass project_id={project_id} to shared context tools. "
            "Never claim another agent completed work unless the conversation or shared context shows it. "
            f"Be direct, practical, and identify assumptions. Your role is {definition['name']}: {definition['instructions']}"
            f"{self._skill_guidance(role, project_id)}"
            f"{self._tool_guidance(role, project_id)}"
            f"{self._git_guidance(role, project_id)}"
        )

    def _model(self, config: dict[str, str]):
        provider, model = config["provider"], config["model"]
        if provider == "openai":
            if not os.getenv("OPENAI_API_KEY"):
                raise ProviderError("OPENAI_API_KEY is not configured")
            return model
        if provider == "google":
            env_name = config["api_key_env"] or "GEMINI_API_KEY"
            key = os.getenv(env_name)
            if not key:
                raise ProviderError(f"{env_name} is not configured")
            client = AsyncOpenAI(api_key=key, base_url="https://generativelanguage.googleapis.com/v1beta/openai/")
            return OpenAIChatCompletionsModel(model=model, openai_client=client)
        if provider == "anthropic":
            env_name = config["api_key_env"] or "ANTHROPIC_API_KEY"
            key = os.getenv(env_name)
            if not key:
                raise ProviderError(f"{env_name} is not configured")
            client = AsyncOpenAI(api_key=key, base_url="https://api.anthropic.com/v1/")
            return OpenAIChatCompletionsModel(model=model, openai_client=client)
        if provider == "compatible":
            env_name = config["api_key_env"] or "LOCAL_API_KEY"
            key = os.getenv(env_name, "local-not-required")
            client = AsyncOpenAI(api_key=key, base_url=config["base_url"])
            return OpenAIChatCompletionsModel(model=model, openai_client=client)
        raise ProviderError(f"{provider} is not an Agents SDK provider")

    def _agent(self, role: str, config: dict[str, str], project_id: int = 1) -> Agent:
        if not self.mcp:
            raise ProviderError("MCP server is not connected")
        return Agent(
            name=self.definitions.get(role, project_id)["name"],
            instructions=self._instructions(role, project_id),
            model=self._model(config),
            mcp_servers=[self.mcp],
        )

    def _conversation_prompt(self, role: str, message: str, project_id: int = 1,
                             reply_to_id: int | None = None,
                             exclude_message_ids: set[int] | None = None) -> str:
        history = self.configs.history(role, project_id=project_id)
        excluded = exclude_message_ids or set()
        reply = self.configs.message(reply_to_id, role, project_id) if reply_to_id else None
        if reply_to_id and not reply:
            raise ValueError("The message being replied to does not exist in this chat")
        reply_text = f"\n\n<replying_to>\n{reply['content']}\n</replying_to>" if reply else ""
        transcript_items = []
        for item in history:
            if item["speaker"] == "agent" and int(item["id"]) in excluded:
                continue
            speaker = item["speaker"]
            if speaker == "agent":
                source = item.get("source_role") or "team"
                kind = item.get("message_kind") or "message"
                if kind == "command":
                    label = f"[COMMAND from {source}]"
                    content = self._command_prompt_content(item.get("content") or "")
                elif kind == "report":
                    label = f"[REPORT from {source}]"
                    content = str(item.get("content") or "")
                else:
                    label = f"{source} ({kind})"
                    content = str(item.get("content") or "")
            elif speaker == "assistant":
                label = self.definitions.get(role, project_id)["name"]
                content = str(item.get("content") or "")
            else:
                label = speaker.title()
                content = str(item.get("content") or "")
            transcript_items.append(f"{label}: {content}")
        if not transcript_items:
            return f"{message}{reply_text}"
        transcript = "\n".join(transcript_items)
        return f"<conversation_history>\n{transcript}\n</conversation_history>\n\nUser: {message}{reply_text}"

    @staticmethod
    def _command_prompt_content(content: str) -> str:
        """Flatten a stored command envelope into ordinary provider prompt text."""
        parts = RuntimeConfigStore.command_parts(content)
        if len(parts) > 1:
            return "\n".join(f"- {part}" for part in parts)
        return parts[0] if parts else ""

    @staticmethod
    def _inter_agent_prompt(messages: list[dict[str, Any]]) -> str:
        if not messages:
            return ""
        lines = [
            "Incoming team messages. Treat each [COMMAND ...] entry as an ordinary actionable command, "
            "and each [REPORT ...] entry as context for your next response."
        ]
        for item in messages:
            kind = "command" if item.get("message_kind") == "command" else "report"
            sender = item.get("source_role") or "team"
            timestamp = item.get("created_at") or ""
            at = f" at {timestamp}" if timestamp else ""
            lines.append(f"[{kind.upper()} from {sender}{at}]")
            content = item.get("content") or ""
            lines.append(
                AgentTeam._command_prompt_content(content)
                if kind == "command" else str(content)
            )
        lines.append(
            "Synthesize these commands and reports into your next action. "
            "Do not claim a command was completed until you actually complete it."
        )
        return "\n".join(lines)

    def send_agent_message(self, sender_role: str, recipient_role: str, content: str,
                           relationship: str, project_id: int = 1) -> dict[str, Any]:
        """Validate and persist an inter-agent message for this project."""
        self.projects.get(project_id)
        self.definitions.get(sender_role, project_id)
        self.definitions.get(recipient_role, project_id)
        message = self.configs.send_agent_message(
            sender_role, recipient_role, content, relationship, project_id,
        )
        active = self.configs.active_chat_run(recipient_role, project_id)
        message["recipient_active"] = bool(active)
        message["queued_for_next_prompt"] = True
        message["active_run_id"] = active["id"] if active else None
        return message

    @staticmethod
    def _data_url(attachment: dict[str, Any]) -> str:
        encoded = base64.b64encode(Path(attachment["path"]).read_bytes()).decode()
        return f"data:{attachment['mime_type']};base64,{encoded}"

    @staticmethod
    def _message_text(content: Any) -> str:
        """Normalize OpenAI-compatible text content, including provider part arrays."""
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            else:
                text = getattr(item, "text", None)
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)

    def _attachment_text(self, attachments: list[dict[str, Any]]) -> str:
        parts = []
        for item in attachments:
            mime = item["mime_type"]
            path = Path(item["path"])
            if mime.startswith("text/") or mime in {"application/json", "application/xml", "application/javascript"}:
                content = path.read_text(errors="replace")[:100_000]
                parts.append(f"<attached_file name={json.dumps(item['name'])}>\n{content}\n</attached_file>")
            elif not mime.startswith("image/"):
                parts.append(f"Attached file {item['name']} is available locally at {path}.")
        return "\n\n".join(parts)

    def _agents_input(self, role: str, message: str, project_id: int, reply_to_id: int | None,
                      attachments: list[dict[str, Any]], provider: str,
                      exclude_message_ids: set[int] | None = None) -> str | list[dict[str, Any]]:
        prompt = self._conversation_prompt(role, message, project_id, reply_to_id, exclude_message_ids)
        attachment_text = self._attachment_text(attachments)
        if attachment_text:
            prompt += f"\n\n{attachment_text}"
        rich: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
        for item in attachments:
            if item["mime_type"].startswith("image/"):
                rich.append({"type": "input_image", "image_url": self._data_url(item), "detail": "auto"})
            elif provider == "openai" and item["mime_type"] == "application/pdf":
                rich.append({"type": "input_file", "filename": item["name"], "file_data": self._data_url(item)})
        return [{"role": "user", "content": rich}] if len(rich) > 1 else prompt

    async def _agents_chat(self, role: str, message: str, config: dict[str, str], project_id: int = 1,
                           reply_to_id: int | None = None,
                           attachments: list[dict[str, Any]] | None = None,
                           exclude_message_ids: set[int] | None = None) -> dict[str, str]:
        agent = self._agent(role, config, project_id)
        # Delegation must go through the durable send_agent_message MCP tool.
        # Native Agents SDK handoffs run inside this provider task, bypass the
        # recipient's SQLite transcript/chat_run, and therefore make a
        # researcher appear not to have run. The dispatcher in main.py starts
        # the recipient's own provider task as soon as that durable message is
        # written, preserving the group-chat and run history for every agent.
        result = await Runner.run(
            agent,
            self._agents_input(
                role, message, project_id, reply_to_id, attachments or [], config["provider"],
                exclude_message_ids,
            ),
            run_config=RunConfig(tracing_disabled=config["provider"] != "openai"),
        )
        return {"response": str(result.final_output), "answered_by": result.last_agent.name}

    async def _google_chat(self, role: str, message: str, config: dict[str, str], project_id: int = 1,
                           reply_to_id: int | None = None,
                           attachments: list[dict[str, Any]] | None = None,
                           exclude_message_ids: set[int] | None = None) -> dict[str, str]:
        """Use Gemini's OpenAI-compatible chat surface with a small coordination tool bridge."""
        env_name = config["api_key_env"] or "GEMINI_API_KEY"
        key = os.getenv(env_name)
        if not key:
            raise ProviderError(f"{env_name} is not configured")
        shared = self.context.list(role, project_id)
        context_text = "\n\n".join(f"[{item['title']}]\n{item['content']}" for item in shared) or "No shared context."
        messages: list[dict[str, Any]] = [{
            "role": "system",
            "content": (
                f"{self._instructions(role, project_id)}\n\nCurrent shared team context:\n{context_text}\n\n"
                f"{GOOGLE_TEXT_ONLY_INSTRUCTION}"
            ),
        }]
        excluded = exclude_message_ids or set()
        for item in self.configs.history(role, project_id=project_id):
            if item["speaker"] == "agent" and int(item["id"]) in excluded:
                continue
            if item["speaker"] in {"user", "assistant"}:
                messages.append({"role": "user" if item["speaker"] == "user" else "assistant", "content": item["content"]})
            elif item["speaker"] == "agent":
                source = item.get("source_role") or "team"
                kind = item.get("message_kind") or "message"
                content = str(item.get("content") or "")
                if kind == "command":
                    content = self._command_prompt_content(content)
                    label = f"[COMMAND from {source}]"
                elif kind == "report":
                    label = f"[REPORT from {source}]"
                else:
                    label = f"[{kind.upper()} from {source}]"
                messages.append({"role": "user", "content": f"{label}\n{content}"})
        reply = self.configs.message(reply_to_id, role, project_id) if reply_to_id else None
        if reply_to_id and not reply:
            raise ValueError("The message being replied to does not exist in this chat")
        prompt = message
        if reply:
            prompt += f"\n\n<replying_to>\n{reply['content']}\n</replying_to>"
        attachment_text = self._attachment_text(attachments or [])
        if attachment_text:
            prompt += f"\n\n{attachment_text}"
        content: str | list[dict[str, Any]] = prompt
        images = [item for item in (attachments or []) if item["mime_type"].startswith("image/")]
        if images:
            content = [{"type": "text", "text": prompt}]
            content.extend({"type": "image_url", "image_url": {"url": self._data_url(item)}} for item in images)
        messages.append({"role": "user", "content": content})
        # Google's model listing uses `models/…`; its OpenAI-compatible chat API expects the bare ID.
        model = config["model"].removeprefix("models/")
        tool_history_start = len(messages)
        async def create_completion(client: AsyncOpenAI, use_tools: bool):
            kwargs: dict[str, Any] = {"model": model, "messages": messages}
            if use_tools:
                kwargs["tools"] = GOOGLE_INTER_AGENT_TOOLS
            return await client.chat.completions.create(**kwargs)

        def call_value(call: Any, key: str, default: Any = "") -> Any:
            if isinstance(call, dict):
                return call.get(key, default)
            return getattr(call, key, default)

        async with AsyncOpenAI(
            api_key=key, base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
        ) as client:
            use_tools = True
            for _ in range(5):
                try:
                    completion = await create_completion(client, use_tools)
                except Exception as exc:
                    status = getattr(exc, "status_code", None)
                    detail = getattr(exc, "message", None) or str(exc)
                    body = getattr(exc, "body", None)
                    # A provider/model may advertise chat completions but not
                    # function calling. Retry this request as text-only so a
                    # coordination capability never hides an otherwise valid answer.
                    if use_tools and (status in {400, 404, 422}
                                      or (isinstance(exc, TypeError) and "tools" in str(exc).lower())):
                        use_tools = False
                        # If a malformed/unsupported tool round was already
                        # appended, discard it before the text-only retry.
                        messages = messages[:tool_history_start]
                        continue
                    code = ""
                    if isinstance(body, dict):
                        nested_error = body.get("error")
                        code = str(body.get("code") or (nested_error.get("code") if isinstance(nested_error, dict) else "") or "")
                    request_id = str(getattr(exc, "request_id", "") or "")
                    response = getattr(exc, "response", None)
                    if response is not None and not request_id:
                        request_id = response.headers.get("x-request-id", "") or response.headers.get("x-goog-request-id", "")
                    raise ProviderError(
                        detail, provider="google", status_code=status, code=code,
                        request_id=request_id, body=body,
                    ) from exc
                if not completion.choices:
                    raise ProviderError(
                        "Gemini returned no choices",
                        provider="google", code="empty_choices",
                        body={"model": getattr(completion, "model", model), "choices": 0},
                    )
                choice = completion.choices[0]
                calls = call_value(choice.message, "tool_calls", None) or []
                response = self._message_text(call_value(choice.message, "content", None))
                if calls and use_tools:
                    assistant_call = {
                        "role": "assistant",
                        "content": response or None,
                        "tool_calls": [],
                    }
                    for call in calls:
                        function = call_value(call, "function", {}) or {}
                        name = call_value(function, "name", "")
                        arguments = call_value(function, "arguments", "{}") or "{}"
                        call_id = call_value(call, "id", "") or uuid.uuid4().hex
                        assistant_call["tool_calls"].append({
                            "id": call_id, "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        })
                        extra_content = call_value(call, "extra_content", None)
                        if extra_content is not None:
                            if hasattr(extra_content, "model_dump"):
                                extra_content = extra_content.model_dump()
                            assistant_call["tool_calls"][-1]["extra_content"] = extra_content
                        try:
                            parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
                            if not isinstance(parsed, dict):
                                raise ValueError("tool arguments must be a JSON object")
                            if name == "send_agent_message":
                                tool_result = self.send_agent_message(
                                    role, str(parsed.get("recipient_role") or ""),
                                    str(parsed.get("content") or ""),
                                    str(parsed.get("relationship") or ""), project_id,
                                )
                            elif name == "list_agent_messages":
                                tool_result = {"messages": self.configs.agent_inbox(role, project_id)}
                            else:
                                tool_result = {"ok": False, "error": f"Unsupported Gemini tool: {name}"}
                        except Exception as exc:
                            tool_result = {"ok": False, "error": str(exc)}
                        messages.append({
                            "role": "tool", "name": name, "tool_call_id": call_id,
                            "content": json.dumps(tool_result, default=str),
                        })
                    messages.append(assistant_call)
                    # The OpenAI-compatible protocol expects the assistant tool
                    # call before its tool results. Move the just-created call
                    # ahead of those results while preserving earlier history.
                    tool_start = len(messages) - len(calls) - 1
                    assistant_entry = messages.pop()
                    tool_entries = messages[tool_start:]
                    del messages[tool_start:]
                    messages.append(assistant_entry)
                    messages.extend(tool_entries)
                    continue
                if response.strip():
                    return {"response": response.strip(), "answered_by": self.definitions.get(role, project_id)["name"]}
                tool_names = [str(call_value(call_value(call, "function", {}), "name", "unknown")) for call in calls]
                if calls:
                    code = "tool_call_without_tools" if not use_tools else "tool_call_loop_exhausted"
                    raise ProviderError(
                        "Gemini returned a tool call without usable text; the call could not be completed.",
                        provider="google", code=code,
                        body={"model": getattr(completion, "model", model), "finish_reason": getattr(choice, "finish_reason", ""),
                              "tool_calls": tool_names},
                    )
                raise ProviderError(
                    "Gemini returned an empty response",
                    provider="google", code="empty_response",
                    body={"model": getattr(completion, "model", model), "finish_reason": getattr(choice, "finish_reason", "")},
                )
        raise ProviderError("Gemini did not return a final response after processing tool calls", provider="google", code="tool_call_loop_exhausted")

    async def _codex_chat(self, role: str, message: str, config: dict[str, str], project_id: int = 1,
                          reply_to_id: int | None = None,
                          attachments: list[dict[str, Any]] | None = None) -> dict[str, str]:
        lock = self._codex_chat_locks.setdefault((project_id, role), asyncio.Lock())
        async with lock:
            return await self._codex_chat_locked(role, message, config, project_id, reply_to_id, attachments or [])

    async def _codex_chat_locked(self, role: str, message: str, config: dict[str, str], project_id: int,
                                 reply_to_id: int | None, attachments: list[dict[str, Any]]) -> dict[str, str]:
        command = self._codex_command()
        if not command:
            raise ProviderError("Codex CLI was not found. Set CODEX_COMMAND or install and sign in to Codex CLI.")
        login = await self.codex_login_status()
        if not login["connected"]:
            raise ProviderError(
                CODEX_AUTH_MESSAGE,
                provider="codex",
                status_code=401,
                code="codex_not_authenticated",
            )
        working_root = self._project_root(project_id)
        shared = self.context.list(role, project_id)
        context_text = "\n\n".join(f"[{item['title']}]\n{item['content']}" for item in shared) or "No shared context."
        existing = self.configs.codex_session(role, project_id)
        shared_prompt = f"<current_shared_context>\n{context_text}\n</current_shared_context>"
        reply = self.configs.message(reply_to_id, role, project_id) if reply_to_id else None
        if reply_to_id and not reply:
            raise ValueError("The message being replied to does not exist in this chat")
        reply_prompt = f"\n\nYou are replying specifically to:\n<replying_to>{reply['content']}</replying_to>" if reply else ""
        attachment_prompt = self._attachment_text(attachments)
        attachment_prompt = f"\n\n{attachment_prompt}" if attachment_prompt else ""
        if existing:
            prompt = (
                f"The shared team context, reusable skill assignments, and local toolsets may have changed since the prior turn.\n"
                f"{shared_prompt}\n{self._skill_guidance(role, project_id)}{self._tool_guidance(role, project_id)}{self._git_guidance(role, project_id)}\n\n"
                f"User: {message}{reply_prompt}{attachment_prompt}"
            )
        else:
            prompt = (
                f"{self._instructions(role, project_id)}\n\n"
                "You are a persistent conversational team member. Do not inspect secret files such as .env or .env.local. "
                "Keep continuity with future turns in this Codex session.\n\n"
                f"{shared_prompt}\n\nUser: {message}{reply_prompt}{attachment_prompt}"
            )
        with tempfile.TemporaryDirectory(prefix="agent-team-") as temp_dir:
            output_path = Path(temp_dir) / "last-message.txt"
            if existing:
                # `-C/--cd` belongs to the top-level `exec` command in current
                # Codex CLI releases. Putting it after `resume` makes the CLI
                # reject the invocation with only the unhelpful "try --help"
                # footer, which used to look like an account/usage failure.
                args = [command, "exec", "-C", str(working_root), "resume", "--skip-git-repo-check"]
            else:
                args = [command, "exec", "--sandbox", "read-only", "--skip-git-repo-check", "-C", str(working_root)]
            if config["model"]:
                args.extend(["--model", config["model"]])
            if config.get("reasoning_effort"):
                args.extend(["-c", f'model_reasoning_effort="{config["reasoning_effort"]}"'])
            for item in attachments:
                if item["mime_type"].startswith("image/"):
                    args.extend(["--image" if not existing else "-i", item["path"]])
            args.extend(["--json", "--output-last-message", str(output_path)])
            if existing:
                args.extend([existing["session_id"], "-"])
            else:
                args.append("-")
            process = await asyncio.create_subprocess_exec(
                *codex_process_args(args),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=codex_process_env(),
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(prompt.encode()), timeout=600)
            if process.returncode:
                diagnostic = _codex_diagnostic(stdout, stderr)
                if existing and any(token in diagnostic.lower() for token in ("session", "thread", "resume", "conversation")):
                    self.configs.clear_codex_session(role, project_id)
                if _codex_auth_failure(diagnostic):
                    raise ProviderError(
                        CODEX_AUTH_MESSAGE,
                        provider="codex",
                        status_code=401,
                        code="codex_not_authenticated",
                    )
                raise ProviderError(
                    f"Codex failed (exit code {process.returncode}):\n{diagnostic}",
                    provider="codex", status_code=process.returncode,
                    body={"command": args, "diagnostic": diagnostic},
                )
            session_id = existing["session_id"] if existing else ""
            for line in stdout.decode(errors="replace").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "thread.started":
                    session_id = event.get("thread_id", "")
                    break
            if not session_id:
                raise ProviderError("Codex started without returning a persistent session ID")
            self.configs.save_codex_session(
                role, project_id, session_id, config.get("model", ""), config.get("reasoning_effort", "")
            )
            response = output_path.read_text(encoding="utf-8", errors="replace").strip()
        if not response:
            raise ProviderError("Codex completed without a final response")
        return {"response": response, "answered_by": self.definitions.get(role, project_id)["name"]}

    async def list_models(self, provider: str, base_url: str = "", api_key_env: str = "") -> list[dict]:
        if provider == "codex":
            return await self._codex_models()
        if provider == "openai":
            key, url = os.getenv("OPENAI_API_KEY"), None
        elif provider == "google":
            key, url = os.getenv(api_key_env or "GEMINI_API_KEY"), "https://generativelanguage.googleapis.com/v1beta/openai/"
        elif provider == "anthropic":
            key = os.getenv(api_key_env or "ANTHROPIC_API_KEY")
            if not key:
                raise ProviderError(f"{api_key_env or 'ANTHROPIC_API_KEY'} is not configured")
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get("https://api.anthropic.com/v1/models", headers={"x-api-key": key, "anthropic-version": "2023-06-01"}, params={"limit": 1000})
                response.raise_for_status()
                models = response.json()["data"]
            result = []
            for item in models:
                result.append({"id": item["id"], "displayName": item.get("display_name", item["id"]), "description": "", "hidden": False, "supportedReasoningEfforts": [], "defaultReasoningEffort": ""})
            return result
        elif provider == "compatible":
            key, url = os.getenv(api_key_env or "LOCAL_API_KEY", "local-not-required"), base_url
        else:
            raise ProviderError("Unknown provider")
        if not key:
            raise ProviderError(f"{api_key_env or PROVIDERS[provider]['api_key_env']} is not configured")
        if provider == "compatible" and not url:
            raise ProviderError("Enter a base URL before loading models")
        async with AsyncOpenAI(api_key=key, base_url=url) as client:
            page = await client.models.list()
        return [{"id": item.id, "displayName": item.id, "description": "", "hidden": False,
                 "supportedReasoningEfforts": [], "defaultReasoningEffort": ""}
                for item in sorted(page.data, key=lambda item: item.id)]

    def provider_commands(self, provider: str) -> list[dict[str, str]]:
        """Return the native slash-command catalog for a provider.

        OpenAI-compatible APIs, Gemini, and Anthropic do not define a shared
        slash-command protocol.  Returning an empty catalog for those
        providers keeps arbitrary slash text available for passthrough while
        Codex gets its interactive command suggestions.
        """
        if provider not in PROVIDERS:
            raise ValueError("Unknown provider")
        return [dict(command) for command in PROVIDER_COMMANDS.get(provider, [])]

    async def _codex_models(self) -> list[dict]:
        command = self._codex_command()
        if not command:
            raise ProviderError("Codex CLI was not found")
        process = await asyncio.create_subprocess_exec(
            *codex_process_args([command, "app-server", "--stdio"]),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=codex_process_env(),
        )
        assert process.stdin and process.stdout
        request_id = 0

        async def request(method: str, params: dict) -> dict:
            nonlocal request_id
            request_id += 1
            process.stdin.write((json.dumps({"id": request_id, "method": method, "params": params}) + "\n").encode())
            await process.stdin.drain()
            while True:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=30)
                if not line:
                    raise ProviderError("Codex app-server closed while listing models")
                response = json.loads(line)
                if response.get("id") == request_id:
                    if "error" in response:
                        raise ProviderError(str(response["error"]))
                    return response["result"]

        try:
            await request("initialize", {"clientInfo": {"name": "MultiAgentWF", "version": "0.1.0"}, "capabilities": {}})
            process.stdin.write((json.dumps({"method": "initialized", "params": {}}) + "\n").encode())
            await process.stdin.drain()
            models, cursor = [], None
            while True:
                result = await request("model/list", {"includeHidden": True, "limit": 100, "cursor": cursor})
                models.extend(result["data"])
                cursor = result.get("nextCursor")
                if not cursor:
                    return models
        finally:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                process.kill()

    async def generate_agent_definition(self, prompt: str) -> dict[str, str]:
        command = self._codex_command()
        if not command:
            raise ProviderError("Codex CLI was not found")
        schema = {"type":"object","properties":{
            "name":{"type":"string","description":"Human-facing title in title case, such as Security Architect"},
            "role":{"type":"string","pattern":"^[a-z][a-z0-9_]{1,39}$","description":"Stable lowercase snake_case identifier, such as security_architect"},
            "brief":{"type":"string","description":"One-sentence summary of the specialist's responsibility"},
            "instructions":{"type":"string","description":"Specific operational instructions defining behavior, outputs, and boundaries"}},
            "required":["name","role","brief","instructions"],"additionalProperties":False}
        with tempfile.TemporaryDirectory(prefix="agent-team-definition-") as temp_dir:
            schema_path = Path(temp_dir) / "schema.json"
            output_path = Path(temp_dir) / "output.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            task = f"Design one focused AI team-member role from this request: {prompt}. Return only the requested structured fields. The role must be a lowercase snake_case ID. Instructions should be specific and operational."
            process = await asyncio.create_subprocess_exec(
                *codex_process_args([
                    command, "exec", "--ephemeral", "--sandbox", "read-only", "--skip-git-repo-check",
                    "-C", str(self.root), "--output-schema", str(schema_path),
                    "--output-last-message", str(output_path), "-",
                ]),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=codex_process_env(),
            )
            _, stderr = await asyncio.wait_for(process.communicate(task.encode()), timeout=600)
            if process.returncode:
                raise ProviderError(stderr.decode(errors="replace").strip().splitlines()[-1])
            draft = json.loads(output_path.read_text(encoding="utf-8"))
            if "_" in draft["name"] and draft["name"].lower() == draft["name"]:
                draft["name"] = draft["name"].replace("_", " ").title()
            if not re.fullmatch(r"[a-z][a-z0-9_]{1,39}", draft["role"]):
                draft["role"] = re.sub(r"[^a-z0-9]+", "_", draft["name"].lower()).strip("_")[:40]
            return draft

    async def generate_skill_definition(self, prompt: str) -> dict[str, Any]:
        command = self._codex_command()
        if not command:
            raise ProviderError("Codex CLI was not found")
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Human-facing skill name in title case"},
                "slug": {"type": "string", "pattern": "^[a-z][a-z0-9_-]{1,79}$", "description": "Stable lowercase ACP name/folder ID"},
                "summary": {"type": "string", "description": "One or two sentence SKILL.md description"},
                "version": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$", "description": "Portable skill package version"},
                "body": {"type": "string", "description": "Markdown instructions for the SKILL.md body, including when to use the skill, workflow, and safety boundaries"},
                "compatibility": {"type": "string", "description": "Optional OS, tool, runtime, or dependency requirements"},
                "license": {"type": "string", "description": "Optional license identifier"},
                "allowed_tools": {"type": "string", "description": "Optional ACP allowed-tools frontmatter value"},
                "required_secrets": {"type": "string", "description": "JSON-encoded array of secret references, each with name, label, description, and required; never include secret values"},
            },
            "required": ["name", "slug", "summary", "version", "body", "compatibility", "license", "allowed_tools", "required_secrets"],
            "additionalProperties": False,
        }
        with tempfile.TemporaryDirectory(prefix="agent-team-skill-") as temp_dir:
            schema_path = Path(temp_dir) / "schema.json"
            output_path = Path(temp_dir) / "output.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            task = (
                "Design one reusable portable Agent Skill from this request: "
                f"{prompt}. Return only the requested structured fields. The skill must be deterministic and "
                "self-contained. Do not create an executable script, input/output JSON schemas, or OS-specific variants. "
                "Return required_secrets as a JSON-encoded array of declarations such as "
                '[{"name":"WEATHER_API_KEY","label":"Weather API key","description":"Used for the weather service","required":true}]. '
                "Declare names only; never include API keys, tokens, passwords, or other secret values. "
                "The result must be portable Agent Skills format: name/description frontmatter plus a concise Markdown body. "
                "The body must say when to use the skill, give a safe repeatable workflow, and state its output contract. "
                "Keep discovery metadata concise because it is shown to agents before the body is loaded."
            )
            process = await asyncio.create_subprocess_exec(
                *codex_process_args([
                    command, "exec", "--ephemeral", "--sandbox", "read-only", "--skip-git-repo-check",
                    "-C", str(self.root), "--output-schema", str(schema_path),
                    "--output-last-message", str(output_path), "-",
                ]),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=codex_process_env(),
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(task.encode()), timeout=600)
            except asyncio.TimeoutError as exc:
                process.kill()
                stdout, stderr = await process.communicate()
                raise ProviderError(
                    "Codex skill generation timed out after 600 seconds. "
                    f"{_codex_diagnostic(stdout, stderr)}", provider="codex"
                ) from exc
            if process.returncode:
                raise ProviderError(
                    f"Codex skill generation failed (exit code {process.returncode}): "
                    f"{_codex_diagnostic(stdout, stderr)}", provider="codex", status_code=process.returncode
                )
            if not output_path.is_file():
                raise ProviderError(
                    "Codex completed without creating its structured response file. "
                    f"{_codex_diagnostic(stdout, stderr)}", provider="codex"
                )
            raw_output = output_path.read_text(encoding="utf-8", errors="replace")
            try:
                draft = _json_from_codex_output(raw_output)
            except (json.JSONDecodeError, ValueError) as exc:
                diagnostic = _codex_diagnostic(stdout, stderr)
                if diagnostic == "Codex returned no diagnostic output." and raw_output.strip():
                    diagnostic = f"Raw Codex response:\n{raw_output[-4000:]}"
                raise ProviderError(
                    "Codex returned an invalid skill definition. "
                    f"{diagnostic}", provider="codex", body=raw_output[-4000:]
                ) from exc
        draft["slug"] = skill_slug(draft.get("slug") or draft.get("name"))
        draft["version"] = str(draft.get("version") or "1.0.0")
        draft["compatibility"] = str(draft.get("compatibility") or "")
        draft["allowed_tools"] = str(draft.get("allowed_tools") or "")
        draft["license"] = str(draft.get("license") or "")
        draft["body"] = str(draft.get("body") or "").strip()
        try:
            draft["required_secrets"] = normalize_skill_secret_refs(draft.get("required_secrets", []))
        except ValueError:
            draft["required_secrets"] = []
        if not draft["body"]:
            draft["body"] = f"# {draft['name']}\n\n{draft['summary']}"
        return draft

    async def generate_toolset_definition(self, prompt: str) -> dict[str, Any]:
        command = self._codex_command()
        if not command:
            raise ProviderError("Codex CLI was not found")
        tool_properties = {
            "name": {"type": "string", "pattern": "^[a-z][a-z0-9-]{0,63}$", "description": "Stable hyphenated tool name"},
            "description": {"type": "string", "description": "One-sentence description of when this tool is useful"},
            "inputs": {"type": "string", "description": "Ordered positional argument contract, including optional arguments"},
            "outputs": {"type": "string", "description": "Exact stdout/result contract"},
            "filename": {"type": "string", "pattern": "^[a-z][a-z0-9-]{0,63}\\.py$", "description": "Unique Python executable filename"},
            "output_format": {"type": "string", "enum": ["text", "markdown", "json", "code"]},
            "result_template": {"type": "string", "description": "Chat template using stdout, stderr, exit_code, toolset, or tool markers in braces"},
            "env_vars": {
                "type": "array",
                "items": {"type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_]*$"},
                "description": "Environment-variable names required by this tool; names only, never values",
            },
            "source": {"type": "string", "description": "Complete cross-platform Python 3 source code"},
        }
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Human-facing toolset name"},
                "slug": {"type": "string", "pattern": "^[a-z][a-z0-9-]{0,63}$", "description": "Stable toolset folder ID"},
                "description": {"type": "string", "description": "One-sentence summary used in prompt discovery"},
                "details": {"type": "string", "description": "Concise shared constraints and usage guidance"},
                "tools": {
                    "type": "array", "minItems": 1, "maxItems": 8,
                    "items": {
                        "type": "object",
                        "properties": tool_properties,
                        "required": list(tool_properties),
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["name", "slug", "description", "details", "tools"],
            "additionalProperties": False,
        }
        with tempfile.TemporaryDirectory(prefix="agent-team-toolset-") as temp_dir:
            schema_path = Path(temp_dir) / "schema.json"
            output_path = Path(temp_dir) / "output.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            task = (
                "Design one focused local command-line toolset from this request: "
                f"{prompt}. Return only the requested structured fields. Create between one and eight narrowly useful "
                "tools. Every tool must be a complete cross-platform Python 3 program that uses only the standard "
                "library, reads positional arguments from sys.argv[1:], writes its documented result to stdout, writes "
                "diagnostics to stderr, and exits nonzero on failure. A tool may invoke an installed command-line "
                "program with subprocess.run using an argument list and shell=False. Never interpolate arguments into "
                "a shell command. Internet tools should use urllib and set a descriptive User-Agent. Do not access .env "
                "files or embed API keys, tokens, passwords, cookies, or other secret values. If credentials are needed, "
                "declare environment-variable names only in env_vars. Make filenames and tool names unique. Describe "
                "inputs in exact positional order. Use {stdout} as the default result_template unless a small Markdown "
                "wrapper materially improves the chat result. Keep the toolset discovery description to one sentence."
            )
            process = await asyncio.create_subprocess_exec(
                *codex_process_args([
                    command, "exec", "--ephemeral", "--sandbox", "read-only", "--skip-git-repo-check",
                    "-C", str(self.root), "--output-schema", str(schema_path),
                    "--output-last-message", str(output_path), "-",
                ]),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=codex_process_env(),
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(task.encode()), timeout=600)
            except asyncio.TimeoutError as exc:
                process.kill()
                stdout, stderr = await process.communicate()
                raise ProviderError(
                    "Codex toolset generation timed out after 600 seconds. "
                    f"{_codex_diagnostic(stdout, stderr)}", provider="codex"
                ) from exc
            if process.returncode:
                diagnostic = _codex_diagnostic(stdout, stderr)
                if _codex_auth_failure(diagnostic):
                    raise ProviderError(CODEX_AUTH_MESSAGE, provider="codex", status_code=401,
                                        code="codex_not_authenticated")
                raise ProviderError(
                    f"Codex toolset generation failed (exit code {process.returncode}): {diagnostic}",
                    provider="codex", status_code=process.returncode,
                )
            if not output_path.is_file():
                raise ProviderError(
                    "Codex completed without creating its structured toolset response file. "
                    f"{_codex_diagnostic(stdout, stderr)}", provider="codex"
                )
            raw_output = output_path.read_text(encoding="utf-8", errors="replace")
            try:
                draft = _json_from_codex_output(raw_output)
            except (json.JSONDecodeError, ValueError) as exc:
                raise ProviderError(
                    "Codex returned an invalid toolset definition. "
                    f"{_codex_diagnostic(stdout, stderr)}", provider="codex", body=raw_output[-4000:]
                ) from exc
        try:
            draft["slug"] = toolset_slug(draft.get("slug") or draft.get("name"))
            draft["name"] = str(draft.get("name") or "Generated toolset").strip()
            draft["description"] = str(draft.get("description") or "").strip()
            draft["details"] = str(draft.get("details") or "").strip()
            draft["tools"] = [self.toolsets._normalize_tool(item) for item in draft.get("tools", [])]
            if not draft["tools"]:
                raise ValueError("Codex did not define any tools")
            for tool in draft["tools"]:
                compile(tool["source"], tool["filename"], "exec")
        except ValueError as exc:
            raise ProviderError(f"Codex generated an invalid toolset: {exc}", provider="codex") from exc
        except SyntaxError as exc:
            raise ProviderError(
                f"Codex generated invalid Python for {exc.filename}: {exc.msg} (line {exc.lineno})",
                provider="codex",
            ) from exc
        return draft

    def _codex_tui_command(self, executable: str, command_text: str, config: dict[str, str],
                           session_id: str = "", working_root: Path | None = None) -> str:
        terminal = create_terminal(rows=40, columns=120)
        env = codex_process_env()
        env.update({"TERM": "xterm-256color", "COLUMNS": "120", "LINES": "40"})
        working_root = working_root or self.root
        if session_id:
            args = [executable, "resume", "--include-non-interactive", "--no-alt-screen", "-C", str(working_root)]
        else:
            args = [executable, "--no-alt-screen", "-C", str(working_root)]
        if config.get("model"):
            args.extend(["--model", config["model"]])
        if config.get("reasoning_effort"):
            args.extend(["-c", f'model_reasoning_effort="{config["reasoning_effort"]}"'])
        if session_id:
            args.append(session_id)
        args = codex_process_args(args)
        screen = pyte.Screen(120, 40)
        stream = pyte.Stream(screen)

        def drain(duration: float) -> None:
            deadline = time.monotonic() + duration
            while time.monotonic() < deadline and terminal.is_alive():
                chunk = terminal.read_available(0.15)
                try:
                    if chunk:
                        stream.feed(chunk)
                except (OSError, ValueError):
                    break

        try:
            terminal.spawn(args, env=env, cwd=working_root)
            # Let the real TUI finish loading its model and MCP status before entering the command.
            ready = False
            for _ in range(30):
                drain(0.5)
                visible = "\n".join(screen.display)
                if ("OpenAI Codex" in visible and "directory:" in visible and "›" in visible
                        and "Starting MCP" not in visible and "Booting MCP" not in visible
                        and "Select Model and Effort" not in visible):
                    ready = True
                    break
            if not ready:
                raise ProviderError("Codex interactive terminal did not become ready")
            # The TUI processes key events, not pasted stdin; pace characters so its slash
            # completion state consumes the full command before Enter selects it.
            for character in command_text:
                terminal.write(character.encode())
                time.sleep(0.025)
                drain(0.01)
            time.sleep(0.1)
            terminal.write(b"\r")
            drain(2.5)
            output = "\n".join(line.rstrip() for line in screen.display).strip()
            if not output:
                raise ProviderError("Codex returned an empty terminal screen")
            return output
        finally:
            terminal.terminate()
            terminal.close()

    async def native_command(self, role: str, command_text: str, project_id: int = 1) -> dict[str, str]:
        command_name = command_text.partition(" ")[0]
        config = self.configs.get(role, project_id)
        if config["provider"] != "codex":
            raise ValueError(f"{command_name} requires an agent configured for the Codex provider")
        executable = self._codex_command()
        if not executable:
            raise ProviderError("Codex CLI was not found")
        session = self.configs.codex_session(role, project_id)
        working_root = self._project_root(project_id)
        lock = self._codex_chat_locks.setdefault((project_id, role), asyncio.Lock())
        async with lock:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    self._codex_tui_command, executable, command_text, config,
                    session["session_id"] if session else "",
                    working_root,
                ), timeout=30)
        self.configs.add_message(
            role, "native", response, "codex", config.get("model", "") or "Codex default", project_id
        )
        return {"response": response, "command": command_name, "provider": "codex"}

    async def chat(self, role: str, message: str, model: str = "", reasoning_effort: str = "",
                   project_id: int = 1, reply_to_id: int | None = None,
                   attachment_ids: list[str] | None = None,
                   record_user_message: bool = True,
                   user_message_id: int | None = None) -> dict[str, Any]:
        config = self.configs.get(role, project_id).copy()
        attachments = self.configs.pending_attachments(attachment_ids or [], role, project_id)
        existing_user_message = None
        if user_message_id:
            existing_user_message = self.configs.message(user_message_id, role, project_id)
            if not existing_user_message:
                raise ValueError("The queued user message no longer exists in this chat")
            record_user_message = False
        if model:
            config["model"] = model
        if reasoning_effort:
            config["reasoning_effort"] = reasoning_effort
        delivery_run_id = uuid.uuid4().hex
        inbound_messages: list[dict[str, Any]] = []
        try:
            inbound_messages = self.configs.claim_pending_agent_messages(
                role, project_id, delivery_run_id,
            )
            inbound_ids = {int(item["id"]) for item in inbound_messages}
            # Human prompts are written before their durable run starts so a
            # queued request can survive a restart.  Do not let the current
            # prompt (or a later queued prompt) get picked up from the
            # transcript while an earlier provider call is still running.
            excluded_history_ids = inbound_ids | self.configs.active_chat_user_message_ids(
                role, project_id,
            )
            inbound_prompt = self._inter_agent_prompt(inbound_messages)
            provider_message = f"{inbound_prompt}\n\n{message}" if inbound_prompt else message
            if config["provider"] == "codex":
                result = await self._codex_chat(
                    role, provider_message, config, project_id, reply_to_id, attachments,
                )
            elif config["provider"] == "google":
                result = await self._google_chat(
                    role, provider_message, config, project_id, reply_to_id, attachments,
                    excluded_history_ids,
                )
            else:
                result = await self._agents_chat(
                    role, provider_message, config, project_id, reply_to_id, attachments,
                    excluded_history_ids,
                )
        except Exception as exc:
            self.configs.release_agent_messages(
                [int(item["id"]) for item in inbound_messages], delivery_run_id,
            )
            details = exc.details() if isinstance(exc, ProviderError) else {
                "provider": config["provider"], "type": type(exc).__name__, "message": str(exc),
                "status_code": getattr(exc, "status_code", None), "code": "", "request_id": "", "body": None,
            }
            visible = [f"{details['provider'].title()} call failed", f"Type: {details['type']}"]
            if details.get("status_code"):
                visible.append(f"HTTP status: {details['status_code']}")
            if details.get("code"):
                visible.append(f"Code: {details['code']}")
            if details.get("request_id"):
                visible.append(f"Request ID: {details['request_id']}")
            visible.append(f"Message: {details['message']}")
            if details.get("body") is not None:
                visible.append("Provider response:\n" + json.dumps(details["body"], indent=2, default=str))
            error_text = "\n".join(visible)
            user_message = existing_user_message
            if user_message is None and record_user_message:
                user_message = self.configs.add_message(
                    role, "user", message, config["provider"], config["model"], project_id, reply_to_id
                )
            if user_message is not None:
                self.configs.attach_to_message(attachment_ids or [], user_message["id"])
            error_message = self.configs.add_message(
                role, "error", error_text, config["provider"], config["model"], project_id
            )
            if user_message is not None:
                user_message["attachments"] = [
                    {key: item[key] for key in ("id", "name", "mime_type", "size")} for item in attachments
                ]
            error_message["attachments"] = []
            # print(visible)
            return {
                "ok": False, "response": error_text, "error": details,
                "answered_by": self.definitions.get(role, project_id)["name"], "user_message": user_message,
                "assistant_message": error_message, "provider": config["provider"],
                "model": config["model"] or "Codex default",
                "reasoning_effort": config.get("reasoning_effort", ""),
                "inter_agent_message_ids": [int(item["id"]) for item in inbound_messages],
            }
        else:
            try:
                self.configs.mark_agent_messages_delivered(
                    [int(item["id"]) for item in inbound_messages], delivery_run_id,
                )
            except Exception:
                self.configs.release_agent_messages(
                    [int(item["id"]) for item in inbound_messages], delivery_run_id,
                )
                raise
            resolved_response, tool_calls = await resolve_tool_calls(
                result["response"], self.toolsets, self._project_root(project_id), project_id, role,
            )
            result["response"] = resolved_response
            if tool_calls:
                result["tool_calls"] = tool_calls
            print(result, flush=True)
            user_message = existing_user_message
            if user_message is None and record_user_message:
                user_message = self.configs.add_message(
                    role, "user", message, config["provider"], config["model"], project_id, reply_to_id
                )
            if user_message is not None:
                self.configs.attach_to_message(attachment_ids or [], user_message["id"])
            assistant_message = self.configs.add_message(
                role, "assistant", result["response"], config["provider"], config["model"], project_id
            )
            if user_message is not None:
                user_message["attachments"] = [
                    {key: item[key] for key in ("id", "name", "mime_type", "size")} for item in attachments
                ]
            assistant_message["attachments"] = []
            return {**result, "ok": True, "user_message": user_message, "assistant_message": assistant_message,
                    "provider": config["provider"], "model": config["model"] or "Codex default",
                    "reasoning_effort": config.get("reasoning_effort", ""),
                    "inter_agent_message_ids": [int(item["id"]) for item in inbound_messages]}
