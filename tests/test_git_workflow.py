from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from git_workflow import GitWorkflowStore


pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")


def _git(store: GitWorkflowStore, repository: Path, *args: str) -> str:
    return store._run(repository, *args).stdout.strip()


def _workflow(tmp_path):
    repository = tmp_path / "workspace"
    repository.mkdir()
    workflow = GitWorkflowStore(tmp_path / "data" / "workspace.db")
    workflow.configure(1, repository, "team-main", initialize=True)
    _git(workflow, repository, "config", "user.name", "Neel Test")
    _git(workflow, repository, "config", "user.email", "neel@example.test")
    workflow.set_agent_enabled(1, "programmer", True, repository)
    return workflow, repository


def _change(workflow: GitWorkflowStore, repository: Path, path: str, content: str, message: str):
    run = workflow.begin_agent_run(1, "programmer", repository, f"run-{path}")
    target = Path(run["workspace"]) / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return workflow.finish_agent_run(1, "programmer", f"run-{path}", repository, message, run["change_id"])


def test_agent_uses_isolated_workspace_and_commits_on_current_branch(tmp_path):
    workflow, repository = _workflow(tmp_path)
    run = workflow.begin_agent_run(1, "programmer", repository, "run-1")

    assert run["branch"] == "team-main"
    assert _git(workflow, repository, "branch", "--show-current") == "team-main"
    assert not (repository / "feature.py").exists()
    (Path(run["workspace"]) / "feature.py").write_text("print('hello')\n", encoding="utf-8")
    commit = workflow.finish_agent_run(1, "programmer", "run-1", repository, "Add feature", run["change_id"])

    assert commit and not commit.get("held")
    assert commit["message"] == "Add feature"
    assert commit["agent_branch"] == ""
    assert (repository / "feature.py").read_text(encoding="utf-8") == "print('hello')\n"
    assert _git(workflow, repository, "log", "-1", "--format=%an <%ae>") == "Neel Test <neel@example.test>"
    assert not Path(run["workspace"]).exists()


def test_unrelated_local_edits_are_not_staged(tmp_path):
    workflow, repository = _workflow(tmp_path)
    (repository / "mine.txt").write_text("keep me local\n", encoding="utf-8")
    commit = _change(workflow, repository, "agent.txt", "agent work\n", "Add agent work")

    assert commit and not commit.get("held")
    assert "mine.txt" in _git(workflow, repository, "status", "--porcelain")
    assert "mine.txt" not in _git(workflow, repository, "show", "--format=", "--name-only", "HEAD")


def test_overlapping_local_edit_holds_patch_without_overwrite(tmp_path):
    workflow, repository = _workflow(tmp_path)
    run = workflow.begin_agent_run(1, "programmer", repository, "run-overlap")
    (Path(run["workspace"]) / "same.txt").write_text("agent\n", encoding="utf-8")
    (repository / "same.txt").write_text("mine\n", encoding="utf-8")

    held = workflow.finish_agent_run(1, "programmer", "run-overlap", repository, "Change same", run["change_id"])
    assert held and held["held"] is True
    assert (repository / "same.txt").read_text(encoding="utf-8") == "mine\n"
    detail = workflow.change_detail(1, held["change_id"])
    assert detail["state"] == "held_conflict"
    assert "same.txt" in detail["diff"]


def test_pushes_current_branch_not_role_branch(tmp_path):
    workflow, repository = _workflow(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    workflow.configure(1, repository, "team-main", remote="gh", remote_url=str(remote))

    commit = _change(workflow, repository, "pushed.txt", "remote\n", "Add remote work")
    assert commit and commit.get("pushed")
    refs = subprocess.run(["git", "--git-dir", str(remote), "for-each-ref", "--format=%(refname)"], check=True,
                          capture_output=True, text=True).stdout
    assert "refs/heads/team-main" in refs
    assert "refs/heads/programmer" not in refs


def test_activity_overview_separates_local_and_agent_work(tmp_path):
    workflow, repository = _workflow(tmp_path)
    committed = _change(workflow, repository, "agent.txt", "done\n", "Add work")
    (repository / "my-commit.txt").write_text("committed locally\n", encoding="utf-8")
    _git(workflow, repository, "add", "my-commit.txt")
    _git(workflow, repository, "commit", "-m", "Save local work")
    local_hash = _git(workflow, repository, "rev-parse", "HEAD")
    (repository / "local.txt").write_text("still local\n", encoding="utf-8")
    overview = workflow.overview(1, repository, [{"role": "programmer", "name": "Programmer"}])

    assert committed and overview["agents"][0]["mode"] == "shared-current-branch"
    assert any(item["path"] == "local.txt" for item in overview["working_changes"])
    assert any(item["commit_hash"] == committed["commit_hash"] for item in overview["changes"])
    agent_change = next(item for item in overview["changes"] if item["commit_hash"] == committed["commit_hash"])
    assert agent_change["commit"] == {
        "hash": committed["commit_hash"], "short_hash": committed["commit_hash"][:7],
        "subject": "Add work", "author": "Neel Test", "date": agent_change["commit"]["date"],
    }
    local_commit = next(item for item in overview["local_commits"] if item["hash"] == local_hash)
    assert local_commit["short_hash"] == local_hash[:7]
    assert local_commit["subject"] == "Save local work"
    assert local_commit["files"] == [{
        "path": "my-commit.txt", "previous_path": "", "status": "A", "additions": 1, "deletions": 0,
    }]


def test_discard_held_patch_and_normal_branch_tools_remain_available(tmp_path):
    workflow, repository = _workflow(tmp_path)
    run = workflow.begin_agent_run(1, "programmer", repository, "run-held")
    (Path(run["workspace"]) / "same.txt").write_text("agent\n", encoding="utf-8")
    (repository / "same.txt").write_text("local\n", encoding="utf-8")
    held = workflow.finish_agent_run(1, "programmer", "run-held", repository, "Conflict", run["change_id"])
    assert held and held["held"]
    assert workflow.discard_change(1, held["change_id"])["state"] == "discarded"
    (repository / "same.txt").unlink()
    workflow.create_branch(1, repository, "topic", "team-main")
    assert workflow.checkout_branch(1, repository, "topic")["branch"] == "topic"
    assert workflow.checkout_branch(1, repository, "team-main")["branch"] == "team-main"
    assert workflow.delete_branch(1, repository, "topic") == {"deleted": "topic"}


def test_revert_remains_available_for_agent_commit(tmp_path):
    workflow, repository = _workflow(tmp_path)
    committed = _change(workflow, repository, "agent.txt", "done\n", "Add work")
    assert committed
    reverted = workflow.revert(1, repository, committed["commit_hash"])
    assert reverted["reverted"] == committed["commit_hash"]
    assert not (repository / "agent.txt").exists()
