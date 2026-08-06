import pytest

from project_store import ProjectStore
from runtime_config import RuntimeConfigStore


def test_runtime_defaults_save_and_history(tmp_path):
    store = RuntimeConfigStore(tmp_path / "runtime.db")
    assert store.get("programmer")["provider"] == "codex"
    saved = store.save(
        "researcher", "compatible", "qwen3", "http://model-box:11434/v1", "LOCAL_API_KEY", "high",
        context_window_tokens=200_000, context_compaction_threshold=80,
    )
    assert saved["model"] == "qwen3"
    assert saved["reasoning_effort"] == "high"
    assert saved["context_window_tokens"] == 200_000
    assert saved["context_compaction_threshold"] == 80
    store.add_message("researcher", "user", "hello", "compatible", "qwen3")
    assert store.history("researcher")[0]["content"] == "hello"
    store.add_message("researcher", "user", "other project", "compatible", "qwen3", project_id=2)
    assert [item["content"] for item in store.history("researcher", project_id=2)] == ["other project"]
    store.clear_history("researcher")
    assert store.history("researcher") == []
    assert len(store.history("researcher", project_id=2)) == 1


def test_codex_sessions_are_scoped_and_cleared_with_chat(tmp_path):
    store = RuntimeConfigStore(tmp_path / "runtime.db")
    store.save_codex_session("programmer", 1, "thread-one", "gpt-test", "high")
    store.save_codex_session("programmer", 2, "thread-two")
    assert store.codex_session("programmer", 1)["session_id"] == "thread-one"
    assert store.codex_session("programmer", 2)["session_id"] == "thread-two"
    store.mark_codex_context_compacted("programmer", 2, 17)
    assert store.codex_session("programmer", 2)["compacted_message_id"] == 17
    store.clear_history("programmer", 1)
    assert store.codex_session("programmer", 1) is None
    assert store.codex_session("programmer", 2)["session_id"] == "thread-two"


def test_provider_context_usage_is_scoped_and_cleared_with_chat(tmp_path):
    store = RuntimeConfigStore(tmp_path / "context-usage.db")
    store.save_context_usage(
        "programmer", 1, "codex", "gpt-test", input_tokens=120, output_tokens=30,
        total_tokens=150, context_tokens=150, context_window_tokens=1000,
        observed_message_id=9, source="codex_token_count", exact=True,
    )
    usage = store.context_usage("programmer", 1)
    assert usage["context_tokens"] == 150
    assert usage["exact"] == 1
    assert store.context_usage("programmer", 2) is None
    store.clear_history("programmer", 1)
    assert store.context_usage("programmer", 1) is None


def test_compatible_requires_base_url(tmp_path):
    store = RuntimeConfigStore(tmp_path / "runtime.db")
    with pytest.raises(ValueError, match="base URL"):
        store.save("reviewer", "compatible", "local-model", "", "")


def test_retired_gemini_model_is_migrated(tmp_path):
    db_path = tmp_path / "runtime.db"
    store = RuntimeConfigStore(db_path)
    store.save("researcher", "google", "models/gemini-2.5-flash-lite", "", "")
    store = RuntimeConfigStore(db_path)
    assert store.get("researcher")["model"] == "models/gemini-flash-lite-latest"


def test_retired_gemini_model_is_migrated_for_project_profiles(tmp_path):
    db_path = tmp_path / "project-runtime.db"
    project_id = ProjectStore(db_path).create("Gemini project")["id"]
    store = RuntimeConfigStore(db_path)
    store.save("researcher", "google", "models/gemini-2.5-flash-lite", "", "", project_id=project_id)
    reopened = RuntimeConfigStore(db_path)
    assert reopened.get("researcher", project_id=project_id)["model"] == "models/gemini-flash-lite-latest"


def test_replies_and_attachments_are_persisted(tmp_path):
    store = RuntimeConfigStore(tmp_path / "runtime.db")
    original = store.add_message("researcher", "assistant", "Initial finding", "google", "gemini", 3)
    reply = store.add_message(
        "researcher", "user", "Can you expand?", "google", "gemini", 3, original["id"]
    )
    attachment = store.create_attachment(3, "researcher", "notes.txt", "text/plain", 5, "/tmp/notes.txt")
    assert store.pending_attachments([attachment["id"]], "researcher", 3)[0]["name"] == "notes.txt"
    store.attach_to_message([attachment["id"]], reply["id"])
    history = store.history("researcher", project_id=3)
    assert history[1]["reply_to_id"] == original["id"]
    assert history[1]["attachments"][0]["name"] == "notes.txt"
    with pytest.raises(ValueError, match="already sent"):
        store.pending_attachments([attachment["id"]], "researcher", 3)


def test_inter_agent_messages_are_project_scoped_and_claimed(tmp_path):
    store = RuntimeConfigStore(tmp_path / "agent-messages.db")
    message = store.send_agent_message(
        "orchestrator", "researcher", "Investigate the API failure.", "command", project_id=7,
    )
    assert message["speaker"] == "agent"
    assert message["source_role"] == "orchestrator"
    assert message["message_kind"] == "command"
    assert message["delivery_status"] == "pending"
    assert store.history("researcher", project_id=1) == []

    claimed = store.claim_pending_agent_messages("researcher", project_id=7, delivery_run_id="run-1")
    assert [item["id"] for item in claimed] == [message["id"]]
    assert store.agent_inbox("researcher", project_id=7, include_delivered=False)[0]["delivery_status"] == "in_prompt"
    store.mark_agent_messages_delivered([message["id"]], "run-1")
    assert store.agent_inbox("researcher", project_id=7, include_delivered=False) == []
    assert store.agent_inbox("researcher", project_id=7)[0]["delivery_status"] == "delivered"


def test_unsent_commands_from_one_sender_are_compiled_into_one_chat_message(tmp_path):
    store = RuntimeConfigStore(tmp_path / "compiled-commands.db")
    first = store.send_agent_message("orchestrator", "programmer", "Implement the API.", "command")
    second = store.send_agent_message("orchestrator", "programmer", "Add integration tests.", "command")

    assert second["id"] == first["id"]
    assert second["compiled"] is True
    assert second["compiled_count"] == 2
    history = store.history("programmer")
    assert len(history) == 1
    assert history[0]["content"].startswith("COMPILED COMMAND REQUEST:")
    assert "<compiled_command_request>" not in history[0]["content"]
    assert "Implement the API." in history[0]["content"]
    assert "Add integration tests." in history[0]["content"]


def test_legacy_compiled_command_envelope_is_still_unwrapped(tmp_path):
    store = RuntimeConfigStore(tmp_path / "legacy-command-envelope.db")
    assert store.command_parts(
        "<compiled_command_request>\n- First task\n- Second task\n</compiled_command_request>"
    ) == ["First task", "Second task"]


def test_submitted_commands_are_not_replaced_by_later_commands(tmp_path):
    store = RuntimeConfigStore(tmp_path / "submitted-commands.db")
    first = store.send_agent_message("orchestrator", "programmer", "Implement the API.", "command")
    store.claim_pending_agent_messages("programmer", delivery_run_id="run-submitted")
    second = store.send_agent_message("orchestrator", "programmer", "Add integration tests.", "command")

    assert second["id"] != first["id"]
    assert [item["content"] for item in store.history("programmer")] == [
        "Implement the API.", "Add integration tests."
    ]


def test_reopen_compiles_pending_commands_left_by_an_older_process(tmp_path):
    db_path = tmp_path / "legacy-compiled-commands.db"
    store = RuntimeConfigStore(db_path)
    store.add_message(
        "programmer", "agent", "Implement the API.", "internal", "", source_role="orchestrator", message_kind="command"
    )
    store.add_message(
        "programmer", "agent", "Add integration tests.", "internal", "", source_role="orchestrator", message_kind="command"
    )
    reopened = RuntimeConfigStore(db_path)
    history = reopened.history("programmer")
    assert len(history) == 1
    assert "Add integration tests." in history[0]["content"]


def test_inter_agent_messages_can_be_released_after_a_failed_prompt(tmp_path):
    store = RuntimeConfigStore(tmp_path / "agent-message-release.db")
    message = store.send_agent_message("orchestrator", "reviewer", "Check this.", "report")
    store.claim_pending_agent_messages("reviewer", delivery_run_id="run-2")
    store.release_agent_messages([message["id"]], "run-2")
    assert store.agent_inbox("reviewer", include_delivered=False)[0]["delivery_status"] == "pending"


def test_restart_requeues_messages_claimed_by_an_interrupted_prompt(tmp_path):
    db_path = tmp_path / "agent-message-restart.db"
    store = RuntimeConfigStore(db_path)
    message = store.send_agent_message("orchestrator", "reviewer", "Do this next.", "command")
    store.claim_pending_agent_messages("reviewer", delivery_run_id="run-3")
    reopened = RuntimeConfigStore(db_path)
    inbox = reopened.agent_inbox("reviewer", include_delivered=False)
    assert inbox[0]["id"] == message["id"]
    assert inbox[0]["delivery_status"] == "pending"


def test_chat_runs_are_durable_and_recover_interrupted_work(tmp_path):
    db_path = tmp_path / "runtime.db"
    store = RuntimeConfigStore(db_path)
    run = store.create_chat_run("researcher", 3)
    assert store.active_chat_run("researcher", 3)["id"] == run["id"]

    store.update_chat_run(run["id"], "running")
    # Reopening the app marks a provider task that could not survive a process
    # restart as a visible error instead of leaving the browser polling forever.
    reopened = RuntimeConfigStore(db_path)
    recovered = reopened.chat_run(run["id"])
    assert recovered["status"] == "error"
    assert "server restarted" in recovered["error"].lower()
    assert reopened.active_chat_run("researcher", 3) is None

    completed = reopened.create_chat_run("researcher", 3)
    reopened.update_chat_run(completed["id"], "completed", {"ok": True, "response": "done"})
    assert reopened.chat_run(completed["id"])["result"]["response"] == "done"


def test_pending_agent_recipients_and_latest_runs_are_project_scoped(tmp_path):
    store = RuntimeConfigStore(tmp_path / "dispatcher.db")
    store.send_agent_message("orchestrator", "researcher", "Investigate this.", "command", project_id=4)
    store.send_agent_message("orchestrator", "reviewer", "Review this.", "report", project_id=5)

    assert store.pending_agent_recipients() == [
        {"project_id": 4, "role": "researcher", "first_message_id": 1},
        {"project_id": 5, "role": "reviewer", "first_message_id": 2},
    ]
    assert store.latest_chat_run("researcher", 4) is None
    run = store.create_chat_run("researcher", 4)
    store.update_chat_run(run["id"], "completed", {"ok": True})
    assert store.latest_chat_run("researcher", 4)["result"] == {"ok": True}


def test_queued_chat_requests_keep_their_prompt_until_the_agent_is_free(tmp_path):
    db_path = tmp_path / "queued-chat.db"
    store = RuntimeConfigStore(db_path)
    first = store.create_chat_run("researcher", 1, {"message": "first", "project_id": 1})
    second = store.create_chat_run("researcher", 1, {"message": "second", "project_id": 1})

    assert [item["request"]["message"] for item in store.queued_chat_runs()] == ["first", "second"]
    assert store.active_chat_run("researcher", 1)["id"] == first["id"]
    running = store.claim_queued_chat_run(first["id"])
    assert running["status"] == "running"
    assert store.active_chat_run("researcher", 1)["id"] == first["id"]

    reopened = RuntimeConfigStore(db_path)
    assert reopened.chat_run(second["id"])["request"]["message"] == "second"
    assert reopened.chat_run(first["id"])["status"] == "error"
