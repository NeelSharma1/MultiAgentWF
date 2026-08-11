from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import shutil
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


TOOLSET_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
TOOL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TOOL_CALL_RE = re.compile(
    r"(?m)^[ \t]*TOOLCALL[ \t]*-[ \t]*"
    r"(?P<toolset>[a-z0-9]+(?:-[a-z0-9]+)*)/"
    r"(?P<tool>[a-z0-9]+(?:-[a-z0-9]+)*)[ \t]*-[ \t]*"
    r"(?P<arguments>\[[^\r\n]*\])[ \t]*\.[ \t]*$"
)
LOCAL_COMMAND_RE = re.compile(
    r"(?m)^[ \t]*COMMAND[ \t]*-[ \t]*(?P<command>\S[^\r\n]*?)[ \t]*$",
    flags=re.IGNORECASE,
)
PROJECT_VENV_LAUNCHER_RE = re.compile(
    r"(?<![A-Za-z0-9_.\\/-])"
    r"(?P<prefix>\.?[\\/])?"
    r"(?P<environment>_venv|venv|\.venv|env)"
    r"(?P<separator>[\\/])"
    r"(?P<bin>bin|Scripts)"
    r"(?P=separator)"
    r"(?P<executable>[A-Za-z0-9_.+-]+)(?:\.exe)?",
    flags=re.IGNORECASE,
)
READ_LINE_SELECTOR_RE = re.compile(
    r"^(?:lines?\s*[:=]?\s*)?\d+(?:-\d+)?(?:\s*,\s*\d+(?:-\d+)?)*$",
    flags=re.IGNORECASE,
)
FILE_ACTION_MARKER_RE = re.compile(
    r"(?P<create_block>^[ \t]*CREATE[ \t]*-[ \t]*"
    r"(?P<block_path>(?![^\r\n]*[ \t]+-[ \t]+)[^\r\n]+?)[ \t]*\r?\n"
    r"(?P<block_content>.*?)^[ \t]*END[ \t]+CREATE[ \t]*$)"
    r"|(?P<read>^[ \t]*READ[ \t]*-[ \t]*(?P<read_payload>\S[^\r\n]*?)[ \t]*$)"
    r"|(?P<create_inline>^[ \t]*CREATE[ \t]*-[ \t]*(?P<create_payload>\S[^\r\n]*?)[ \t]*$)",
    flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
SHELL_FAILURE_DIAGNOSTIC_RE = re.compile(
    r"(?:"
    r"(?:^|\r?\n)\s*(?:/bin/)?(?:sh|bash|zsh)(?:\.exe)?\s*:.*?"
    r"(?:no such file or directory|command not found|not found|cannot execute|permission denied)"
    r"|is not recognized as an internal or external command"
    r"|the system cannot find the (?:file|path) specified"
    r")",
    flags=re.IGNORECASE,
)
TOOL_TIMEOUT_SECONDS = 60
LOCAL_COMMAND_TIMEOUT_SECONDS = 600
TOOL_OUTPUT_LIMIT = 200_000
PROJECT_ENVIRONMENT_NAMES = ("venv", ".venv", "env", "_venv")
FILE_READ_OUTPUT_LIMIT = 200_000
CREATE_CONTENT_LIMIT = 1_000_000
SAFE_ENV_NAMES = {
    "PATH", "HOME", "USER", "USERNAME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "LOGNAME", "SHELL", "TERM", "TMPDIR", "TMP", "TEMP", "LANG", "LANGUAGE",
    "LC_ALL", "VIRTUAL_ENV", "PYTHONPATH", "SYSTEMROOT", "SystemRoot", "COMSPEC",
    "PATHEXT", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES",
    "PROGRAMFILES(X86)", "WINDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
}


def toolset_slug(value: str) -> str:
    raw = re.sub(r"[_\s]+", "-", str(value or "").strip().lower())
    raw = re.sub(r"[^a-z0-9-]+", "-", raw).strip("-")
    raw = re.sub(r"-+", "-", raw)[:64].strip("-")
    if not raw or not TOOLSET_SLUG_RE.fullmatch(raw):
        raise ValueError("Toolset ID must contain lowercase letters, numbers, and single hyphens")
    return raw


def tool_name(value: str) -> str:
    raw = toolset_slug(value)
    if not TOOL_NAME_RE.fullmatch(raw):
        raise ValueError("Tool name must contain lowercase letters, numbers, and single hyphens")
    return raw


def safe_relative_path(value: str) -> PurePosixPath:
    raw = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(raw)
    if (not raw or path.is_absolute() or ":" in path.parts[0]
            or any(part in {"", ".", ".."} for part in path.parts)):
        raise ValueError("Tool filenames must be relative paths without '.' or '..' segments")
    if len(path.parts) > 5:
        raise ValueError("Tool filenames may be nested at most five directories deep")
    return path


def _markdown_cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def _summary_markdown(manifest: dict[str, Any]) -> str:
    lines = [
        f"# {manifest['name']}",
        "",
        manifest["description"],
        "",
        "<!-- TOOLSET-SUMMARY-START -->",
        "## Tool summary",
        "",
        "Read only this summary section when deciding whether to call a tool.",
        "",
        "| Tool | Description | Inputs | Outputs | Execute |",
        "| --- | --- | --- | --- | --- |",
    ]
    for tool in manifest["tools"]:
        lines.append(
            f"| `{_markdown_cell(tool['name'])}` | {_markdown_cell(tool['description'])} | "
            f"{_markdown_cell(tool['inputs'])} | {_markdown_cell(tool['outputs'])} | "
            f"`{_markdown_cell(tool['filename'])}` |"
        )
    lines.extend([
        "",
        "Call syntax: `TOOLCALL - <toolset>/<tool name> - [arguments].`",
        "Arguments must be a valid JSON list in the same positional order described above.",
        "<!-- TOOLSET-SUMMARY-END -->",
    ])
    details = str(manifest.get("details") or "").strip()
    if details:
        lines.extend(["", "## Additional guidance", "", details])
    return "\n".join(lines).rstrip() + "\n"


class ToolsetStore:
    """Filesystem-backed toolsets stored in each project's .agents/tools directory."""

    @staticmethod
    def root(project_root: Path) -> Path:
        return Path(project_root).expanduser().resolve() / ".agents" / "tools"

    @staticmethod
    def _assignment_path(project_root: Path) -> Path:
        return ToolsetStore.root(project_root) / ".assignments.json"

    def _assignments(self, project_root: Path) -> dict[str, dict[str, list[str]]]:
        path = self._assignment_path(project_root)
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_assignments(self, project_root: Path, value: dict[str, Any]) -> None:
        root = self.root(project_root)
        root.mkdir(parents=True, exist_ok=True)
        self._assignment_path(project_root).write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def _directory(self, project_root: Path, slug: str) -> Path:
        normalized = toolset_slug(slug)
        root = self.root(project_root)
        directory = (root / normalized).resolve()
        if directory.parent != root.resolve():
            raise ValueError("Toolset path escapes .agents/tools")
        return directory

    @staticmethod
    def _normalize_tool(raw: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError("Each tool definition must be an object")
        name = tool_name(raw.get("name") or "")
        description = str(raw.get("description") or "").strip()
        if not description:
            raise ValueError(f"Tool '{name}' needs a description")
        filename = safe_relative_path(raw.get("filename") or "")
        env_vars = raw.get("env_vars") or []
        if isinstance(env_vars, str):
            env_vars = [part.strip() for part in env_vars.split(",") if part.strip()]
        if not isinstance(env_vars, list) or any(not ENV_NAME_RE.fullmatch(str(item)) for item in env_vars):
            raise ValueError(f"Tool '{name}' has an invalid environment-variable name")
        output_format = str(raw.get("output_format") or "text").strip().lower()
        if output_format not in {"text", "markdown", "json", "code"}:
            raise ValueError(f"Unsupported output format for tool '{name}'")
        return {
            "name": name,
            "description": description[:2000],
            "inputs": str(raw.get("inputs") or "No arguments.").strip()[:4000],
            "outputs": str(raw.get("outputs") or "Text output.").strip()[:4000],
            "filename": filename.as_posix(),
            "output_format": output_format,
            "result_template": str(raw.get("result_template") or "{stdout}")[:20_000],
            "env_vars": list(dict.fromkeys(str(item) for item in env_vars)),
            "source": str(raw.get("source") or ""),
        }

    def save(self, project_root: Path, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        description = str(payload.get("description") or "").strip()
        if not name or not description:
            raise ValueError("Toolset name and description are required")
        slug = toolset_slug(payload.get("slug") or name)
        tools = [self._normalize_tool(item) for item in (payload.get("tools") or [])]
        if not tools:
            raise ValueError("A toolset must define at least one tool")
        names = [item["name"] for item in tools]
        filenames = [item["filename"].lower() for item in tools]
        if len(names) != len(set(names)):
            raise ValueError("Tool names must be unique within a toolset")
        if len(filenames) != len(set(filenames)):
            raise ValueError("Each tool must use a different executable filename")
        directory = self._directory(project_root, slug)
        directory.mkdir(parents=True, exist_ok=True)
        manifest = {
            "format": "multiagent-toolset/v1",
            "name": name[:120],
            "slug": slug,
            "description": description[:2000],
            "details": str(payload.get("details") or "")[:50_000],
            "tools": [{key: value for key, value in item.items() if key != "source"} for item in tools],
        }
        for tool in tools:
            relative = safe_relative_path(tool["filename"])
            target = directory.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not tool["source"] and not target.exists():
                raise ValueError(f"Tool '{tool['name']}' needs executable source code")
            if tool["source"]:
                target.write_text(tool["source"], encoding="utf-8")
        (directory / "toolset.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        (directory / "TOOLSET.md").write_text(_summary_markdown(manifest), encoding="utf-8")
        return self.get(project_root, slug) or manifest

    def get(self, project_root: Path, slug: str, *, include_source: bool = True) -> dict[str, Any] | None:
        directory = self._directory(project_root, slug)
        manifest_path = directory / "toolset.json"
        if not manifest_path.is_file():
            return None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Toolset '{slug}' has an invalid toolset.json: {exc}") from exc
        tools = []
        for item in manifest.get("tools") or []:
            normalized = self._normalize_tool(item)
            source_path = directory.joinpath(*safe_relative_path(normalized["filename"]).parts)
            normalized["source"] = (
                source_path.read_text(encoding="utf-8", errors="replace")
                if include_source and source_path.is_file() else ""
            )
            tools.append(normalized)
        return {
            "format": "multiagent-toolset/v1",
            "name": str(manifest.get("name") or slug),
            "slug": toolset_slug(manifest.get("slug") or slug),
            "description": str(manifest.get("description") or ""),
            "details": str(manifest.get("details") or ""),
            "filename": f".agents/tools/{slug}/TOOLSET.md",
            "tools": tools,
        }

    def list(self, project_root: Path, project_id: int, role: str | None = None) -> list[dict[str, Any]]:
        root = self.root(project_root)
        if not root.is_dir():
            return []
        project_assignments = self._assignments(project_root).get(str(project_id), {})
        result = []
        for manifest_path in sorted(root.glob("*/toolset.json")):
            try:
                item = self.get(project_root, manifest_path.parent.name, include_source=False)
            except ValueError:
                continue
            if not item:
                continue
            item["assigned_roles"] = list(project_assignments.get(item["slug"], []))
            if role and role not in item["assigned_roles"]:
                continue
            result.append(item)
        return result

    def assign(self, project_root: Path, project_id: int, slug: str, roles: list[str]) -> dict[str, Any]:
        if not self.get(project_root, slug, include_source=False):
            raise KeyError(f"Toolset '{slug}' was not found")
        assignments = self._assignments(project_root)
        project = assignments.setdefault(str(project_id), {})
        project[toolset_slug(slug)] = list(dict.fromkeys(str(role) for role in roles if str(role)))
        self._save_assignments(project_root, assignments)
        return {"slug": toolset_slug(slug), "project_id": project_id, "roles": project[toolset_slug(slug)]}

    def delete(self, project_root: Path, slug: str) -> None:
        directory = self._directory(project_root, slug)
        if not directory.is_dir():
            raise KeyError(f"Toolset '{slug}' was not found")
        shutil.rmtree(directory)
        assignments = self._assignments(project_root)
        changed = False
        for project in assignments.values():
            if isinstance(project, dict) and project.pop(toolset_slug(slug), None) is not None:
                changed = True
        if changed:
            self._save_assignments(project_root, assignments)

    def summary(self, project_root: Path, project_id: int, role: str, slug: str) -> dict[str, Any]:
        assigned = {item["slug"]: item for item in self.list(project_root, project_id, role)}
        if slug not in assigned:
            raise PermissionError(f"Toolset '{slug}' is not assigned to {role}")
        path = self._directory(project_root, slug) / "TOOLSET.md"
        text = path.read_text(encoding="utf-8", errors="replace")
        start = text.find("<!-- TOOLSET-SUMMARY-START -->")
        end = text.find("<!-- TOOLSET-SUMMARY-END -->")
        summary = text[start:end + len("<!-- TOOLSET-SUMMARY-END -->")] if start >= 0 and end >= start else text
        return {"slug": slug, "filename": assigned[slug]["filename"], "summary": summary}

    async def execute(self, project_root: Path, project_id: int, role: str, slug: str,
                      name: str, arguments: list[Any]) -> dict[str, Any]:
        assigned = {item["slug"]: item for item in self.list(project_root, project_id, role)}
        toolset = assigned.get(toolset_slug(slug))
        if not toolset:
            raise PermissionError(f"Toolset '{slug}' is not assigned to {role}")
        tool = next((item for item in toolset["tools"] if item["name"] == tool_name(name)), None)
        if not tool:
            raise KeyError(f"Tool '{name}' was not found in toolset '{slug}'")
        if not isinstance(arguments, list):
            raise ValueError("Tool arguments must be a JSON list")
        directory = self._directory(project_root, slug)
        executable = directory.joinpath(*safe_relative_path(tool["filename"]).parts)
        if executable.is_symlink() or not executable.is_file():
            raise FileNotFoundError(f"Tool executable was not found: {tool['filename']}")
        resolved = executable.resolve()
        if not resolved.is_relative_to(directory.resolve()):
            raise ValueError("Tool executable escapes its toolset directory")
        command = self._command_for(resolved)
        command.extend(str(value) for value in arguments)
        env = {key: value for key, value in os.environ.items() if key in SAFE_ENV_NAMES}
        declared_values: list[str] = []
        for name_ref in tool.get("env_vars") or []:
            if name_ref in os.environ:
                env[name_ref] = os.environ[name_ref]
                if os.environ[name_ref]:
                    declared_values.append(os.environ[name_ref])
        env.update({
            "TOOLSET_NAME": slug,
            "TOOL_NAME": name,
            "TOOL_ARGS_JSON": json.dumps(arguments, ensure_ascii=False),
        })
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(Path(project_root).resolve()),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=TOOL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            raise TimeoutError(f"Tool exceeded the {TOOL_TIMEOUT_SECONDS}-second timeout")
        stdout_text = stdout[:TOOL_OUTPUT_LIMIT].decode(errors="replace").rstrip()
        stderr_text = stderr[:TOOL_OUTPUT_LIMIT].decode(errors="replace").rstrip()
        for value in declared_values:
            stdout_text = stdout_text.replace(value, "[REDACTED]")
            stderr_text = stderr_text.replace(value, "[REDACTED]")
        return {
            "ok": process.returncode == 0,
            "toolset": slug,
            "tool": name,
            "arguments": arguments,
            "exit_code": process.returncode,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "output_format": tool.get("output_format") or "text",
            "result_template": tool.get("result_template") or "{stdout}",
        }

    @staticmethod
    def _command_for(path: Path) -> list[str]:
        suffix = path.suffix.lower()
        if suffix == ".py":
            return [sys.executable, str(path)]
        if suffix in {".js", ".mjs", ".cjs"}:
            node = shutil.which("node")
            if not node:
                raise FileNotFoundError("Node.js is required to run this tool")
            return [node, str(path)]
        if suffix == ".ps1":
            powershell = shutil.which("pwsh") or shutil.which("powershell")
            if not powershell:
                raise FileNotFoundError("PowerShell is required to run this tool")
            return [powershell, "-NoProfile", "-NonInteractive", "-File", str(path)]
        if suffix in {".sh", ".bash"}:
            shell = shutil.which("bash") or shutil.which("sh")
            if not shell:
                raise FileNotFoundError("A POSIX shell is required to run this tool")
            return [shell, str(path)]
        if suffix in {".exe", ".com"} or not suffix:
            return [str(path)]
        raise ValueError(f"Unsupported tool executable type: {suffix or '(none)'}")


async def _read_limited(stream: asyncio.StreamReader, limit: int) -> tuple[str, bool]:
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


def _kill_local_command(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if os.name == "nt":
        process.kill()
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()


def _project_venv_details(project_root: Path) -> tuple[Path, Path] | None:
    """Return the selected project environment root and its launcher folder."""
    executable_name = "python.exe" if os.name == "nt" else "python"
    bin_name = "Scripts" if os.name == "nt" else "bin"
    for environment_name in PROJECT_ENVIRONMENT_NAMES:
        environment_root = project_root / environment_name
        if (environment_root / bin_name / executable_name).is_file():
            return environment_root, environment_root / bin_name
    return None


def _project_venv_launcher_name(launcher_directory: Path, requested: str) -> str | None:
    """Choose the matching launcher, falling back to the environment Python."""
    suffix = ".exe" if os.name == "nt" else ""
    requested_name = f"{requested}{suffix}"
    candidates = [requested_name]
    if requested.lower().startswith("python"):
        candidates.append(f"python{suffix}")
    elif requested.lower().startswith("pytest"):
        candidates.append(f"pytest{suffix}")
    elif requested.lower().startswith("pip"):
        candidates.append(f"pip{suffix}")
    for candidate in dict.fromkeys(candidates):
        if (launcher_directory / candidate).is_file():
            return candidate
    return None


def normalize_project_venv_command(command: str, project_root: Path) -> str:
    """Route explicit relative venv launchers to the project's actual venv.

    Agents are given a portable default such as ``./venv/bin/python``. Projects
    commonly use ``.venv`` instead, so leaving that path untouched makes the
    shell fail before Python can start. Only relative launcher paths are
    rewritten; absolute paths and unrelated files are left alone.
    """
    details = _project_venv_details(project_root)
    if details is None:
        return command
    environment_root, launcher_directory = details
    target_environment = environment_root.name
    target_bin = launcher_directory.name

    def replace(match: re.Match[str]) -> str:
        launcher_name = _project_venv_launcher_name(
            launcher_directory, match.group("executable"),
        )
        if not launcher_name:
            return match.group(0)
        prefix = match.group("prefix") or ""
        separator = match.group("separator")
        return f"{prefix}{target_environment}{separator}{target_bin}{separator}{launcher_name}"

    return PROJECT_VENV_LAUNCHER_RE.sub(replace, str(command))


def _unquote_file_marker_path(value: str) -> str:
    path = str(value or "").strip()
    if len(path) >= 2 and path[0] == path[-1] and path[0] in {"'", '"'}:
        return path[1:-1]
    return path


def _workspace_file_path(project_root: Path, raw_path: str) -> tuple[Path, str]:
    """Resolve a marker path while keeping it inside the project root."""
    value = _unquote_file_marker_path(raw_path).replace("\\", "/")
    portable = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        not value or portable.is_absolute() or windows.is_absolute() or windows.drive
        or ".." in portable.parts
    ):
        raise ValueError("File actions require a relative path inside the project workspace")
    parts = tuple(part for part in portable.parts if part not in {"", "."})
    if not parts:
        raise ValueError("File actions require a file path")
    root = Path(project_root).expanduser().resolve()
    candidate = (root.joinpath(*parts)).resolve()
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("File actions cannot access paths outside the project workspace") from exc
    return candidate, relative.as_posix()


def _parse_line_ranges(selector: str) -> list[tuple[int, int]]:
    raw = re.sub(r"^lines?\s*[:=]?\s*", "", str(selector or "").strip(), flags=re.IGNORECASE)
    if not READ_LINE_SELECTOR_RE.fullmatch(raw):
        raise ValueError("Line selections must use values such as '1-10' or '1,4,8-12'")
    ranges: list[tuple[int, int]] = []
    for item in raw.split(","):
        values = item.strip().split("-", 1)
        start = int(values[0])
        end = int(values[-1])
        if start < 1 or end < start or end - start > 10_000:
            raise ValueError("Line selections must be positive, ordered, and at most 10,001 lines wide")
        ranges.append((start, end))
    return ranges


def _parse_read_marker(payload: str) -> tuple[str, list[tuple[int, int]] | None]:
    raw = str(payload or "").strip()
    selector_match = re.match(
        r"^(?P<path>.+?)\s+-\s+(?P<selector>(?:lines?\s*[:=]?\s*)?\d+(?:-\d+)?(?:\s*,\s*\d+(?:-\d+)?)*?)$",
        raw,
        flags=re.IGNORECASE,
    )
    if not selector_match:
        return _unquote_file_marker_path(raw), None
    return _unquote_file_marker_path(selector_match.group("path")), _parse_line_ranges(
        selector_match.group("selector"),
    )


def _parse_create_inline_marker(payload: str) -> tuple[str, str]:
    path, separator, content = str(payload or "").strip().partition(" - ")
    if not separator:
        raise ValueError("CREATE requires '<path> - <content>' or a CREATE block")
    return _unquote_file_marker_path(path), content


def _execute_file_action(action: dict[str, Any], project_root: Path) -> dict[str, Any]:
    path, relative = _workspace_file_path(project_root, str(action.get("path") or ""))
    action_name = str(action.get("action") or "").upper()
    if action_name == "READ":
        if not path.is_file():
            raise FileNotFoundError(f"File does not exist: {relative}")
        content = path.read_bytes().decode("utf-8", errors="replace")
        ranges = action.get("line_ranges")
        if ranges:
            lines = content.splitlines()
            selected: list[str] = []
            for start, end in ranges:
                for line_number in range(start, min(end, len(lines)) + 1):
                    selected.append(f"{line_number}: {lines[line_number - 1]}")
            output = "\n".join(selected)
        else:
            output = content
        truncated = len(output) > FILE_READ_OUTPUT_LIMIT
        if truncated:
            output = output[:FILE_READ_OUTPUT_LIMIT].rstrip() + (
                f"\n[READ output truncated after {FILE_READ_OUTPUT_LIMIT:,} characters]"
            )
        return {
            "ok": True, "action": "READ", "path": relative, "line_ranges": ranges,
            "stdout": output, "stderr": "", "exit_code": 0, "cwd": str(Path(project_root).resolve()),
        }
    if action_name == "CREATE":
        content = str(action.get("content") or "")
        if len(content) > CREATE_CONTENT_LIMIT:
            raise ValueError(f"CREATE content cannot exceed {CREATE_CONTENT_LIMIT:,} characters")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {
            "ok": True, "action": "CREATE", "path": relative, "bytes": len(content.encode("utf-8")),
            "stdout": "", "stderr": "", "exit_code": 0, "cwd": str(Path(project_root).resolve()),
        }
    raise ValueError("Unknown file action")


def format_file_action_result(result: dict[str, Any]) -> str:
    """Render a READ/CREATE result for the agent and the chat transcript."""
    action = str(result.get("action") or "FILE").upper()
    path = str(result.get("path") or "")
    if not result.get("ok"):
        detail = str(result.get("error") or result.get("stderr") or result.get("stdout") or "").strip()
        return f"> {action} `{path}` failed.\n> {detail or 'The file action produced no diagnostic output.'}"
    if action == "READ":
        ranges = result.get("line_ranges")
        scope = "" if not ranges else f" (requested lines {', '.join(f'{start}-{end}' for start, end in ranges)})"
        content = str(result.get("stdout") or "")
        return f"> READ `{path}`{scope}:\n```text\n{content}\n```"
    return f"> CREATE `{path}` completed ({result.get('bytes', 0)} bytes written)."


async def resolve_file_markers(
    text: str, project_root: Path, allow_read: bool = True, allow_create: bool = True,
) -> tuple[str, list[dict[str, Any]], list[str], dict[str, str]]:
    """Resolve permission-gated READ and CREATE markers from an agent response."""
    matches = list(FILE_ACTION_MARKER_RE.finditer(str(text or "")))
    if not matches:
        return text, [], [], {}
    root = Path(project_root).expanduser().resolve()
    parts: list[str] = []
    calls: list[dict[str, Any]] = []
    pending: list[str] = []
    replacements: dict[str, str] = {}
    cursor = 0
    for match in matches:
        parts.append(text[cursor:match.start()])
        marker = match.group(0).strip()
        action_name = "READ" if match.group("read") is not None else "CREATE"
        try:
            if action_name == "READ":
                path, line_ranges = _parse_read_marker(match.group("read_payload"))
                action = {"action": action_name, "path": path, "line_ranges": line_ranges}
            elif match.group("create_block") is not None:
                action = {
                    "action": action_name,
                    "path": _unquote_file_marker_path(match.group("block_path")),
                    "content": match.group("block_content"),
                }
            else:
                path, content = _parse_create_inline_marker(match.group("create_payload"))
                action = {"action": action_name, "path": path, "content": content}
        except Exception as exc:
            action = {"action": action_name, "path": "", "error": str(exc)}
        allowed = allow_read if action_name == "READ" else allow_create
        if not allowed:
            pending.append(marker)
            cursor = match.end()
            continue
        try:
            result = _execute_file_action(action, root) if "error" not in action else {
                "ok": False, "action": action_name, "path": action.get("path", ""),
                "error": action["error"], "exit_code": None, "cwd": str(root),
            }
        except Exception as exc:
            result = {
                "ok": False, "action": action_name, "path": action.get("path", ""),
                "error": str(exc), "exit_code": None, "cwd": str(root),
            }
        calls.append(result)
        placeholder = f"__MULTIAGENT_FILE_RESULT_{len(replacements)}__"
        replacements[placeholder] = format_file_action_result(result)
        parts.append(placeholder)
        cursor = match.end()
    parts.append(text[cursor:])
    return "".join(parts).strip(), calls, pending, replacements


def restore_file_action_results(text: str, replacements: dict[str, str]) -> str:
    """Restore shielded file results after other marker resolvers finish."""
    for placeholder, value in replacements.items():
        text = text.replace(placeholder, value)
    return text


def _local_command_environment(project_root: Path) -> dict[str, str]:
    """Build a small, non-secret environment for a host-side command."""
    environment = {
        key: value for key, value in os.environ.items() if key in SAFE_ENV_NAMES
    }
    environment["PYTHONUNBUFFERED"] = "1"
    environment["PWD"] = str(project_root)
    executable_name = "python.exe" if os.name == "nt" else "python"
    details = _project_venv_details(project_root)
    if details is not None:
        environment_root, launcher_directory = details
        interpreter = launcher_directory / executable_name
        environment["VIRTUAL_ENV"] = str(environment_root)
        environment["PYTHON"] = str(interpreter)
        environment["PATH"] = os.pathsep.join([
            str(interpreter.parent), environment.get("PATH", "")
        ])
    return environment


async def run_local_command(command: str, project_root: Path) -> dict[str, Any]:
    """Run one command on the application host from the configured repository."""
    requested_command = str(command or "").strip()
    command = requested_command
    root = Path(project_root).expanduser().resolve()
    if not command:
        raise ValueError("Local command cannot be empty")
    if "\x00" in command:
        raise ValueError("Local command cannot contain NUL characters")
    if not root.is_dir():
        raise FileNotFoundError(f"Project folder does not exist: {root}")
    command = normalize_project_venv_command(command, root)
    process = await asyncio.create_subprocess_shell(
        command,
        cwd=str(root),
        env=_local_command_environment(root),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=os.name != "nt",
    )
    assert process.stdout is not None and process.stderr is not None
    stdout_task = asyncio.create_task(_read_limited(process.stdout, TOOL_OUTPUT_LIMIT))
    stderr_task = asyncio.create_task(_read_limited(process.stderr, TOOL_OUTPUT_LIMIT))
    timed_out = False
    try:
        await asyncio.wait_for(process.wait(), timeout=LOCAL_COMMAND_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        timed_out = True
        _kill_local_command(process)
        await process.wait()
    except asyncio.CancelledError:
        _kill_local_command(process)
        await process.wait()
        raise
    stdout, stdout_truncated = await stdout_task
    stderr, stderr_truncated = await stderr_task
    if timed_out:
        stderr = f"{stderr}\nProcess timed out after {LOCAL_COMMAND_TIMEOUT_SECONDS} seconds.".strip()
    if stdout_truncated:
        stdout = f"{stdout}\n[stdout truncated after {TOOL_OUTPUT_LIMIT:,} bytes]".strip()
    if stderr_truncated:
        stderr = f"{stderr}\n[stderr truncated after {TOOL_OUTPUT_LIMIT:,} bytes]".strip()
    shell_diagnostic_failure = bool(SHELL_FAILURE_DIAGNOSTIC_RE.search(stderr or ""))
    result = {
        "ok": process.returncode == 0 and not timed_out and not shell_diagnostic_failure,
        "command": command,
        "cwd": str(root),
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "shell_diagnostic_failure": shell_diagnostic_failure,
        "stdout": stdout,
        "stderr": stderr,
    }
    if command != requested_command:
        result["requested_command"] = requested_command
    return result


def format_local_command_result(result: dict[str, Any]) -> str:
    """Render a host command result back into the agent's assistant message."""
    status = "completed" if result.get("ok") else "failed"
    lines = [
        f"> Local command `{result.get('command', '')}` {status} "
        f"(exit code {result.get('exit_code')}).",
        f"> Working directory: `{result.get('cwd', '')}`",
    ]
    requested_command = str(result.get("requested_command") or "").strip()
    if requested_command and requested_command != result.get("command"):
        lines.append(
            f"> Routed project environment command `{requested_command}` to `{result.get('command')}`."
        )
    if result.get("shell_diagnostic_failure"):
        lines.append(
            "> The shell reported a failed command segment even though the overall shell exit code was zero."
        )
    stdout = str(result.get("stdout") or "").strip()
    stderr = str(result.get("stderr") or "").strip()
    if stdout:
        lines.extend(["", "```text", stdout, "```"])
    if stderr:
        lines.extend(["", "Command diagnostics:", "```text", stderr, "```"])
    return "\n".join(lines)


async def resolve_command_markers(
    text: str, project_root: Path, allow_execution: bool = True,
) -> tuple[str, list[dict[str, Any]], list[str]]:
    """Resolve `COMMAND - …` markers or return them as pending approvals.

    The command itself is deliberately plain text so each provider can emit the
    same protocol. Execution happens in this application process, with the
    configured project as its working directory, never in a provider-owned
    development workspace.
    """
    matches = list(LOCAL_COMMAND_RE.finditer(str(text or "")))
    if not matches:
        return text, [], []
    parts: list[str] = []
    calls: list[dict[str, Any]] = []
    pending: list[str] = []
    cursor = 0
    for match in matches:
        parts.append(text[cursor:match.start()])
        command = match.group("command").strip()
        if not allow_execution:
            pending.append(command)
        else:
            try:
                result = await run_local_command(command, project_root)
                calls.append(result)
                parts.append(format_local_command_result(result))
            except Exception as exc:
                result = {
                    "ok": False, "command": command, "cwd": str(Path(project_root).resolve()),
                    "exit_code": None, "error": str(exc), "stdout": "", "stderr": "",
                }
                calls.append(result)
                parts.append(f"> Local command `{command}` was rejected: {exc}")
        cursor = match.end()
    parts.append(text[cursor:])
    return "".join(parts).strip(), calls, pending


def format_tool_result(result: dict[str, Any]) -> str:
    if not result.get("ok"):
        detail = result.get("stderr") or result.get("stdout") or "The tool exited without diagnostic output."
        return (
            f"> Tool `{result.get('toolset')}/{result.get('tool')}` failed "
            f"(exit code {result.get('exit_code')}).\n>\n> {str(detail).replace(chr(10), chr(10) + '> ')}"
        )
    values = {
        "stdout": str(result.get("stdout") or ""),
        "stderr": str(result.get("stderr") or ""),
        "exit_code": str(result.get("exit_code", 0)),
        "toolset": str(result.get("toolset") or ""),
        "tool": str(result.get("tool") or ""),
    }
    rendered = str(result.get("result_template") or "{stdout}")
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", value)
    output_format = result.get("output_format") or "text"
    if output_format == "json":
        try:
            rendered = json.dumps(json.loads(rendered), indent=2, ensure_ascii=False)
        except json.JSONDecodeError:
            pass
        return f"```json\n{rendered}\n```"
    if output_format == "code":
        return f"```\n{rendered}\n```"
    return rendered


async def resolve_tool_calls(text: str, store: ToolsetStore, project_root: Path,
                             project_id: int, role: str, allow_execution: bool = True) -> tuple[str, list[dict[str, Any]]]:
    matches = list(TOOL_CALL_RE.finditer(str(text or "")))
    if not matches:
        return text, []
    parts: list[str] = []
    calls: list[dict[str, Any]] = []
    cursor = 0
    for match in matches:
        parts.append(text[cursor:match.start()])
        slug, name = match.group("toolset"), match.group("tool")
        try:
            if not allow_execution:
                raise PermissionError("This agent is not permitted to execute local commands")
            arguments = json.loads(match.group("arguments"))
            if not isinstance(arguments, list):
                raise ValueError("arguments must be a JSON list")
            result = await store.execute(project_root, project_id, role, slug, name, arguments)
            replacement = format_tool_result(result)
        except Exception as exc:
            result = {
                "ok": False, "toolset": slug, "tool": name,
                "arguments": match.group("arguments"), "error": str(exc),
            }
            replacement = f"> Tool call `{slug}/{name}` was rejected: {exc}"
        calls.append(result)
        parts.append(replacement)
        cursor = match.end()
    parts.append(text[cursor:])
    return "".join(parts).strip(), calls
