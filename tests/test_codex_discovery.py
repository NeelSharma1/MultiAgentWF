import os
from pathlib import Path

import team


def test_resolve_codex_command_honors_configured_path(tmp_path, monkeypatch):
    command = tmp_path / "codex"
    command.write_text("#!/bin/sh\n")
    command.chmod(0o755)
    monkeypatch.setenv("CODEX_COMMAND", str(command))

    assert team.resolve_codex_command() == str(command.resolve())


def test_resolve_codex_command_uses_jetbrains_runtime(tmp_path, monkeypatch):
    command = (
        tmp_path / "Library/Caches/JetBrains/PyCharm2026.2/acp-agents/.runtimes/"
        "node/24/npm-cache/_npx/install/node_modules/.bin/codex"
    )
    command.parent.mkdir(parents=True)
    command.write_text("#!/bin/sh\n")
    command.chmod(0o755)
    monkeypatch.delenv("CODEX_COMMAND", raising=False)
    monkeypatch.setattr(team.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(team.shutil, "which", lambda *args, **kwargs: None)
    monkeypatch.setattr(team.subprocess, "run", lambda *args, **kwargs: None)

    assert team.resolve_codex_command() == str(command.resolve())
