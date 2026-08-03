import subprocess

from github_status import collect_git_report, format_git_report


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)


def test_collect_git_report_includes_worktree_state_and_redacts_remote_credentials(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "notes.txt").write_text("untracked")
    git(tmp_path, "remote", "add", "origin", "https://alice:secret@example.com/team/repo.git")

    report = collect_git_report(tmp_path)
    rendered = format_git_report(report)

    assert report["repository"] == str(tmp_path)
    assert "notes.txt" in report["status"]
    assert "alice:secret" not in rendered
    assert "***@example.com" in rendered
    assert "Staged changes:" in rendered
    assert "Unstaged changes:" in rendered
