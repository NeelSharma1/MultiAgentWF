import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

from team import AgentTeam, ProviderError


def test_provider_errors_are_returned_and_persisted(tmp_path):
    team = AgentTeam(tmp_path)
    team.configs.save("researcher", "google", "models/gemini-test", "", "")

    async def fail(*args, **kwargs):
        raise ProviderError(
            "model is unavailable",
            provider="google",
            status_code=404,
            code="NOT_FOUND",
            request_id="request-123",
            body={"error": {"code": "NOT_FOUND", "message": "model is unavailable"}},
        )

    team._google_chat = fail
    result = asyncio.run(team.chat("researcher", "hello?", project_id=1))

    assert result["ok"] is False
    assert result["error"]["status_code"] == 404
    assert "Request ID: request-123" in result["response"]
    history = team.configs.history("researcher", project_id=1)
    assert [message["speaker"] for message in history] == ["user", "error"]
    assert "Provider response" in history[1]["content"]


def test_internal_continuation_prompt_is_not_saved_as_a_user_message(tmp_path):
    team = AgentTeam(tmp_path)
    team.configs.save("researcher", "compatible", "local-model", "http://localhost:1234/v1", "")

    async def fake_chat(*_args, **_kwargs):
        return {"response": "Researcher completed the queued work.", "answered_by": "Researcher"}

    team._agents_chat = fake_chat
    result = asyncio.run(
        team.chat(
            "researcher",
            "Process the queued team messages now.",
            project_id=1,
            record_user_message=False,
        )
    )

    assert result["ok"] is True
    assert result["user_message"] is None
    history = team.configs.history("researcher")
    assert [item["speaker"] for item in history] == ["assistant"]
    assert "Process the queued team messages now." not in history[0]["content"]


def test_queued_user_turns_are_not_leaked_into_the_current_provider_prompt(tmp_path):
    team = AgentTeam(tmp_path)
    team.configs.save("researcher", "compatible", "local-model", "http://localhost:1234/v1", "")
    first_user = team.configs.add_message(
        "researcher", "user", "first request", "compatible", "local-model", 1,
    )
    first_run = team.configs.create_chat_run(
        "researcher", 1,
        {"message": "first request", "project_id": 1, "user_message_id": first_user["id"]},
    )
    team.configs.claim_queued_chat_run(first_run["id"])
    second_user = team.configs.add_message(
        "researcher", "user", "future request", "compatible", "local-model", 1,
    )
    team.configs.create_chat_run(
        "researcher", 1,
        {"message": "future request", "project_id": 1, "user_message_id": second_user["id"]},
    )
    captured = {}

    async def fake_chat(role, message, config, project_id, reply_to_id, attachments, exclude_message_ids):
        captured["message"] = message
        captured["excluded"] = exclude_message_ids
        return {"response": "first response", "answered_by": "Researcher"}

    team._agents_chat = fake_chat
    result = asyncio.run(
        team.chat(
            "researcher", "first request", project_id=1,
            record_user_message=False, user_message_id=first_user["id"],
        )
    )

    assert result["ok"] is True
    assert captured["message"].count("first request") == 1
    assert "future request" not in captured["message"]
    assert first_user["id"] in captured["excluded"]
    assert second_user["id"] in captured["excluded"]


def test_gemini_bridge_forces_text_when_role_instructions_mention_tools(tmp_path, monkeypatch):
    team = AgentTeam(tmp_path)
    team.configs.save("researcher", "google", "models/gemini-test", "", "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    class FakeClient:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def create(self, *, model, messages):
            assert "Do not emit tool calls or function calls" in messages[0]["content"]
            return SimpleNamespace(
                model=model,
                choices=[SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content="Gemini answer", tool_calls=None),
                )],
            )

    monkeypatch.setattr("team.AsyncOpenAI", FakeClient)
    result = asyncio.run(team._google_chat("researcher", "hello", team.configs.get("researcher")))

    assert result["response"] == "Gemini answer"


def test_codex_native_command_output_is_preserved_verbatim(tmp_path, monkeypatch):
    team = AgentTeam(tmp_path)
    team.configs.save("researcher", "codex", "gpt-test", "", "")
    monkeypatch.setattr(team, "_codex_command", lambda: "/usr/bin/codex")
    monkeypatch.setattr(team, "_codex_tui_command", lambda *args: "exact native status panel")

    result = asyncio.run(team.native_command("researcher", "/status", project_id=1))

    assert result["response"] == "exact native status panel"
    history = team.configs.history("researcher", project_id=1)
    assert history[-1]["speaker"] == "native"
    assert history[-1]["content"] == "exact native status panel"


def test_codex_prompt_requires_chatgpt_login_before_starting_a_request(tmp_path, monkeypatch):
    agent_team = AgentTeam(tmp_path)
    agent_team.configs.save("researcher", "codex", "", "", "")
    monkeypatch.setattr(agent_team, "_codex_command", lambda: "codex")

    async def not_logged_in():
        return {"connected": False, "detail": "Not logged in", "login_output": ""}

    monkeypatch.setattr(agent_team, "codex_login_status", not_logged_in)
    result = asyncio.run(agent_team.chat("researcher", "hello?"))

    assert result["ok"] is False
    assert result["error"]["status_code"] == 401
    assert result["error"]["code"] == "codex_not_authenticated"
    assert "Open Connections" in result["response"]
    assert "Missing bearer" not in result["response"]


def test_codex_resume_places_working_directory_before_resume_and_keeps_diagnostics(tmp_path, monkeypatch):
    team = AgentTeam(tmp_path)
    team.configs.save("researcher", "codex", "gpt-test", "", "")
    team.configs.save_codex_session("researcher", 1, "session-123", "gpt-test", "")
    monkeypatch.setattr(team, "_codex_command", lambda: "/usr/bin/codex")
    monkeypatch.setattr(team, "_project_root", lambda _project_id: tmp_path)
    interpreter = tmp_path / "venv" / ("Scripts" if os.name == "nt" else "bin") / (
        "python.exe" if os.name == "nt" else "python"
    )
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("", encoding="utf-8")

    async def logged_in():
        return {"connected": True, "detail": "Logged in using ChatGPT", "login_output": ""}

    monkeypatch.setattr(team, "codex_login_status", logged_in)
    monkeypatch.setattr("team.project_python_executable", lambda _root: interpreter)
    captured = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self, _prompt):
            Path(captured["output_path"]).write_text("Codex resumed response")
            return b'{"type":"thread.started","thread_id":"session-123"}\n', b""

    async def fake_create(*args, **_kwargs):
        captured["args"] = list(args)
        captured["output_path"] = args[args.index("--output-last-message") + 1]
        return FakeProcess()

    monkeypatch.setattr("team.asyncio.create_subprocess_exec", fake_create)
    result = asyncio.run(team._codex_chat_locked(
        "researcher", "Continue", team.configs.get("researcher"), 1, None, [],
    ))

    args = captured["args"]
    assert args.index("-C") < args.index("resume")
    assert args[args.index("-C") + 1] == str(tmp_path)
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert "--ask-for-approval" not in args
    assert result["response"] == "Codex resumed response"


def test_codex_resume_prompt_explains_workspace_agents_and_native_subagents(tmp_path, monkeypatch):
    team = AgentTeam(tmp_path)
    team.configs.save("researcher", "codex", "gpt-test", "", "")
    team.configs.save_codex_session("researcher", 1, "session-123", "gpt-test", "")
    monkeypatch.setattr(team, "_codex_command", lambda: "/usr/bin/codex")
    monkeypatch.setattr(team, "_project_root", lambda _project_id: tmp_path)
    interpreter = tmp_path / "venv" / ("Scripts" if os.name == "nt" else "bin") / (
        "python.exe" if os.name == "nt" else "python"
    )
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("", encoding="utf-8")

    async def logged_in():
        return {"connected": True, "detail": "Logged in using ChatGPT", "login_output": ""}

    monkeypatch.setattr(team, "codex_login_status", logged_in)
    monkeypatch.setattr("team.project_python_executable", lambda _root: interpreter)
    captured = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self, prompt):
            captured["prompt"] = prompt.decode()
            Path(captured["output_path"]).write_text("Codex resumed response")
            return b'{"type":"thread.started","thread_id":"session-123"}\n', b""

    async def fake_create(*args, **_kwargs):
        captured["output_path"] = args[list(args).index("--output-last-message") + 1]
        return FakeProcess()

    monkeypatch.setattr("team.asyncio.create_subprocess_exec", fake_create)
    asyncio.run(team._codex_chat_locked(
        "researcher", "Continue", team.configs.get("researcher"), 1, None, [],
    ))

    prompt = captured["prompt"]
    assert "<workspace_team_delegation>" in prompt
    assert "Codex-native subagents" in prompt
    assert "PRIMARY DELEGATION RULE" in prompt
    assert "Do not create native subagents by default" in prompt
    assert "configured workspace-agent system is the primary coordination path" in prompt
    assert "role_id=programmer" in prompt
    assert "send_agent_message(sender_role='researcher'" in prompt
    assert "run_project_tests" in prompt


def test_codex_command_permission_can_run_tests_with_project_python(tmp_path, monkeypatch):
    team = AgentTeam(tmp_path)
    team.configs.save("researcher", "codex", "gpt-test", "", "")
    team.projects.set_agent_action_permissions(1, "researcher", True, False)
    monkeypatch.setattr(team, "_codex_command", lambda: "/usr/bin/codex")
    monkeypatch.setattr(team, "_project_root", lambda _project_id: tmp_path)

    environment_root = tmp_path / "venv"
    bin_directory = environment_root / ("Scripts" if os.name == "nt" else "bin")
    bin_directory.mkdir(parents=True)
    (bin_directory / ("python.exe" if os.name == "nt" else "python")).write_text("", encoding="utf-8")

    async def logged_in():
        return {"connected": True, "detail": "Logged in using ChatGPT", "login_output": ""}

    monkeypatch.setattr(team, "codex_login_status", logged_in)
    captured = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self, prompt):
            captured["prompt"] = prompt.decode()
            Path(captured["output_path"]).write_text("Tests completed", encoding="utf-8")
            return b'{"type":"thread.started","thread_id":"session-tests"}\n', b""

    async def fake_create(*args, **kwargs):
        captured["args"] = list(args)
        captured["env"] = kwargs["env"]
        captured["output_path"] = args[args.index("--output-last-message") + 1]
        return FakeProcess()

    monkeypatch.setattr("team.asyncio.create_subprocess_exec", fake_create)
    result = asyncio.run(team._codex_chat_locked(
        "researcher", "Run the test suite", team.configs.get("researcher"), 1, None, [],
    ))

    args = captured["args"]
    assert args[args.index("--sandbox") + 1] == "workspace-write"
    config_values = [args[index + 1] for index, value in enumerate(args[:-1]) if value == "-c"]
    assert any(value.startswith("mcp_servers.multiagent_workflow.command=") for value in config_values)
    assert any(value.startswith("mcp_servers.multiagent_workflow.args=") for value in config_values)
    assert 'mcp_servers.multiagent_workflow.default_tools_approval_mode="approve"' in config_values
    assert 'mcp_servers.multiagent_workflow.tools.run_project_tests.approval_mode="approve"' in config_values
    assert "mcp_servers.multiagent_workflow.tool_timeout_sec=660" in config_values
    assert captured["env"]["VIRTUAL_ENV"] == str(environment_root)
    assert captured["env"]["PATH"].split(os.pathsep)[0] == str(bin_directory)
    assert "authorized this agent to execute commands" in captured["prompt"]
    assert "project's local virtual environment" in captured["prompt"]
    assert "-m pytest" in captured["prompt"]
    assert result["response"] == "Tests completed"


def test_codex_direct_mcp_uses_the_running_app_endpoint(tmp_path, monkeypatch):
    agent_team = AgentTeam(tmp_path)
    monkeypatch.setenv("WORKSPACE_APP_URL", "http://127.0.0.1:8123/")

    args = agent_team._codex_team_mcp_config_args()
    config_values = [args[index + 1] for index, value in enumerate(args[:-1]) if value == "-c"]

    assert "mcp_servers.multiagent_workflow.url=\"http://127.0.0.1:8123/mcp/\"" in config_values
    assert 'mcp_servers.multiagent_workflow.default_tools_approval_mode="approve"' in config_values
    assert 'mcp_servers.multiagent_workflow.tools.run_project_tests.approval_mode="approve"' in config_values
    assert "mcp_servers.multiagent_workflow.tool_timeout_sec=660" in config_values
    assert not any(value.startswith("mcp_servers.multiagent_workflow.command=") for value in config_values)
    assert not any(value.startswith("mcp_servers.multiagent_workflow.args=") for value in config_values)


def test_codex_temporary_external_approval_uses_unrestricted_sandbox(tmp_path, monkeypatch):
    team = AgentTeam(tmp_path)
    team.configs.save("researcher", "codex", "gpt-test", "", "")
    monkeypatch.setattr(team, "_codex_command", lambda: "/usr/bin/codex")

    async def logged_in():
        return {"connected": True, "detail": "Logged in using ChatGPT", "login_output": ""}

    monkeypatch.setattr(team, "codex_login_status", logged_in)
    captured = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self, _prompt):
            Path(captured["output_path"]).write_text("Codex unrestricted response")
            return b'{"type":"thread.started","thread_id":"session-unrestricted"}\n', b""

    async def fake_create(*args, **_kwargs):
        captured["args"] = list(args)
        captured["output_path"] = args[args.index("--output-last-message") + 1]
        return FakeProcess()

    monkeypatch.setattr("team.asyncio.create_subprocess_exec", fake_create)
    result = asyncio.run(team._codex_chat_locked(
        "researcher", "Inspect the sibling workspace", team.configs.get("researcher"), 1, None, [], "external",
    ))

    args = captured["args"]
    assert args[args.index("--sandbox") + 1] == "danger-full-access"
    assert result["response"] == "Codex unrestricted response"


def test_context_usage_and_automatic_compaction_watermark(tmp_path, monkeypatch):
    team = AgentTeam(tmp_path)
    config = team.configs.save(
        "researcher", "codex", "gpt-test", "", "", context_window_tokens=1_000,
        context_compaction_threshold=50,
    )
    team.configs.add_message("researcher", "user", "x" * 4_000, "codex", "gpt-test")
    team.configs.save_codex_session("researcher", 1, "session-context", "gpt-test", "")
    session = team.configs.codex_session("researcher", 1)
    assert session is not None
    assert team.context_usage("researcher", 1)["remaining_percent"] == 0
    calls = []
    monkeypatch.setattr(team, "_codex_tui_command", lambda *args: calls.append(args) or "Compacted")

    asyncio.run(team._compact_context_if_needed(
        "researcher", config, 1, session, "/usr/bin/codex", tmp_path,
    ))

    assert calls[0][1] == "/compact"
    assert team.configs.codex_session("researcher", 1)["compacted_message_id"] > 0


def test_codex_json_usage_includes_effective_context_window(tmp_path):
    team = AgentTeam(tmp_path)
    record = team._codex_usage_record([
        {"type": "thread.started", "thread_id": "thread-usage"},
        {"type": "token_count", "info": {
            "model_context_window": 258400,
            "last_token_usage": {
                "input_tokens": 1200, "cached_input_tokens": 500,
                "output_tokens": 300, "reasoning_output_tokens": 100,
                "total_tokens": 1500,
            },
        }},
        {"type": "turn.completed", "usage": {"input_tokens": 1200, "output_tokens": 300, "total_tokens": 1500}},
    ], {"context_window_tokens": 128000})

    assert record["context_tokens"] == 1500
    assert record["context_window_tokens"] == 258400
    assert record["source"] == "codex_token_count"
    assert record["exact"] is True


def test_api_and_compatible_providers_compact_to_saved_summary(tmp_path, monkeypatch):
    team = AgentTeam(tmp_path)
    config = team.configs.save(
        "researcher", "compatible", "local-model", "http://localhost:1234/v1", "",
        context_window_tokens=1_000, context_compaction_threshold=50,
    )
    team.configs.add_message("researcher", "user", "x" * 4_000, "compatible", "local-model")
    calls = []

    async def summarize(compaction_config, previous_summary, messages):
        calls.append((compaction_config, previous_summary, messages))
        return "The user asked for a long-running task; preserve its key requirements."

    monkeypatch.setattr(team, "_summarize_context", summarize)
    asyncio.run(team._compact_context_if_needed("researcher", config, 1))

    summary = team.configs.context_summary("researcher", 1)
    assert calls
    assert summary is not None
    assert summary["compacted_message_id"] > 0
    prompt = team._conversation_prompt("researcher", "Continue")
    assert "conversation_summary" in prompt
    assert "key requirements" in prompt
    assert "x" * 100 not in prompt


def test_api_provider_can_request_agent_directed_compaction(tmp_path, monkeypatch):
    team = AgentTeam(tmp_path)
    team.configs.save(
        "researcher", "compatible", "local-model", "http://localhost:1234/v1", "",
        context_window_tokens=1_000, context_compaction_threshold=0,
    )
    team.configs.add_message("researcher", "user", "x" * 4_000, "compatible", "local-model")

    async def fake_chat(*_args, **_kwargs):
        return {"response": "Completed the task.\n<context_compaction_request/>", "answered_by": "Researcher"}

    async def summarize(*_args, **_kwargs):
        return "Compact memory of the completed task."

    monkeypatch.setattr(team, "_agents_chat", fake_chat)
    monkeypatch.setattr(team, "_summarize_context", summarize)
    result = asyncio.run(team.chat("researcher", "Continue", project_id=1))

    assert result["ok"] is True
    assert "context_compaction_request" not in result["response"]
    assert team.configs.context_summary("researcher", 1)["summary"].startswith("Compact memory")


def test_inter_agent_message_is_synthesized_into_the_next_prompt(tmp_path):
    team = AgentTeam(tmp_path)
    team.configs.save("researcher", "compatible", "local-model", "http://localhost:1234/v1", "")
    team.send_agent_message(
        "orchestrator", "researcher", "Investigate the failing request first.", "command",
    )
    team.send_agent_message(
        "orchestrator", "researcher", "Add a regression test for the failure.", "command",
    )
    captured = {}

    async def fake_chat(role, message, config, project_id, reply_to_id, attachments, exclude_message_ids):
        captured["message"] = message
        captured["excluded"] = exclude_message_ids
        return {"response": "done", "answered_by": "Researcher"}

    team._agents_chat = fake_chat
    result = asyncio.run(team.chat("researcher", "Start now"))

    assert result["ok"] is True
    assert "<new_inter_agent_messages>" not in captured["message"]
    assert "<compiled_commands>" not in captured["message"]
    assert "<compiled_command_request>" not in captured["message"]
    assert "COMPILED COMMAND REQUEST:" not in captured["message"]
    assert "COMMAND from orchestrator" in captured["message"]
    assert "Investigate the failing request first." in captured["message"]
    assert "- Investigate the failing request first." in captured["message"]
    assert "- Add a regression test for the failure." in captured["message"]
    assert "Add a regression test for the failure." in captured["message"]
    history = team.configs.history("researcher")
    incoming_messages = [item for item in history if item["speaker"] == "agent"]
    assert len(incoming_messages) == 1
    incoming = incoming_messages[0]
    assert incoming["delivery_status"] == "delivered"
    assert incoming["id"] in captured["excluded"]


def test_historical_compiled_command_is_flattened_for_provider_history(tmp_path):
    team = AgentTeam(tmp_path)
    team.configs.save("researcher", "compatible", "local-model", "http://localhost:1234/v1", "")
    team.send_agent_message("orchestrator", "researcher", "First task", "command")
    team.send_agent_message("orchestrator", "researcher", "Second task", "command")

    prompt = team._conversation_prompt("researcher", "Continue")

    assert "COMPILED COMMAND REQUEST:" not in prompt
    assert "<compiled_command_request>" not in prompt
    assert "[COMMAND from orchestrator]" in prompt
    assert "- First task" in prompt
    assert "- Second task" in prompt


def test_failed_prompt_releases_inter_agent_message(tmp_path):
    team = AgentTeam(tmp_path)
    team.configs.save("researcher", "compatible", "local-model", "http://localhost:1234/v1", "")
    team.send_agent_message("orchestrator", "researcher", "Please report back.", "report")

    async def fail(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")

    team._agents_chat = fail
    result = asyncio.run(team.chat("researcher", "Continue"))
    assert result["ok"] is False
    assert team.configs.agent_inbox("researcher", include_delivered=False)[0]["delivery_status"] == "pending"


def test_gemini_can_send_an_inter_agent_message_with_a_function_call(tmp_path, monkeypatch):
    team = AgentTeam(tmp_path)
    team.configs.save("researcher", "google", "models/gemini-test", "", "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    calls = []

    class FakeClient:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def create(self, *, model, messages, tools=None):
            calls.append({"messages": messages, "tools": tools})
            if len(calls) == 1:
                return SimpleNamespace(
                    model=model,
                    choices=[SimpleNamespace(
                        finish_reason="tool_calls",
                        message=SimpleNamespace(
                            content=None,
                            tool_calls=[SimpleNamespace(
                                id="call-1",
                                extra_content={"google": {"thought_signature": "test-signature"}},
                                function=SimpleNamespace(
                                    name="send_agent_message",
                                    arguments=json.dumps({
                                        "recipient_role": "reviewer",
                                        "relationship": "report",
                                        "content": "Research is complete.",
                                    }),
                                ),
                            )],
                        ),
                    )],
                )
            return SimpleNamespace(
                model=model,
                choices=[SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content="Message sent.", tool_calls=None),
                )],
            )

    monkeypatch.setattr("team.AsyncOpenAI", FakeClient)
    result = asyncio.run(team._google_chat("researcher", "Finish", team.configs.get("researcher")))

    assert result["response"] == "Message sent."
    inbox = team.configs.agent_inbox("reviewer", include_delivered=False)
    assert inbox[0]["source_role"] == "researcher"
    assert inbox[0]["message_kind"] == "report"
    assert calls[0]["tools"]
    assistant_tools = next(item["tool_calls"] for item in calls[1]["messages"] if item.get("role") == "assistant")
    assert assistant_tools[0]["extra_content"]["google"]["thought_signature"] == "test-signature"
