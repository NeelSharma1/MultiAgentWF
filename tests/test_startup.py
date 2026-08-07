import asyncio

import team
from team import AgentTeam


def test_mcp_startup_uses_project_virtual_environment(tmp_path, monkeypatch):
    captured = {}

    class FakeMCP:
        def __init__(self, *, name, params, cache_tools_list, client_session_timeout_seconds):
            captured["name"] = name
            captured["params"] = params
            captured["cache_tools_list"] = cache_tools_list
            captured["client_session_timeout_seconds"] = client_session_timeout_seconds

        async def connect(self):
            captured["connected"] = True

        async def cleanup(self):
            captured["cleaned_up"] = True

    monkeypatch.delenv("PYTHON", raising=False)
    monkeypatch.setattr(team, "MCPServerStdio", FakeMCP)
    interpreter = tmp_path / "venv" / ("Scripts" if team.os.name == "nt" else "bin") / (
        "python.exe" if team.os.name == "nt" else "python"
    )
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("", encoding="utf-8")

    agent_team = AgentTeam(tmp_path)
    asyncio.run(agent_team.start())

    assert captured["name"] == "shared-context"
    assert captured["params"]["command"] == str(interpreter.resolve())
    assert captured["params"]["args"] == [str(tmp_path / "mcp_server.py")]
    assert captured["client_session_timeout_seconds"] == 660
    assert captured["connected"] is True
