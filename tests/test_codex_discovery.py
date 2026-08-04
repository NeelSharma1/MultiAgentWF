import os
import subprocess
import sys
from pathlib import Path

import pytest

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


@pytest.mark.skipif(sys.platform != "win32", reason="Windows launcher shim test")
def test_windows_codex_cmd_shim_can_be_invoked(tmp_path):
    command = tmp_path / "codex.cmd"
    command.write_text("@echo off\necho codex-shim %*\n", encoding="utf-8")

    completed = subprocess.run(
        team.codex_process_args([str(command), "login", "status"]),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert "codex-shim login status" in completed.stdout.lower()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native Codex discovery test")
def test_windows_codex_shim_prefers_adjacent_native_binary(tmp_path, monkeypatch):
    node_modules = tmp_path / "node_modules"
    shim = node_modules / ".bin" / "codex.cmd"
    native = node_modules / "@openai" / "codex-win32-x64" / "vendor" / "x86_64-pc-windows-msvc" / "bin" / "codex.exe"
    shim.parent.mkdir(parents=True)
    native.parent.mkdir(parents=True)
    shim.write_text("@echo off\n", encoding="utf-8")
    native.write_bytes(b"native-test")
    monkeypatch.setenv("CODEX_COMMAND", str(shim))

    assert team.resolve_codex_command() == str(native.resolve())


def test_codex_process_env_uses_the_user_profile_for_chatgpt_auth(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.setattr(team.Path, "home", classmethod(lambda cls: tmp_path))

    env = team.codex_process_env()

    assert env["HOME"] == str(tmp_path)
    assert env["USERPROFILE"] == str(tmp_path)
    assert env["CODEX_HOME"] == str(tmp_path / ".codex")


def test_codex_auth_failure_detects_missing_bearer_message():
    assert team._codex_auth_failure(
        "unexpected status 401 Unauthorized: Missing bearer or basic authentication in header"
    )
    assert not team._codex_auth_failure("unexpected status 500 Internal Server Error")
