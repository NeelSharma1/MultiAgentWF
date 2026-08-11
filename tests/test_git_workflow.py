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
    assert overview["commits"]
    assert any(item["agent_commit"] and item["subject"] == "agent(programmer): Create graph" for item in overview["commits"])
    assert overview["commits_truncated"] is False

    assert workflow.checkout_branch(1, repository, "review")["branch"] == "review"
    with pytest.raises(GitWorkflowError, match="configured main branch"):
        workflow.delete_branch(1, repository, "team-main")
    assert workflow.checkout_branch(1, repository, "team-main")["branch"] == "team-main"
    assert workflow.delete_branch(1, repository, "review") == {"deleted": "review"}


def test_commit_inspection_rebase_merge_and_revert_for_regular_commits(tmp_path):
    workflow, repository, _ = _workflow(tmp_path)

    workflow.begin_agent_run(1, "programmer", repository)
    (repository / "tracked.py").write_text("tracked = True\n", encoding="utf-8")
    tracked = workflow.finish_agent_run(1, "programmer", "run-detail", repository, "Add tracked file")
    assert tracked is not None
    detail = workflow.commit_detail(1, repository, tracked["commit_hash"])
    assert detail["hash"] == tracked["commit_hash"]
    assert detail["files"][0]["path"] == "tracked.py"
    assert "+tracked = True" in workflow.file_diff(1, repository, tracked["commit_hash"], "tracked.py")["diff"]

    workflow.create_branch(1, repository, "topic", "team-main")
    workflow.checkout_branch(1, repository, "topic")
    (repository / "topic.py").write_text("topic = True\n", encoding="utf-8")
    _git(workflow, repository, "add", "topic.py")
    _git(workflow, repository, "commit", "-m", "Topic change")
    topic_commit = _git(workflow, repository, "rev-parse", "HEAD")

    workflow.checkout_branch(1, repository, "team-main")
    (repository / "main.py").write_text("main = True\n", encoding="utf-8")
    _git(workflow, repository, "add", "main.py")
    _git(workflow, repository, "commit", "-m", "Main change")
    main_commit = _git(workflow, repository, "rev-parse", "HEAD")

    rebased = workflow.rebase(1, repository, main_commit, "topic")
    assert rebased["rebased"] == "topic"
    assert _git(workflow, repository, "branch", "--show-current") == "team-main"
    rebased_topic = _git(workflow, repository, "rev-parse", "refs/heads/topic")
    assert _git(workflow, repository, "merge-base", "--is-ancestor", main_commit, rebased_topic) == ""

    merged = workflow.merge(1, repository, rebased_topic, "team-main")
    assert merged["target_branch"] == "team-main"
    assert _git(workflow, repository, "branch", "--show-current") == "team-main"
    assert (repository / "topic.py").is_file()
    merge_detail = workflow.commit_detail(1, repository, merged["head"])
    assert any(item["path"] == "topic.py" for item in merge_detail["files"])
    assert "+topic = True" in workflow.file_diff(1, repository, merged["head"], "topic.py")["diff"]

    reverted = workflow.revert(1, repository, rebased_topic)
    assert reverted["reverted"] == rebased_topic
    assert not (repository / "topic.py").exists()


def test_merge_commit_into_all_branches_skips_branches_that_already_contain_it(tmp_path):
    workflow, repository, _ = _workflow(tmp_path)
    workflow.begin_agent_run(1, "programmer", repository)
    (repository / "base.py").write_text("base = True\n", encoding="utf-8")
    assert workflow.finish_agent_run(1, "programmer", "run-base", repository, "Create base")

    workflow.create_branch(1, repository, "topic", "team-main")
    workflow.checkout_branch(1, repository, "topic")
    (repository / "topic.py").write_text("topic = True\n", encoding="utf-8")
    _git(workflow, repository, "add", "topic.py")
    _git(workflow, repository, "commit", "-m", "Topic change")
    topic_commit = _git(workflow, repository, "rev-parse", "HEAD")
    workflow.checkout_branch(1, repository, "team-main")
    (repository / "base.py").write_text("keep my local edit\n", encoding="utf-8")

    result = workflow.merge_into_all_branches(1, repository, topic_commit)

    assert set(result["merged"]) == {"team-main", "programmer"}
    assert result["skipped"] == ["topic"]
    assert result["failed"] == []
    assert _git(workflow, repository, "branch", "--show-current") == "team-main"
    assert (repository / "base.py").read_text(encoding="utf-8") == "keep my local edit\n"
    for branch in ("team-main", "programmer"):
        assert _git(workflow, repository, "merge-base", "--is-ancestor", topic_commit, f"refs/heads/{branch}") == ""

    repeat = workflow.merge_into_all_branches(1, repository, topic_commit)
    assert repeat["merged"] == []
    assert set(repeat["skipped"]) == {"team-main", "programmer", "topic"}


def test_consolidate_branches_integrates_divergent_heads_into_main_and_retains_refs(tmp_path):
    workflow, repository, _ = _workflow(tmp_path)
    workflow.begin_agent_run(1, "programmer", repository)
    (repository / "base.py").write_text("base = True\n", encoding="utf-8")
    assert workflow.finish_agent_run(1, "programmer", "run-base", repository, "Create base")

    workflow.create_branch(1, repository, "topic", "team-main")
    workflow.checkout_branch(1, repository, "topic")
    (repository / "topic.py").write_text("topic = True\n", encoding="utf-8")
    _git(workflow, repository, "add", "topic.py")
    _git(workflow, repository, "commit", "-m", "Topic change")
    topic_commit = _git(workflow, repository, "rev-parse", "HEAD")
    workflow.checkout_branch(1, repository, "team-main")
    (repository / "base.py").write_text("keep my local edit\n", encoding="utf-8")

    result = workflow.consolidate_branches(1, repository)

    assert result["main_branch"] == "team-main"
    assert result["merged"] == ["topic"]
    assert "programmer" in result["skipped"]
    assert result["failed"] == []
    assert result["consolidated"] is True
    assert _git(workflow, repository, "merge-base", "--is-ancestor", topic_commit, "refs/heads/team-main") == ""
    assert _git(workflow, repository, "branch", "--show-current") == "team-main"
    assert (repository / "base.py").read_text(encoding="utf-8") == "keep my local edit\n"
    overview = workflow.overview(1, repository, [{"role": "programmer", "name": "Programmer"}])
    branches = {item["name"]: item for item in overview["branches"]}
    assert branches["topic"]["merged_into_main"] is True
    assert branches["programmer"]["merged_into_main"] is True
