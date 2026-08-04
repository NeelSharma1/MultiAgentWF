from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from team import AgentTeam
from toolsets import ToolsetStore, resolve_tool_calls


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
    assert "Returns each positional argument as JSON." not in instructions


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
