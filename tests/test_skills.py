import asyncio
import json
from pathlib import Path

import pytest

from project_store import ProjectStore
from skills import SkillStore, parse_skill_md, run_skill_script_async
from team import AgentTeam, ProviderError


def test_skill_library_and_project_agent_assignments_are_scoped(tmp_path):
    db_path = tmp_path / "skills.db"
    projects = ProjectStore(db_path)
    project = projects.create("Skill workspace")
    store = SkillStore(db_path)
    skill = store.save(
        "Echo JSON", "Returns the provided payload.",
        {"type": "object", "required": ["value"]},
        {"type": "object"}, "python",
        "import json, sys\nprint(json.dumps({'echo': json.load(sys.stdin)['value']}))",
    )

    store.assign(skill["id"], project["id"], ["researcher"])

    assert store.summaries(project["id"], "researcher")[0]["summary"] == "Returns the provided payload."
    assert "script" not in store.summaries(project["id"], "researcher")[0]
    assert store.summaries(project["id"], "programmer") == []


def test_skill_runner_returns_structured_output_and_errors(tmp_path):
    db_path = tmp_path / "skills.db"
    projects = ProjectStore(db_path)
    project = projects.create("Runner workspace", root_path=str(tmp_path))
    store = SkillStore(db_path)
    skill = store.save(
        "Echo JSON", "Echo", {"required": ["value"]}, {}, "python",
        "import json, sys\nprint(json.dumps({'echo': json.load(sys.stdin)['value']}))",
    )
    store.assign(skill["id"], project["id"], ["researcher"])

    result = asyncio.run(run_skill_script_async(skill, {"value": "ok"}, tmp_path))

    assert result["ok"] is True
    assert result["output"] == {"echo": "ok"}
    assert result["exit_code"] == 0


def test_skill_secrets_are_declared_scoped_and_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILL_TEST_KEY", "super-secret-value")
    monkeypatch.setenv("UNDECLARED_SECRET", "must-not-reach-child")
    store = SkillStore(tmp_path / "secrets.db")
    skill = store.save(
        "Secret probe", "Reads one configured service key.", {}, {}, "python",
        "import json, os\nprint(json.dumps({'declared': os.getenv('SKILL_TEST_KEY'), 'other': os.getenv('UNDECLARED_SECRET')}))",
        required_secrets=[{"name": "SKILL_TEST_KEY", "label": "Test key", "required": True}],
    )

    assert skill["required_secrets"] == [{
        "name": "SKILL_TEST_KEY", "label": "Test key", "description": "", "required": True,
    }]
    assert "super-secret-value" not in skill["skill_md"]
    result = asyncio.run(run_skill_script_async(skill, {}, tmp_path))

    assert result["ok"] is True
    assert result["output"] == {"declared": "[REDACTED]", "other": None}
    assert "super-secret-value" not in result["stdout"]


def test_agent_prompt_contains_skill_summary_without_script(tmp_path):
    team = AgentTeam(tmp_path)
    skill = team.skills.save(
        "Private transform", "Transforms a payload.", {}, {}, "python",
        "print('this script must stay out of the prompt')",
    )
    team.skills.assign(skill["id"], 1, ["researcher"])

    instructions = team._instructions("researcher", 1)

    assert "Transforms a payload." in instructions
    assert "this script must stay out of the prompt" not in instructions
    assert "run_assigned_skill" in instructions


def test_codex_skill_generation_uses_strict_safe_schema_and_restores_objects(tmp_path, monkeypatch):
    team = AgentTeam(tmp_path)

    class FakeProcess:
        returncode = 0

        def __init__(self, args):
            self.args = list(args)

        async def communicate(self, _prompt):
            schema_path = self.args[self.args.index("--output-schema") + 1]
            output_path = self.args[self.args.index("--output-last-message") + 1]
            schema = json.loads(Path(schema_path).read_text())
            assert schema["properties"]["inputs"]["type"] == "string"
            assert schema["properties"]["outputs"]["type"] == "string"
            Path(output_path).write_text(json.dumps({
                "name": "Echo Text",
                "slug": "echo_text",
                "summary": "Echoes a text value.",
                "inputs": '{"type":"object","required":["text"]}',
                "outputs": '{"type":"object","properties":{"text":{"type":"string"}}}',
                "language": "python",
                "script": "print('{}')",
            }))
            return b"", b""

    async def fake_create_subprocess_exec(*args, **_kwargs):
        return FakeProcess(args)

    monkeypatch.setattr(team, "_codex_command", lambda: "codex")
    monkeypatch.setattr("team.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    result = asyncio.run(team.generate_skill_definition("Echo text"))

    assert result["inputs"] == {"type": "object", "required": ["text"]}
    assert result["outputs"]["properties"]["text"]["type"] == "string"


def test_codex_skill_generation_keeps_structured_cli_diagnostic(tmp_path, monkeypatch):
    team = AgentTeam(tmp_path)

    class FailedProcess:
        returncode = 1

        async def communicate(self, _prompt):
            return b"", b'ERROR: {\n  "code": "invalid_json_schema"\n}'

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return FailedProcess()

    monkeypatch.setattr(team, "_codex_command", lambda: "codex")
    monkeypatch.setattr("team.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(ProviderError, match="invalid_json_schema"):
        asyncio.run(team.generate_skill_definition("Echo text"))


def test_acp_packages_select_the_current_os_version_and_support_progressive_disclosure(tmp_path):
    db_path = tmp_path / "acp.db"
    projects = ProjectStore(db_path)
    project = projects.create("ACP workspace", root_path=str(tmp_path))
    store = SkillStore(db_path)
    skill = store.save(
        "Git Diff", "Show the current repository diff. Use when reviewing local changes.",
        {}, {"type": "string"}, "shell", "printf '{\"diff\":\"base\"}'",
        "git_diff", version="1.0.0", platform="any", skill_type="development",
        body="# Git Diff\n\nShow a concise diff.", output_format="diff",
    )
    store.save(
        "Git Diff", skill["summary"], {}, {"type": "string"}, "shell", "printf '{\"diff\":\"mac\"}'",
        "git_diff", skill_id=skill["id"], version="2.0.0", platform="macos", skill_type="development",
        body="# Git Diff macOS", output_format="diff",
    )
    store.assign(skill["id"], project["id"], ["researcher"])

    selected = store.get(skill["id"], platform="macos")
    assert selected["version"] == "2.0.0"
    assert selected["format"] == "agent-skills/v1"
    assert parse_skill_md(selected["skill_md"])["name"] == "git-diff"
    loaded = store.load_assigned(skill["id"], project["id"], "researcher", platform="macos")
    assert loaded["body"] == "# Git Diff macOS"

    materialized = store.materialize(tmp_path, project["id"], "researcher")
    assert Path(materialized[0]["path"]).parts[-3:] == (".agents", "skills", "git-diff")
    assert (tmp_path / ".agents/skills/git-diff/SKILL.md").is_file()


def test_marketplace_search_normalizes_catalog_records(tmp_path, monkeypatch):
    import skills as skills_module

    class Response:
        status_code = 200
        content = b"{}"
        text = ""

        def json(self):
            return {"data": {"skills": [{
                "id": "git-diff", "name": "git-diff", "author": "example",
                "description": "Review diffs", "githubUrl": "https://github.com/example/skills",
                "skillUrl": "https://skillsmp.com/skills/git-diff", "stars": 12,
            }], "pagination": {"page": 1}}}

    class Client:
        def __init__(self, *args, **kwargs):
            self.headers = kwargs.get("headers", {})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(skills_module.httpx, "AsyncClient", Client)
    result = asyncio.run(SkillStore(tmp_path / "market.db").search_marketplace("git"))

    assert result["skills"][0]["github_url"].endswith("/example/skills")
    assert result["skills"][0]["stars"] == 12
