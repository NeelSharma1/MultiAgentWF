from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from team import AgentTeam
from toolsets import (
    ToolsetStore, normalize_project_venv_command, resolve_command_markers, resolve_file_markers,
    resolve_tool_calls, restore_file_action_results,
)


def echo_toolset() -> dict:
    return {
        "name": "Local helpers",
        "slug": "local-helpers",
        "description": "Runs small, deterministic local helper commands.",
        "details": "Use these helpers instead of estimating their results.",
        "tools": [{
            "name": "echo-arguments",
            "description": "Returns each positional argument as JSON.",
            "inputs": "Any number of positional string values.",
            "outputs": "A JSON object containing the received argument list.",
            "filename": "echo.py",
            "output_format": "json",
            "result_template": "{stdout}",
            "env_vars": [],
            "source": "import json, sys\nprint(json.dumps({'arguments': sys.argv[1:]}))\n",
        }],
    }


def test_toolset_is_materialized_with_summary_and_scoped_assignments(tmp_path):
    store = ToolsetStore()
    saved = store.save(tmp_path, echo_toolset())
    store.assign(tmp_path, 1, saved["slug"], ["researcher"])

    summary_path = tmp_path / ".agents" / "tools" / "local-helpers" / "TOOLSET.md"
    summary = summary_path.read_text(encoding="utf-8")

    assert saved["filename"] == ".agents/tools/local-helpers/TOOLSET.md"
    assert "<!-- TOOLSET-SUMMARY-START -->" in summary
    assert "| `echo-arguments` |" in summary
    assert "Any number of positional string values." in summary
    assert store.list(tmp_path, 1, "researcher")[0]["slug"] == "local-helpers"
    assert store.list(tmp_path, 1, "programmer") == []


def test_team_prompt_uses_striated_toolset_discovery(tmp_path):
    team = AgentTeam(tmp_path)
    team._project_root = lambda _project_id: tmp_path
    team.toolsets.save(tmp_path, echo_toolset())
    team.toolsets.assign(tmp_path, 1, "local-helpers", ["researcher"])

    instructions = team._instructions("researcher", 1)

    assert "You have access to the following tools" in instructions
    assert ".agents/tools/local-helpers/TOOLSET.md" in instructions
    assert "TOOLCALL - <toolset>/<tool name> - [arguments]." in instructions
    assert "COMMAND - <text of command>" in instructions
    assert "Returns each positional argument as JSON." not in instructions


def test_team_prompt_uses_the_detected_project_python_path(tmp_path):
    launcher_directory = tmp_path / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    launcher_directory.mkdir(parents=True)
    (launcher_directory / ("python.exe" if os.name == "nt" else "python")).touch()
    team = AgentTeam(tmp_path)
    team._project_root = lambda _project_id: tmp_path

    instructions = team._action_guidance("researcher", 1)

    expected = (
        r".\.venv\Scripts\python.exe -m pytest"
        if os.name == "nt" else
        "./.venv/bin/python -m pytest"
    )
    assert expected in instructions
    assert "Do not assume `./venv` exists" in instructions


def test_command_marker_executes_on_the_host_from_the_project_root(tmp_path):
    text, calls, pending = asyncio.run(resolve_command_markers(
        "Before\nCOMMAND - echo local-command\nAfter", tmp_path,
    ))

    assert pending == []
    assert calls[0]["ok"] is True
    assert calls[0]["cwd"] == str(tmp_path.resolve())
    assert "local-command" in text
    assert "COMMAND -" not in text


def test_relative_venv_launcher_is_routed_to_the_detected_project_environment(tmp_path):
    launcher_directory = tmp_path / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    launcher_directory.mkdir(parents=True)
    (launcher_directory / ("python.exe" if os.name == "nt" else "python")).touch()
    command = (
        r".\venv\Scripts\python.exe -c \"print('ok')\""
        if os.name == "nt" else
        "./venv/bin/python -c \"print('ok')\""
    )

    normalized = normalize_project_venv_command(command, tmp_path)

    expected_prefix = (
        r".\.venv\Scripts\python.exe"
        if os.name == "nt" else
        "./.venv/bin/python"
    )
    assert normalized.startswith(expected_prefix)


def test_command_marker_is_returned_for_ui_approval_without_permission(tmp_path):
    text, calls, pending = asyncio.run(resolve_command_markers(
        "Before\nCOMMAND - echo local-command\nAfter", tmp_path, allow_execution=False,
    ))

    assert calls == []
    assert pending == ["echo local-command"]
    assert "COMMAND -" not in text
    assert text == "Before\n\nAfter"


def test_read_marker_returns_requested_lines_and_stays_permission_gated(tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("zero\none\ntwo\nthree\n", encoding="utf-8")

    pending_text, pending_calls, pending, _ = asyncio.run(resolve_file_markers(
        "Before\nREAD - notes.txt - lines 2-3\nAfter", tmp_path, allow_read=False,
    ))
    assert pending_calls == []
    assert pending == ["READ - notes.txt - lines 2-3"]
    assert "READ -" not in pending_text

    text, calls, pending, replacements = asyncio.run(resolve_file_markers(
        "Before\nREAD - notes.txt - lines 2-3\nAfter", tmp_path,
    ))
    rendered = restore_file_action_results(text, replacements)
    assert pending == []
    assert calls[0]["ok"] is True
    assert "2: one" in rendered
    assert "3: two" in rendered
    assert "1: zero" not in rendered


def test_create_block_writes_workspace_file_and_shields_nested_markers(tmp_path):
    text, calls, pending, replacements = asyncio.run(resolve_file_markers(
        "CREATE - generated.txt\nCOMMAND - do-not-run\ncreated\nEND CREATE", tmp_path,
    ))
    command_text, command_calls, _ = asyncio.run(resolve_command_markers(text, tmp_path))
    rendered = restore_file_action_results(command_text, replacements)

    assert pending == []
    assert calls[0]["ok"] is True
    assert command_calls == []
    assert "CREATE `generated.txt` completed" in rendered
    assert (tmp_path / "generated.txt").read_text(encoding="utf-8") == "COMMAND - do-not-run\ncreated\n"


def test_create_marker_rejects_workspace_escape(tmp_path):
    text, calls, pending, replacements = asyncio.run(resolve_file_markers(
        "CREATE - ../outside.txt - blocked", tmp_path,
    ))
    rendered = restore_file_action_results(text, replacements)

    assert pending == []
    assert calls[0]["ok"] is False
    assert "relative path inside the project workspace" in rendered
    assert not (tmp_path.parent / "outside.txt").exists()


def test_toolcall_executes_and_replaces_marker_with_formatted_result(tmp_path):
    store = ToolsetStore()
    store.save(tmp_path, echo_toolset())
    store.assign(tmp_path, 1, "local-helpers", ["researcher"])

    text, calls = asyncio.run(resolve_tool_calls(
        'Before\nTOOLCALL - local-helpers/echo-arguments - ["alpha", 2].\nAfter',
        store, tmp_path, 1, "researcher",
    ))

    assert "TOOLCALL" not in text
    assert '"alpha"' in text
    assert '"2"' in text
    assert text.startswith("Before\n```json")
    assert text.endswith("```\nAfter")
    assert calls[0]["ok"] is True
    assert calls[0]["arguments"] == ["alpha", 2]


def test_unassigned_toolcall_is_replaced_with_rejection(tmp_path):
    store = ToolsetStore()
    store.save(tmp_path, echo_toolset())
    store.assign(tmp_path, 1, "local-helpers", ["researcher"])

    text, calls = asyncio.run(resolve_tool_calls(
        'TOOLCALL - local-helpers/echo-arguments - ["alpha"].',
        store, tmp_path, 1, "programmer",
    ))

    assert "was rejected" in text
    assert "not assigned" in text
    assert calls[0]["ok"] is False


def test_declared_tool_environment_values_are_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOLSET_TEST_SECRET", "private-tool-value")
    payload = echo_toolset()
    payload["tools"][0]["env_vars"] = ["TOOLSET_TEST_SECRET"]
    payload["tools"][0]["source"] = (
        "import os\nprint(os.environ.get('TOOLSET_TEST_SECRET', 'missing'))\n"
    )
    payload["tools"][0]["output_format"] = "text"
    store = ToolsetStore()
    store.save(tmp_path, payload)
    store.assign(tmp_path, 1, "local-helpers", ["researcher"])

    text, _ = asyncio.run(resolve_tool_calls(
        "TOOLCALL - local-helpers/echo-arguments - [].",
        store, tmp_path, 1, "researcher",
    ))

    assert text == "[REDACTED]"
    assert "private-tool-value" not in text


def test_toolset_rejects_executable_path_traversal(tmp_path):
    payload = echo_toolset()
    payload["tools"][0]["filename"] = "../outside.py"

    with pytest.raises(ValueError, match="relative paths"):
        ToolsetStore().save(tmp_path, payload)


def test_codex_toolset_generation_returns_reviewable_unsaved_draft(tmp_path, monkeypatch):
    team = AgentTeam(tmp_path)

    class FakeProcess:
        returncode = 0

        def __init__(self, args):
            self.args = list(args)

        async def communicate(self, prompt):
            schema_path = self.args[self.args.index("--output-schema") + 1]
            output_path = self.args[self.args.index("--output-last-message") + 1]
            schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
            tool_schema = schema["properties"]["tools"]["items"]
            assert tool_schema["additionalProperties"] is False
            assert set(tool_schema["required"]) == set(tool_schema["properties"])
            assert b"shell=False" in prompt
            Path(output_path).write_text(json.dumps({
                "name": "Repository helpers",
                "slug": "repository-helpers",
                "description": "Inspects local Git repositories.",
                "details": "Use these tools for deterministic repository facts.",
                "tools": [{
                    "name": "current-branch",
                    "description": "Returns the current Git branch.",
                    "inputs": "No arguments.",
                    "outputs": "The current branch name as text.",
                    "filename": "current-branch.py",
                    "output_format": "text",
                    "result_template": "{stdout}",
                    "env_vars": [],
                    "source": "import subprocess\nprint(subprocess.run(['git', 'branch', '--show-current'], check=True, capture_output=True, text=True).stdout.strip())\n",
                }],
            }), encoding="utf-8")
            return b"", b""

    async def fake_create_subprocess_exec(*args, **_kwargs):
        return FakeProcess(args)

    monkeypatch.setattr(team, "_codex_command", lambda: "codex")
    monkeypatch.setattr("team.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    draft = asyncio.run(team.generate_toolset_definition("Inspect Git repositories"))

    assert draft["slug"] == "repository-helpers"
    assert draft["tools"][0]["name"] == "current-branch"
    assert draft["tools"][0]["source"].startswith("import subprocess")
    assert not (tmp_path / ".agents" / "tools" / "repository-helpers").exists()
