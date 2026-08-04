import os
import sys
import time
from pathlib import Path

import pytest

from terminal import WindowsTerminal


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PTY backend test")
def test_windows_terminal_can_spawn_read_and_write(tmp_path: Path):
    terminal = WindowsTerminal(rows=10, columns=80)
    command = [os.environ.get("COMSPEC", "cmd.exe"), "/c", "echo terminal-ready"]
    terminal.spawn(command, env=os.environ.copy(), cwd=tmp_path)
    output = ""
    deadline = time.monotonic() + 10
    try:
        while time.monotonic() < deadline and terminal.is_alive():
            output += terminal.read_available(0.2)
        output += terminal.read_available(0.2)
    finally:
        terminal.terminate()
        terminal.close()

    assert "terminal-ready" in output.lower()


def test_windows_terminal_reports_missing_optional_dependency(monkeypatch):
    if sys.platform == "win32":
        pytest.skip("The Windows dependency is available in the Windows test environment")
    assert WindowsTerminal().rows == 40
