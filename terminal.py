"""Cross-platform pseudo-terminal sessions for the Codex TUI."""

from __future__ import annotations

import os
import select
import subprocess
from pathlib import Path
from typing import Mapping, Sequence


class TerminalSession:
    """Small platform-neutral interface used by the Codex TUI runner."""

    def spawn(self, args: Sequence[str], env: Mapping[str, str], cwd: Path) -> None:
        raise NotImplementedError

    def read_available(self, timeout: float) -> str:
        raise NotImplementedError

    def write(self, data: bytes) -> None:
        raise NotImplementedError

    def is_alive(self) -> bool:
        raise NotImplementedError

    def terminate(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class PosixTerminal(TerminalSession):
    """PTY implementation for Linux and macOS."""

    def __init__(self, rows: int = 40, columns: int = 120) -> None:
        self.rows = rows
        self.columns = columns
        self.master_fd: int | None = None
        self.process: subprocess.Popen[bytes] | None = None

    def spawn(self, args: Sequence[str], env: Mapping[str, str], cwd: Path) -> None:
        import fcntl
        import pty
        import struct
        import termios

        master_fd, slave_fd = pty.openpty()
        try:
            fcntl.ioctl(
                slave_fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", self.rows, self.columns, 0, 0),
            )
            self.process = subprocess.Popen(
                list(args),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=dict(env),
                cwd=str(cwd),
                start_new_session=True,
                close_fds=True,
            )
            self.master_fd = master_fd
        except BaseException:
            os.close(master_fd)
            raise
        finally:
            os.close(slave_fd)

    def read_available(self, timeout: float) -> str:
        if self.master_fd is None:
            return ""
        readable, _, _ = select.select([self.master_fd], [], [], timeout)
        if not readable:
            return ""
        try:
            return os.read(self.master_fd, 65_536).decode(errors="replace")
        except (OSError, ValueError):
            return ""

    def write(self, data: bytes) -> None:
        if self.master_fd is None:
            raise RuntimeError("Terminal has not been spawned")
        os.write(self.master_fd, data)

    def is_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def terminate(self) -> None:
        if not self.is_alive() or self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)

    def close(self) -> None:
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None


class WindowsTerminal(TerminalSession):
    """ConPTY/winpty implementation for Windows via pywinpty."""

    def __init__(self, rows: int = 40, columns: int = 120) -> None:
        self.rows = rows
        self.columns = columns
        self.process = None

    def spawn(self, args: Sequence[str], env: Mapping[str, str], cwd: Path) -> None:
        try:
            from winpty import PtyProcess
        except ImportError as exc:
            raise RuntimeError(
                "WindowsTerminal requires pywinpty. Install it with `pip install pywinpty`."
            ) from exc

        self.process = PtyProcess.spawn(
            list(args),
            cwd=str(cwd),
            env=dict(env),
            dimensions=(self.rows, self.columns),
        )

    def read_available(self, timeout: float) -> str:
        if self.process is None or not self.is_alive():
            return ""
        readable, _, _ = select.select([self.process.fileno()], [], [], timeout)
        if not readable:
            return ""
        try:
            return self.process.read(65_536)
        except (EOFError, OSError, ValueError):
            return ""

    def write(self, data: bytes) -> None:
        if self.process is None:
            raise RuntimeError("Terminal has not been spawned")
        self.process.write(data.decode(errors="replace"))

    def is_alive(self) -> bool:
        return self.process is not None and self.process.isalive()

    def terminate(self) -> None:
        if self.process is not None and self.is_alive():
            self.process.terminate(force=True)

    def close(self) -> None:
        if self.process is not None:
            try:
                self.process.close(force=True)
            except (OSError, IOError):
                pass
            self.process = None


def create_terminal(rows: int = 40, columns: int = 120) -> TerminalSession:
    """Create the correct PTY backend for the current operating system."""

    if os.name == "nt":
        return WindowsTerminal(rows=rows, columns=columns)
    return PosixTerminal(rows=rows, columns=columns)
