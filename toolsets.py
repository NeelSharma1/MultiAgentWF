from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
from pathlib import Path, PurePosixPath
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
TOOL_TIMEOUT_SECONDS = 60
TOOL_OUTPUT_LIMIT = 200_000
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
