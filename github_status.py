from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any


class GitReportError(RuntimeError):
    """A repository report could not be collected from the selected project path."""


def _run_git(path: Path, *arguments: str, tolerate_no_commits: bool = False) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), *arguments],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitReportError(f"Could not run git: {exc}") from exc
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        if tolerate_no_commits and "does not have any commits" in detail:
            return ""
        raise GitReportError(detail or f"git {' '.join(arguments)} failed")
    return completed.stdout.strip()


def _safe_remote(value: str) -> str:
    # Do not put an accidentally embedded credential into the chat transcript.
    return re.sub(r"(://)([^/@\s]+)@", r"\1***@", value)


def collect_git_report(project_path: str | Path) -> dict[str, Any]:
    path = Path(project_path).expanduser()
    if not path.is_dir():
        raise GitReportError(f"Project folder does not exist: {path}")

    top_level = _run_git(path, "rev-parse", "--show-toplevel")
    branch = _run_git(path, "branch", "--show-current") or "(detached HEAD)"
    head = _run_git(path, "log", "-1", "--oneline", "--decorate", tolerate_no_commits=True) or "(no commits yet)"
    status = _run_git(path, "status", "--short", "--branch") or "(clean)"
    staged = _run_git(path, "diff", "--cached", "--stat") or "(none)"
    unstaged = _run_git(path, "diff", "--stat") or "(none)"
    recent = _run_git(path, "log", "-5", "--oneline", "--decorate", tolerate_no_commits=True) or "(no commits yet)"
    remotes = _run_git(path, "remote", "-v") or "(none)"
    remotes = "\n".join(_safe_remote(line) for line in remotes.splitlines())
    return {
        "path": str(path),
        "repository": top_level,
        "branch": branch,
        "head": head,
        "status": status,
        "staged": staged,
        "unstaged": unstaged,
        "recent_commits": recent,
        "remotes": remotes,
    }


def format_git_report(report: dict[str, Any]) -> str:
    return "\n".join(
        (
            "GitHub / repository status",
            f"Path: {report['path']}",
            f"Repository: {report['repository']}",
            f"Branch: {report['branch']}",
            f"HEAD: {report['head']}",
            "",
            "Working tree:",
            report["status"],
            "",
            "Staged changes:",
            report["staged"],
            "",
            "Unstaged changes:",
            report["unstaged"],
            "",
            "Recent commits:",
            report["recent_commits"],
            "",
            "Remotes:",
            report["remotes"],
        )
    )
