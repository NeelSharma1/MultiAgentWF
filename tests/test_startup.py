import asyncio
import sys

import team
from team import AgentTeam


def test_mcp_startup_uses_current_python_by_default(tmp_path, monkeypatch):
    captured = {}

    class FakeMCP:
        def __init__(self, *, name, params, cache_tools_list):
            captured["name"] = name
            captured["params"] = params
            captured["cache_tools_list"] = cache_tools_list

        async def connect(self):
            captured["connected"] = True

        async def cleanup(self):
            captured["cleaned_up"] = True

    monkeypatch.delenv("PYTHON", raising=False)
    monkeypatch.setattr(team, "MCPServerStdio", FakeMCP)

    agent_team = AgentTeam(tmp_path)
    asyncio.run(agent_team.start())

    assert captured["name"] == "shared-context"
    assert captured["params"]["command"] == sys.executable
    assert captured["params"]["args"] == [str(tmp_path / "mcp_server.py")]
    assert captured["connected"] is True
