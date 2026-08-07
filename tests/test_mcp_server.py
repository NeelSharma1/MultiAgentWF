import asyncio
import inspect
import threading

import mcp_server


def test_project_test_tool_runs_the_blocking_bridge_off_the_mcp_event_loop(tmp_path, monkeypatch):
    interpreter = tmp_path / "venv" / ("Scripts" if mcp_server.os.name == "nt" else "bin") / (
        "python.exe" if mcp_server.os.name == "nt" else "python"
    )
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("", encoding="utf-8")

    monkeypatch.setattr(
        mcp_server.projects,
        "get",
        lambda _project_id: {"root_path": str(tmp_path)},
    )
    monkeypatch.setattr(mcp_server.definitions, "get", lambda *_args: {})
    monkeypatch.setattr(
        mcp_server.projects,
        "agent_action_permissions",
        lambda *_args: {"effective_commands": True},
    )
    monkeypatch.setattr(mcp_server, "project_python_executable", lambda _root: interpreter)
    called = {}
    event_loop_thread = threading.get_ident()

    def bridge(*_args):
        called["thread"] = threading.get_ident()
        return {"ok": True, "exit_code": 0}

    monkeypatch.setattr(mcp_server, "run_project_tests_via_app", bridge)

    result = asyncio.run(
        mcp_server.run_project_tests("orchestrator", 1, ["-q"])
    )

    assert inspect.iscoroutinefunction(mcp_server.run_project_tests)
    assert result == {"ok": True, "exit_code": 0}
    assert called["thread"] != event_loop_thread
