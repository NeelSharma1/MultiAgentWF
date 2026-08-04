from __future__ import annotations

import shutil
import subprocess

import pytest

from git_workflow import GitWorkflowError, GitWorkflowStore


pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")


def _git(store: GitWorkflowStore, repository, *args: str) -> str:
    return store._run(repository, *args).stdout.strip()


def _workflow(tmp_path):
    repository = tmp_path / "workspace"
    repository.mkdir()
    workflow = GitWorkflowStore(tmp_path / "data" / "workspace.db")
    status = workflow.configure(1, repository, "team-main", initialize=True)
    _git(workflow, repository, "config", "user.name", "Agent Team Test")
    _git(workflow, repository, "config", "user.email", "agent-team@example.test")
    workflow.set_agent_enabled(1, "programmer", True, repository)
    return workflow, repository, status


def test_agent_run_commits_on_its_role_branch_and_merges_to_main(tmp_path):
    workflow, repository, status = _workflow(tmp_path)

    assert status["is_repository"] is True
    assert status["main_branch"] == "team-main"

    run = workflow.begin_agent_run(1, "programmer", repository)
    assert run["branch"] == "programmer"
    assert _git(workflow, repository, "branch", "--show-current") == "programmer"
    (repository / "feature.py").write_text("print('hello')\n", encoding="utf-8")
    commit = workflow.finish_agent_run(1, "programmer", "run-1", repository, "Add the first feature")

    assert commit is not None
    assert commit["agent_branch"] == "programmer"
    assert commit["merge_hash"]
    assert commit["message"] == "agent(programmer): Add the first feature"
    assert commit["files"] == [{
        "path": "feature.py", "previous_path": "", "status": "A", "additions": 1, "deletions": 0,
    }]
    assert _git(workflow, repository, "branch", "--show-current") == "team-main"
    assert _git(workflow, repository, "rev-parse", "HEAD") == commit["merge_hash"]
    assert _git(workflow, repository, "merge-base", "--is-ancestor", commit["commit_hash"], "team-main") == ""
    diff = workflow.file_diff(1, repository, commit["commit_hash"], "feature.py")
    assert "+print('hello')" in diff["diff"]
    assert workflow.status(1, repository)["clean"] is True


def test_revert_and_head_only_rollback_operate_on_the_main_merge(tmp_path):
    workflow, repository, _ = _workflow(tmp_path)
    workflow.begin_agent_run(1, "programmer", repository)
    changed = repository / "feature.py"
    changed.write_text("one\n", encoding="utf-8")
    first = workflow.finish_agent_run(1, "programmer", "run-1", repository, "Create feature")
    assert first is not None

    workflow.begin_agent_run(1, "programmer", repository)
    changed.write_text("two\n", encoding="utf-8")
    second = workflow.finish_agent_run(1, "programmer", "run-2", repository, "Update feature")
    assert second is not None

    with pytest.raises(GitWorkflowError, match="current HEAD"):
        workflow.rollback(1, repository, first["commit_hash"])

    rolled_back = workflow.rollback(1, repository, second["commit_hash"])
    assert rolled_back["head"] == first["merge_hash"]
    assert changed.read_text(encoding="utf-8") == "one\n"

    reverted = workflow.revert(1, repository, first["commit_hash"])
    assert reverted["reverted"] == first["commit_hash"]
    assert not changed.exists()
    assert workflow.commit(1, first["commit_hash"])["state"] == "reverted"


def test_configure_adds_remote_url_and_pushes_main_and_agent_branches(tmp_path):
    workflow, repository, _ = _workflow(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)

    workflow.configure(1, repository, "team-main", remote="main", remote_url=str(remote))
    assert _git(workflow, repository, "remote", "get-url", "main") == str(remote)
    workflow.configure(1, repository, "team-main", remote="gh")
    assert _git(workflow, repository, "remote", "get-url", "gh") == str(remote)
    assert "main" not in _git(workflow, repository, "remote").splitlines()

    workflow.begin_agent_run(1, "programmer", repository)
    (repository / "pushed.txt").write_text("remote\n", encoding="utf-8")
    commit = workflow.finish_agent_run(1, "programmer", "run-remote", repository, "Push change")
    assert commit is not None

    pushed = workflow.push(1, repository, commit["commit_hash"])
    assert pushed["remote"] == "gh"
    assert pushed["main_branch"] == "team-main"
    remote_refs = subprocess.run(
        ["git", "--git-dir", str(remote), "for-each-ref", "--format=%(refname)"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert "refs/heads/team-main" in remote_refs
    assert "refs/heads/programmer" in remote_refs


def test_main_branch_is_unambiguous_when_a_tag_has_the_same_name(tmp_path):
    workflow, repository, _ = _workflow(tmp_path)
    workflow.begin_agent_run(1, "programmer", repository)
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    first = workflow.finish_agent_run(1, "programmer", "run-seed", repository, "Seed main")
    assert first is not None
    _git(workflow, repository, "tag", "team-main")
    _git(workflow, repository, "checkout", "programmer")

    run = workflow.begin_agent_run(1, "programmer", repository)

    assert run["branch"] == "programmer"
    assert _git(workflow, repository, "branch", "--show-current") == "programmer"


def test_version_control_overview_and_branch_management(tmp_path):
    workflow, repository, _ = _workflow(tmp_path)
    workflow.begin_agent_run(1, "programmer", repository)
    (repository / "topology.txt").write_text("graph\n", encoding="utf-8")
    assert workflow.finish_agent_run(1, "programmer", "run-graph", repository, "Create graph")

    created = workflow.create_branch(1, repository, "review", "team-main")
    assert created == {"branch": "review", "source": "team-main"}
    overview = workflow.overview(1, repository, [{"role": "programmer", "name": "Programmer"}])

    assert {item["name"] for item in overview["branches"]} >= {"team-main", "programmer", "review"}
    assert overview["worktrees"][0]["primary"] is True
    assert overview["agents"] == [{
        "role": "programmer", "name": "Programmer", "enabled": True, "branch": "programmer",
        "branch_exists": True, "merged_into_main": True,
    }]

    assert workflow.checkout_branch(1, repository, "review")["branch"] == "review"
    with pytest.raises(GitWorkflowError, match="configured main branch"):
        workflow.delete_branch(1, repository, "team-main")
    assert workflow.checkout_branch(1, repository, "team-main")["branch"] == "team-main"
    assert workflow.delete_branch(1, repository, "review") == {"deleted": "review"}
