from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Iterator


BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,119}$")
REMOTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
MAX_DIFF_BYTES = 300_000


class GitWorkflowError(RuntimeError):
    """A configured shared Git workflow could not complete safely."""


def _safe_remote(value: str) -> str:
    return re.sub(r"(://)([^/@\s]+)@", r"\1***@", value)


def _relative_git_path(value: str) -> str:
    path = PurePosixPath(str(value or "").replace("\\", "/"))
    if not path.parts or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise GitWorkflowError("File path must be a repository-relative path")
    return path.as_posix()


class GitWorkflowStore:
    """Shared-branch Git collaboration with isolated agent worktrees."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self.artifact_root = Path(self.db_path).parent / "git-diffs"
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self._apply_locks: dict[int, threading.Lock] = {}
        self._runs: dict[str, dict[str, Any]] = {}
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS project_git_workflows (
                project_id INTEGER PRIMARY KEY, repository TEXT NOT NULL, branch TEXT NOT NULL,
                main_branch TEXT NOT NULL DEFAULT '',
                remote TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS project_agent_git_settings (
                project_id INTEGER NOT NULL, role TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(project_id, role)
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS agent_git_commits (
                id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL, role TEXT NOT NULL,
                run_id TEXT NOT NULL DEFAULT '', commit_hash TEXT NOT NULL UNIQUE, parent_hash TEXT NOT NULL DEFAULT '',
                merge_hash TEXT NOT NULL DEFAULT '', main_parent_hash TEXT NOT NULL DEFAULT '', agent_branch TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL, files_json TEXT NOT NULL DEFAULT '[]', state TEXT NOT NULL DEFAULT 'committed',
                pushed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS git_change_sets (
                id TEXT PRIMARY KEY, project_id INTEGER NOT NULL, role TEXT NOT NULL,
                run_id TEXT NOT NULL DEFAULT '', target_branch TEXT NOT NULL, base_commit TEXT NOT NULL,
                patch_path TEXT NOT NULL DEFAULT '', files_json TEXT NOT NULL DEFAULT '[]',
                state TEXT NOT NULL DEFAULT 'running', review_status TEXT NOT NULL DEFAULT 'not_requested',
                review_role TEXT NOT NULL DEFAULT '', review_detail TEXT NOT NULL DEFAULT '',
                commit_hash TEXT NOT NULL DEFAULT '', push_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS git_change_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, change_id TEXT NOT NULL, project_id INTEGER NOT NULL,
                origin TEXT NOT NULL, event_type TEXT NOT NULL, title TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_git_change_sets_project ON git_change_sets(project_id, created_at DESC)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_git_change_events_change ON git_change_events(change_id, id)")
            workflow_columns = {row[1] for row in db.execute("PRAGMA table_info(project_git_workflows)")}
            if "main_branch" not in workflow_columns:
                db.execute("ALTER TABLE project_git_workflows ADD COLUMN main_branch TEXT NOT NULL DEFAULT ''")
            db.execute("UPDATE project_git_workflows SET main_branch=branch WHERE main_branch='' OR main_branch IS NULL")
            commit_columns = {row[1] for row in db.execute("PRAGMA table_info(agent_git_commits)")}
            if "state" not in commit_columns:
                db.execute("ALTER TABLE agent_git_commits ADD COLUMN state TEXT NOT NULL DEFAULT 'committed'")
            if "pushed" not in commit_columns:
                db.execute("ALTER TABLE agent_git_commits ADD COLUMN pushed INTEGER NOT NULL DEFAULT 0")
            if "merge_hash" not in commit_columns:
                db.execute("ALTER TABLE agent_git_commits ADD COLUMN merge_hash TEXT NOT NULL DEFAULT ''")
            if "main_parent_hash" not in commit_columns:
                db.execute("ALTER TABLE agent_git_commits ADD COLUMN main_parent_hash TEXT NOT NULL DEFAULT ''")
            if "agent_branch" not in commit_columns:
                db.execute("ALTER TABLE agent_git_commits ADD COLUMN agent_branch TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _run(repository: Path, *args: str, timeout: int = 30, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(
                ["git", "-C", str(repository), *args], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise GitWorkflowError(f"Could not run git: {exc}") from exc
        if check and completed.returncode:
            detail = (completed.stderr or completed.stdout).strip()
            raise GitWorkflowError(detail or f"git {' '.join(args)} failed")
        return completed

    @classmethod
    def _repository(cls, project_root: Path) -> Path | None:
        root = Path(project_root).expanduser().resolve()
        if not root.is_dir():
            raise GitWorkflowError(f"Project folder does not exist: {root}")
        result = cls._run(root, "rev-parse", "--show-toplevel", check=False)
        if result.returncode:
            return None
        return Path(result.stdout.strip()).resolve()

    @staticmethod
    def _validate_branch(repository: Path, branch: str) -> str:
        normalized = str(branch or "").strip()
        if not BRANCH_RE.fullmatch(normalized) or ".." in normalized or normalized.endswith((".", "/")):
            raise GitWorkflowError("Branch names may contain letters, numbers, '.', '_', '-', and '/' only")
        GitWorkflowStore._run(repository, "check-ref-format", "--branch", normalized)
        return normalized

    def configuration(self, project_id: int) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM project_git_workflows WHERE project_id=?", (project_id,)).fetchone()
        return dict(row) if row else None

    def agent_enabled(self, project_id: int, role: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT enabled FROM project_agent_git_settings WHERE project_id=? AND role=?",
                (project_id, role),
            ).fetchone()
        return bool(row and row["enabled"])

    def _main_branch(self, configuration: dict[str, Any]) -> str:
        return str(configuration.get("main_branch") or configuration.get("branch") or "")

    def _agent_branch(self, repository: Path, role: str) -> str:
        return self._validate_branch(repository, role)

    def set_agent_enabled(self, project_id: int, role: str, enabled: bool,
                          project_root: Path | None = None) -> dict[str, Any]:
        configuration = self.configuration(project_id)
        if enabled and not configuration:
            raise GitWorkflowError("Configure the shared Git branch before enabling Git for an agent")
        with self._connect() as db:
            db.execute("""INSERT INTO project_agent_git_settings(project_id,role,enabled) VALUES(?,?,?)
                ON CONFLICT(project_id,role) DO UPDATE SET enabled=excluded.enabled""",
                       (project_id, role, int(enabled)))
        # Role-named branches are legacy-only.  A Git-enabled agent now works
        # against an isolated snapshot of whichever branch the user has open.
        return {"project_id": project_id, "role": role, "enabled": bool(enabled), "branch": ""}

    def remove_agent(self, project_id: int, role: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM project_agent_git_settings WHERE project_id=? AND role=?", (project_id, role))

    def remove_project(self, project_id: int) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM project_agent_git_settings WHERE project_id=?", (project_id,))
            db.execute("DELETE FROM agent_git_commits WHERE project_id=?", (project_id,))
            rows = db.execute("SELECT patch_path FROM git_change_sets WHERE project_id=?", (project_id,)).fetchall()
            db.execute("DELETE FROM git_change_events WHERE project_id=?", (project_id,))
            db.execute("DELETE FROM git_change_sets WHERE project_id=?", (project_id,))
            db.execute("DELETE FROM project_git_workflows WHERE project_id=?", (project_id,))
        for row in rows:
            Path(str(row[0] or "")).unlink(missing_ok=True)

    def configure(self, project_id: int, project_root: Path, main_branch: str, *,
                  initialize: bool = False, remote: str = "", remote_url: str = "") -> dict[str, Any]:
        root = Path(project_root).expanduser().resolve()
        repository = self._repository(root)
        if repository is None:
            if not initialize:
                raise GitWorkflowError("This project folder is not a Git repository. Confirm initialization first.")
            self._run(root, "init")
            repository = self._repository(root)
            assert repository is not None
        main_branch = self._validate_branch(repository, main_branch)
        previous = self.configuration(project_id) or {}
        remote = str(remote or "").strip()
        remote_url = str(remote_url or "").strip()
        if remote and not REMOTE_RE.fullmatch(remote):
            raise GitWorkflowError("Remote names may contain letters, numbers, '.', '_', and '-' only")
        if remote_url and not remote:
            remote = "gh"
        remotes = self._run(repository, "remote").stdout.splitlines()
        previous_remote = str(previous.get("remote") or "").strip()
        rename_source = previous_remote if previous_remote in remotes else (remotes[0] if len(remotes) == 1 else "")
        if remote and remote not in remotes and rename_source:
            self._run(repository, "remote", "rename", rename_source, remote)
            remotes = self._run(repository, "remote").stdout.splitlines()
        if remote_url:
            if remote in remotes:
                self._run(repository, "remote", "set-url", remote, remote_url)
            else:
                self._run(repository, "remote", "add", remote, remote_url)
            self._run(repository, "fetch", remote, timeout=120)
        elif remote and remote not in remotes:
            raise GitWorkflowError(f"Remote '{remote}' does not exist; provide its URL to add it")
        self._checkout_main(repository, main_branch, remote)
        with self._connect() as db:
            db.execute("""INSERT INTO project_git_workflows(project_id,repository,branch,main_branch,remote,enabled,updated_at)
                VALUES(?,?,?,?,?,1,CURRENT_TIMESTAMP)
                ON CONFLICT(project_id) DO UPDATE SET repository=excluded.repository, branch=excluded.branch,
                main_branch=excluded.main_branch, remote=excluded.remote, enabled=1, updated_at=CURRENT_TIMESTAMP""",
                       (project_id, str(repository), main_branch, main_branch, remote))
        return self.status(project_id, root)

    def status(self, project_id: int, project_root: Path) -> dict[str, Any]:
        configuration = self.configuration(project_id)
        repository = self._repository(project_root)
        if repository is None:
            return {"configured": bool(configuration), "is_repository": False, "repository": "",
                    "branch": "", "main_branch": "", "current_branch": "", "clean": None, "remotes": [],
                    "identity_configured": False, "configuration": configuration}
        branch = self._run(repository, "branch", "--show-current").stdout.strip() or "(detached HEAD)"
        remotes = []
        for name in self._run(repository, "remote").stdout.splitlines():
            url = self._run(repository, "remote", "get-url", name, check=False).stdout.strip()
            remotes.append({"name": name, "url": _safe_remote(url)})
        user_name = self._run(repository, "config", "user.name", check=False).stdout.strip()
        user_email = self._run(repository, "config", "user.email", check=False).stdout.strip()
        return {
            "configured": bool(configuration), "is_repository": True, "repository": str(repository),
            "branch": self._main_branch(configuration) if configuration else "",
            "main_branch": self._main_branch(configuration) if configuration else "", "current_branch": branch,
            "clean": not self._run(repository, "status", "--porcelain").stdout.strip(),
            "remotes": remotes, "identity_configured": bool(user_name and user_email),
            "identity": {"name": user_name, "email": user_email}, "configuration": configuration,
        }

    def overview(self, project_id: int, project_root: Path, agents: list[dict[str, str]]) -> dict[str, Any]:
        """Return branch, worktree, and per-agent Git state for the version-control UI."""
        result = self.status(project_id, project_root)
        repository = self._repository(project_root)
        if repository is None:
            return {**result, "branches": [], "worktrees": [], "agents": [], "commits": [], "commits_truncated": False}
        configuration = self.configuration(project_id) or {}
        main_branch = self._main_branch(configuration) if configuration else ""
        current = result["current_branch"]
        branches: list[dict[str, Any]] = []
        refs = self._run(
            repository, "for-each-ref",
            "--format=%(refname:short)\t%(objectname:short)\t%(upstream:short)\t%(HEAD)", "refs/heads",
        ).stdout
        for line in refs.splitlines():
            name, short_hash, upstream, head = (line.split("\t") + ["", "", "", ""])[:4]
            if not name:
                continue
            merged_into_main = bool(main_branch and name != main_branch and self._run(
                repository, "merge-base", "--is-ancestor", f"refs/heads/{name}", f"refs/heads/{main_branch}", check=False,
            ).returncode == 0)
            branches.append({
                "name": name, "head": short_hash, "upstream": upstream, "current": head == "*" or name == current,
                "main": name == main_branch, "merged_into_main": merged_into_main,
            })
        worktrees: list[dict[str, Any]] = []
        for block in self._run(repository, "worktree", "list", "--porcelain").stdout.strip().split("\n\n"):
            if not block.strip():
                continue
            item: dict[str, Any] = {"path": "", "head": "", "branch": "", "detached": False}
            for line in block.splitlines():
                key, _, value = line.partition(" ")
                if key == "worktree": item["path"] = value
                elif key == "HEAD": item["head"] = value[:12]
                elif key == "branch": item["branch"] = value.removeprefix("refs/heads/")
                elif key == "detached": item["detached"] = True
            item["primary"] = Path(item["path"]).resolve() == repository
            worktrees.append(item)
        agent_items: list[dict[str, Any]] = []
        for agent in agents:
            role = str(agent["role"])
            agent_items.append({
                "role": role, "name": str(agent.get("name") or role), "enabled": self.agent_enabled(project_id, role),
                "branch": "", "branch_exists": False, "merged_into_main": False,
                "mode": "shared-current-branch",
            })
        agent_hashes = {
            value for record in self.agent_commits(project_id)
            for value in (record.get("commit_hash"), record.get("merge_hash")) if value
        }
        raw_commits = self._run(
            repository, "log", "--all", "--topo-order", "--date=short",
            "--pretty=format:%H%x1f%P%x1f%D%x1f%h%x1f%an%x1f%ad%x1f%aI%x1f%s%x1e",
        ).stdout
        commits: list[dict[str, Any]] = []
        for record in raw_commits.split("\x1e"):
            values = record.strip().split("\x1f")
            if len(values) != 8 or not values[0]:
                continue
            commit_hash, parents, decorations, short_hash, author, date, timestamp, subject = values
            commits.append({
                "hash": commit_hash, "short_hash": short_hash, "parents": parents.split() if parents else [],
                "decorations": decorations.strip(), "author": author, "date": date, "timestamp": timestamp,
                "subject": subject,
                "agent_commit": commit_hash in agent_hashes,
            })
        # Agent commits intentionally use the configured user's Git identity.  The
        # workflow database is therefore the authoritative source for separating
        # them from ordinary commits made directly in this working copy.
        local_commits = [
            {**commit, "files": self._file_summaries(repository, commit["hash"])}
            for commit in commits if not commit["agent_commit"]
        ]
        commits_by_hash = {commit["hash"]: commit for commit in commits}
        with self._connect() as db:
            change_rows = db.execute("SELECT * FROM git_change_sets WHERE project_id=? ORDER BY created_at DESC LIMIT 100", (project_id,)).fetchall()
        changes = []
        for row in change_rows:
            item = dict(row)
            item["files"] = json.loads(item.pop("files_json") or "[]")
            commit = commits_by_hash.get(item.get("commit_hash") or "")
            if commit:
                item["commit"] = {
                    "hash": commit["hash"], "short_hash": commit["short_hash"],
                    "subject": commit["subject"], "author": commit["author"], "date": commit["date"],
                }
            changes.append(item)
        numstats: dict[str, tuple[int, int]] = {}
        for line in self._run(repository, "diff", "--numstat", "HEAD").stdout.splitlines():
            added, removed, path = (line.split("\t", 2) + ["", "", ""])[:3]
            numstats[path] = (0 if added == "-" else int(added or 0), 0 if removed == "-" else int(removed or 0))
        working_changes = []
        for line in self._run(repository, "status", "--porcelain=v1").stdout.splitlines():
            if len(line) < 4:
                continue
            code, path = line[:2], line[3:]
            if code == "??":
                state, additions, deletions = "new", 0, 0
                file_path = path
            else:
                file_path = path.split(" -> ")[-1]
                additions, deletions = numstats.get(file_path, (0, 0))
                state = "deleted" if "D" in code else "modified"
            working_changes.append({
                "path": file_path, "origin": "local", "state": state,
                "additions": additions, "deletions": deletions,
            })
        working_changes.sort(key=lambda item: (item["state"] == "new", item["path"].lower()))
        return {
            **result, "branches": branches, "worktrees": worktrees, "agents": agent_items,
            "commits": commits, "commits_truncated": False, "changes": changes,
            "working_changes": working_changes, "local_commits": local_commits,
        }

    def change_detail(self, project_id: int, change_id: str) -> dict[str, Any]:
        change = self._change(change_id)
        if int(change["project_id"]) != project_id:
            raise KeyError(f"Unknown Git change {change_id}")
        patch_path = Path(change.get("patch_path") or "")
        change["diff"] = patch_path.read_text(encoding="utf-8", errors="replace") if patch_path.is_file() else ""
        return change

    def discard_change(self, project_id: int, change_id: str) -> dict[str, Any]:
        change = self.change_detail(project_id, change_id)
        if change["state"] in {"committed", "pushed"}:
            raise GitWorkflowError("Committed changes cannot be discarded from the collaboration queue")
        with self._connect() as db:
            db.execute("UPDATE git_change_sets SET state='discarded',updated_at=CURRENT_TIMESTAMP WHERE id=?", (change_id,))
        self._event(change_id, project_id, "user", "discarded", "Discarded held agent patch")
        return self._change(change_id)

    def create_branch(self, project_id: int, project_root: Path, name: str, source: str = "") -> dict[str, str]:
        repository = self._repository(project_root)
        if repository is None:
            raise GitWorkflowError("This project folder is not a Git repository")
        branch = self._validate_branch(repository, name)
        if self._run(repository, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0:
            raise GitWorkflowError(f"Branch '{branch}' already exists")
        configuration = self.configuration(project_id) or {}
        base = str(source or self._main_branch(configuration) or self._run(repository, "branch", "--show-current").stdout.strip())
        base = self._validate_branch(repository, base)
        if self._run(repository, "show-ref", "--verify", "--quiet", f"refs/heads/{base}", check=False).returncode:
            raise GitWorkflowError(f"Source branch '{base}' does not exist")
        self._run(repository, "branch", branch, f"refs/heads/{base}")
        return {"branch": branch, "source": base}

    def checkout_branch(self, project_id: int, project_root: Path, name: str) -> dict[str, Any]:
        configuration, repository = self._configured_repository(project_id, project_root)
        branch = self._validate_branch(repository, name)
        if self._run(repository, "status", "--porcelain").stdout.strip():
            raise GitWorkflowError("Commit, stash, or discard working-tree changes before switching branches")
        if self._run(repository, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode:
            raise GitWorkflowError(f"Branch '{branch}' does not exist")
        self._run(repository, "checkout", "--no-guess", branch)
        return {"branch": branch, "main_branch": self._main_branch(configuration)}

    def delete_branch(self, project_id: int, project_root: Path, name: str, *,
                      disable_agent: bool = False) -> dict[str, Any]:
        configuration, repository = self._configured_repository(project_id, project_root)
        branch = self._validate_branch(repository, name)
        if branch == self._main_branch(configuration):
            raise GitWorkflowError("The configured main branch cannot be deleted")
        if self._run(repository, "branch", "--show-current").stdout.strip() == branch:
            raise GitWorkflowError("Check out another branch before deleting this branch")
        agent_enabled = self.agent_enabled(project_id, branch)
        if agent_enabled and not disable_agent:
            raise GitWorkflowError("Disable this agent's Git workflow before deleting its branch")
        self._run(repository, "branch", "-d", branch)
        if agent_enabled:
            with self._connect() as db:
                db.execute(
                    "UPDATE project_agent_git_settings SET enabled=0 WHERE project_id=? AND role=?",
                    (project_id, branch),
                )
        result: dict[str, Any] = {"deleted": branch}
        if agent_enabled:
            result["disabled_agent_git"] = branch
        return result

    def _configured_repository(self, project_id: int, project_root: Path) -> tuple[dict[str, Any], Path]:
        configuration = self.configuration(project_id)
        if not configuration:
            raise GitWorkflowError("No shared Git branch is configured for this workspace")
        repository = self._repository(project_root)
        if repository is None or repository != Path(configuration["repository"]).resolve():
            raise GitWorkflowError("The selected project folder no longer matches the configured shared repository")
        return configuration, repository

    def _checkout_main(self, repository: Path, main_branch: str, remote: str = "") -> None:
        """Safely make the selected integration branch the checked-out branch."""
        if self._run(repository, "status", "--porcelain").stdout.strip():
            raise GitWorkflowError("Commit, stash, or discard working-tree changes before switching branches")
        current = self._run(repository, "branch", "--show-current").stdout.strip()
        has_head = self._run(repository, "rev-parse", "--verify", "HEAD", check=False).returncode == 0
        if current == main_branch and has_head:
            return
        local = self._run(repository, "show-ref", "--verify", "--quiet", f"refs/heads/{main_branch}", check=False)
        if local.returncode == 0:
            self._run(repository, "checkout", "--no-guess", main_branch)
            return
        remote_ref = f"refs/remotes/{remote}/{main_branch}" if remote else ""
        if remote_ref and self._run(repository, "show-ref", "--verify", "--quiet", remote_ref, check=False).returncode == 0:
            if current == main_branch and not has_head:
                self._run(repository, "checkout", "-B", main_branch, remote_ref)
                self._run(repository, "branch", "--set-upstream-to", remote_ref, f"refs/heads/{main_branch}")
            else:
                self._run(repository, "checkout", "-b", main_branch, "--track", remote_ref)
            return
        if self._run(repository, "rev-parse", "--verify", "HEAD", check=False).returncode == 0:
            self._run(repository, "checkout", "-b", main_branch)
        else:
            self._run(repository, "symbolic-ref", "HEAD", f"refs/heads/{main_branch}")

    def _ensure_initial_main_commit(self, repository: Path, main_branch: str) -> None:
        if self._run(repository, "rev-parse", "--verify", "HEAD", check=False).returncode == 0:
            return
        # The caller is already on the target branch.  Do not require a clean
        # worktree merely to establish the empty base commit: untracked local
        # edits must be allowed alongside an agent run.
        self._run(repository, "commit", "--allow-empty", "-m", "Initialize agent workflow")

    @staticmethod
    def _status_paths(repository: Path) -> set[str]:
        raw = GitWorkflowStore._run(repository, "status", "--porcelain=v1", "-z").stdout
        parts = raw.split("\0")
        paths: set[str] = set()
        for item in parts:
            if not item or len(item) < 4:
                continue
            paths.add(item[3:].replace("\\", "/"))
        return paths

    def _event(self, change_id: str, project_id: int, origin: str, event_type: str, title: str,
               payload: dict[str, Any] | None = None) -> None:
        with self._connect() as db:
            db.execute("""INSERT INTO git_change_events(change_id,project_id,origin,event_type,title,payload_json)
                VALUES(?,?,?,?,?,?)""", (change_id, project_id, origin, event_type, title,
                                              json.dumps(payload or {}, default=str)))

    def _change(self, change_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM git_change_sets WHERE id=?", (change_id,)).fetchone()
            events = db.execute("SELECT * FROM git_change_events WHERE change_id=? ORDER BY id", (change_id,)).fetchall()
        if not row:
            raise KeyError(f"Unknown Git change {change_id}")
        result = dict(row)
        result["files"] = json.loads(result.pop("files_json") or "[]")
        result["events"] = [{**dict(event), "payload": json.loads(event["payload_json"] or "{}")}
                            for event in events]
        return result

    def begin_agent_run(self, project_id: int, role: str, project_root: Path, run_id: str = "") -> dict[str, str]:
        """Create a detached throwaway worktree without changing the user's checkout."""
        if not self.agent_enabled(project_id, role):
            return {}
        configuration, repository = self._configured_repository(project_id, project_root)
        name = self._run(repository, "config", "user.name", check=False).stdout.strip()
        email = self._run(repository, "config", "user.email", check=False).stdout.strip()
        if not name or not email:
            raise GitWorkflowError("Configure git user.name and user.email before Git-enabled agents can commit")
        current_branch = self._run(repository, "branch", "--show-current").stdout.strip()
        if not current_branch:
            raise GitWorkflowError("Check out a branch before starting a Git-enabled agent")
        self._ensure_initial_main_commit(repository, current_branch)
        head = self._run(repository, "rev-parse", "--verify", "HEAD").stdout.strip()
        change_id = uuid.uuid4().hex
        scratch = Path(tempfile.mkdtemp(prefix="maw-agent-worktree-"))
        try:
            self._run(repository, "worktree", "add", "--detach", str(scratch), head, timeout=90)
        except Exception:
            shutil.rmtree(scratch, ignore_errors=True)
            raise
        self._runs[change_id] = {
            "repository": repository, "scratch": scratch, "project_id": project_id, "role": role,
            "run_id": run_id, "base_commit": head, "target_branch": current_branch,
            "initial_dirty": self._status_paths(repository),
        }
        with self._connect() as db:
            db.execute("""INSERT INTO git_change_sets
                (id,project_id,role,run_id,target_branch,base_commit,state) VALUES(?,?,?,?,?,?, 'running')""",
                       (change_id, project_id, role, run_id, current_branch, head))
        self._event(change_id, project_id, "agent", "workspace_created", "Created isolated agent workspace",
                    {"branch": current_branch, "base_commit": head, "role": role})
        return {"repository": str(repository), "base_commit": head, "branch": current_branch,
                "main_branch": current_branch, "change_id": change_id, "workspace": str(scratch)}

    def finish_agent_run(self, project_id: int, role: str, run_id: str, project_root: Path,
                         user_message: str, change_id: str = "") -> dict[str, Any] | None:
        """Persist an isolated patch, then atomically adopt it only if paths remain safe."""
        run = self._runs.pop(change_id, None)
        if not run:
            raise GitWorkflowError("The isolated agent workspace is no longer available")
        repository, scratch = run["repository"], run["scratch"]
        try:
            untracked = self._run(scratch, "ls-files", "--others", "--exclude-standard", "-z").stdout.split("\0")
            paths = [path for path in untracked if path]
            if paths:
                self._run(scratch, "add", "-N", "--", *paths)
            patch = self._run(scratch, "diff", "--binary", "--full-index", run["base_commit"], timeout=90).stdout
            names = self._run(scratch, "diff", "--name-status", run["base_commit"]).stdout.splitlines()
            changed_paths: list[str] = []
            files: list[dict[str, Any]] = []
            for entry in names:
                if not entry:
                    continue
                fields = entry.split("\t")
                status, path = fields[0], fields[-1]
                if path not in changed_paths:
                    changed_paths.append(path)
                    files.append({"path": path, "status": status[:1]})
            if not patch or not changed_paths:
                with self._connect() as db:
                    db.execute("UPDATE git_change_sets SET state='completed',updated_at=CURRENT_TIMESTAMP WHERE id=?", (change_id,))
                self._event(change_id, project_id, "agent", "no_changes", "Agent made no file changes")
                return None
            artifact = self.artifact_root / f"{change_id}.patch"
            artifact.write_text(patch, encoding="utf-8")
            with self._connect() as db:
                db.execute("""UPDATE git_change_sets SET patch_path=?,files_json=?,state='prepared',updated_at=CURRENT_TIMESTAMP
                    WHERE id=?""", (str(artifact), json.dumps(files), change_id))
            self._event(change_id, project_id, "agent", "patch_prepared", f"Prepared {len(files)} file change(s)", {"files": files})
        finally:
            self._run(repository, "worktree", "remove", "--force", str(scratch), check=False, timeout=90)
            shutil.rmtree(scratch, ignore_errors=True)
        change = self._change(change_id)
        if change["review_status"] in {"requested", "delegated"}:
            return self._hold(change_id, f"Awaiting review from {change['review_role']}")
        return self._adopt_change(change_id, user_message, run)

    def _hold(self, change_id: str, detail: str) -> dict[str, Any]:
        with self._connect() as db:
            db.execute("""UPDATE git_change_sets SET state='held_conflict',review_detail=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                       (detail, change_id))
        change = self._change(change_id)
        self._event(change_id, int(change["project_id"]), "system", "held", detail, {"files": change["files"]})
        return {"held": True, "change_id": change_id, "detail": detail, "files": change["files"]}

    def request_review(self, project_id: int, change_id: str, editor_role: str, reviewer_role: str) -> None:
        change = self._change(change_id)
        if int(change["project_id"]) != project_id or change["role"] != editor_role:
            raise GitWorkflowError("Only the editing agent can request review for its active change")
        with self._connect() as db:
            db.execute("""UPDATE git_change_sets SET review_status='requested',review_role=?,state='review_requested',
                updated_at=CURRENT_TIMESTAMP WHERE id=?""", (reviewer_role, change_id))
        self._event(change_id, project_id, "agent", "review_requested", f"Requested review from {reviewer_role}")

    def resolve_review(self, project_id: int, change_id: str, reviewer_role: str, verdict: str,
                       detail: str = "", delegate_role: str = "") -> dict[str, Any]:
        change = self._change(change_id)
        if int(change["project_id"]) != project_id or change.get("review_role") != reviewer_role:
            raise GitWorkflowError("Only the selected reviewer can resolve this change")
        if verdict == "delegate":
            if not delegate_role:
                raise GitWorkflowError("A delegated review needs a recipient role")
            with self._connect() as db:
                db.execute("UPDATE git_change_sets SET review_role=?,review_status='delegated',state='reviewing',review_detail=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                           (delegate_role, detail, change_id))
            self._event(change_id, project_id, "agent", "review_delegated", f"Review delegated to {delegate_role}")
            return self._change(change_id)
        if verdict not in {"approve", "reject"}:
            raise GitWorkflowError("Review verdict must be approve, reject, or delegate")
        state = "approved" if verdict == "approve" else "rejected"
        with self._connect() as db:
            db.execute("UPDATE git_change_sets SET review_status=?,state=?,review_detail=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                       (verdict, state, detail, change_id))
        self._event(change_id, project_id, "agent", f"review_{verdict}", f"Review {verdict}d", {"detail": detail})
        return self._change(change_id)

    def _adopt_change(self, change_id: str, user_message: str, run: dict[str, Any] | None = None) -> dict[str, Any]:
        change = self._change(change_id)
        repository = Path(run["repository"]) if run else Path((self.configuration(int(change["project_id"])) or {})["repository"])
        lock = self._apply_locks.setdefault(int(change["project_id"]), threading.Lock())
        with lock:
            current_branch = self._run(repository, "branch", "--show-current").stdout.strip()
            if current_branch != change["target_branch"]:
                return self._hold(change_id, "Your checked-out branch changed while the agent was working")
            paths = [str(item["path"]) for item in change["files"]]
            dirty = self._status_paths(repository)
            initial_dirty = set((run or {}).get("initial_dirty") or [])
            overlap = sorted(set(paths) & (dirty | initial_dirty))
            if overlap:
                return self._hold(change_id, "Local changes overlap agent paths: " + ", ".join(overlap))
            head = self._run(repository, "rev-parse", "HEAD").stdout.strip()
            changed_since_base = [path for path in paths if self._run(repository, "diff", "--quiet", change["base_commit"], head, "--", path, check=False).returncode]
            if changed_since_base:
                return self._hold(change_id, "Accepted changes overlap agent paths: " + ", ".join(changed_since_base))
            patch_path = Path(change["patch_path"])
            try:
                self._run(repository, "apply", "--index", "--binary", str(patch_path), timeout=90)
            except GitWorkflowError as exc:
                return self._hold(change_id, f"Could not safely apply the agent patch: {exc}")
            subject = " ".join(str(user_message or "").split())[:72].rstrip(".") or "Update workspace"
            subject = subject[:1].upper() + subject[1:]
            try:
                self._run(repository, "commit", "--only", "-m", subject, "--", *paths, timeout=90)
            except GitWorkflowError as exc:
                return self._hold(change_id, f"Patch applied but could not commit only agent paths: {exc}")
            commit_hash = self._run(repository, "rev-parse", "HEAD").stdout.strip()
            files = self._file_summaries(repository, commit_hash)
            with self._connect() as db:
                db.execute("""INSERT OR IGNORE INTO agent_git_commits
                    (project_id,role,run_id,commit_hash,parent_hash,message,files_json,state,pushed)
                    VALUES(?,?,?,?,?,?,?,'committed',0)""",
                    (change["project_id"], change["role"], change["run_id"], commit_hash,
                     self._run(repository, "rev-parse", "HEAD^").stdout.strip(), subject, json.dumps(files)))
                db.execute("UPDATE git_change_sets SET state='committed',commit_hash=?,files_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                           (commit_hash, json.dumps(files), change_id))
            self._event(change_id, int(change["project_id"]), "agent", "committed", f"Committed {len(files)} file change(s)",
                        {"commit_hash": commit_hash, "subject": subject})
            record = self.commit(int(change["project_id"]), commit_hash)
            record.update({"change_id": change_id, "main_branch": current_branch, "target_branch": current_branch})
            configuration = self.configuration(int(change["project_id"])) or {}
            remote = str(configuration.get("remote") or "")
            if remote:
                try:
                    self._run(repository, "push", remote, f"HEAD:refs/heads/{current_branch}", timeout=120)
                    with self._connect() as db:
                        db.execute("UPDATE agent_git_commits SET pushed=1 WHERE project_id=? AND commit_hash=?",
                                   (change["project_id"], commit_hash))
                        db.execute("UPDATE git_change_sets SET state='pushed',updated_at=CURRENT_TIMESTAMP WHERE id=?", (change_id,))
                    record["pushed"] = 1
                    self._event(change_id, int(change["project_id"]), "agent", "pushed", "Pushed current branch", {"remote": remote})
                except GitWorkflowError as exc:
                    with self._connect() as db:
                        db.execute("UPDATE git_change_sets SET state='push_failed',push_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                                   (str(exc), change_id))
                    record["push_error"] = str(exc)
                    self._event(change_id, int(change["project_id"]), "system", "push_failed", "Commit created but push failed", {"detail": str(exc)})
            return record

    def _file_summaries(self, repository: Path, commit_hash: str) -> list[dict[str, Any]]:
        # --root ensures the first commit is summarized as a diff against an
        # empty tree, which is especially important for a newly initialized
        # agent workspace.
        numbers = self._run(repository, "diff-tree", "--root", "--no-commit-id", "--numstat", "-r", "-m", "--find-renames", commit_hash).stdout
        statuses = self._run(repository, "diff-tree", "--root", "--no-commit-id", "--name-status", "-r", "-m", "--find-renames", commit_hash).stdout
        status_by_path: dict[str, str] = {}
        for line in statuses.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                status_by_path[parts[-1]] = parts[0]
        files = []
        seen_paths: set[str] = set()
        for line in numbers.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            additions, deletions = parts[0], parts[1]
            path = parts[-1]
            if path in seen_paths:
                continue
            seen_paths.add(path)
            previous = parts[-2] if len(parts) > 3 else ""
            files.append({
                "path": path, "previous_path": previous, "status": status_by_path.get(path, "M"),
                "additions": None if additions == "-" else int(additions),
                "deletions": None if deletions == "-" else int(deletions),
            })
        return files

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        try:
            item["files"] = json.loads(item.pop("files_json") or "[]")
        except json.JSONDecodeError:
            item["files"] = []
        item["pushed"] = bool(item.get("pushed"))
        return item

    @staticmethod
    def _resolve_commit(repository: Path, commit_hash: str) -> str:
        normalized = str(commit_hash or "").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{4,64}", normalized):
            raise GitWorkflowError("Commit IDs must be hexadecimal Git hashes")
        resolved = GitWorkflowStore._run(
            repository, "rev-parse", "--verify", f"{normalized}^{{commit}}", check=False,
        )
        if resolved.returncode:
            raise GitWorkflowError(f"Commit '{normalized}' does not exist")
        return resolved.stdout.strip()

    def commit_detail(self, project_id: int, project_root: Path, commit_hash: str) -> dict[str, Any]:
        """Return inspectable metadata and changed files for any repository commit."""
        _, repository = self._configured_repository(project_id, project_root)
        resolved = self._resolve_commit(repository, commit_hash)
        values = self._run(
            repository, "show", "-s", "--date=short",
            "--format=%H%x1f%P%x1f%D%x1f%h%x1f%an%x1f%ad%x1f%s", resolved,
        ).stdout.strip().split("\x1f")
        values += [""] * (7 - len(values))
        tracked: dict[str, Any] | None = None
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM agent_git_commits WHERE project_id=? AND commit_hash=?",
                (project_id, resolved),
            ).fetchone()
        if row:
            tracked = self._record(row)
        return {
            "hash": values[0] or resolved,
            "parents": values[1].split() if values[1] else [],
            "decorations": values[2].strip(),
            "short_hash": values[3] or resolved[:12],
            "author": values[4], "date": values[5], "subject": values[6],
            "files": self._file_summaries(repository, resolved),
            "agent_commit": bool(tracked),
            "tracked": tracked,
        }

    def commit(self, project_id: int, commit_hash: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM agent_git_commits WHERE project_id=? AND commit_hash=?",
                             (project_id, commit_hash)).fetchone()
        if not row:
            raise KeyError(f"Commit {commit_hash} is not tracked by this workspace")
        return self._record(row)

    def agent_commits(self, project_id: int, role: str = "") -> list[dict[str, Any]]:
        query = "SELECT * FROM agent_git_commits WHERE project_id=?"
        values: list[Any] = [project_id]
        if role:
            query += " AND role=?"
            values.append(role)
        query += " ORDER BY id DESC"
        with self._connect() as db:
            rows = db.execute(query, values).fetchall()
        return [self._record(row) for row in rows]

    def file_diff(self, project_id: int, project_root: Path, commit_hash: str, path: str) -> dict[str, Any]:
        record = self.commit_detail(project_id, project_root, commit_hash)
        _, repository = self._configured_repository(project_id, project_root)
        path = _relative_git_path(path)
        if path not in {item["path"] for item in record["files"]}:
            raise GitWorkflowError("That file was not changed by the selected commit")
        diff = self._run(repository, "show", "--format=", "--root", "-m", "--find-renames", "--unified=3", record["hash"], "--", path).stdout
        if len(diff.encode("utf-8")) > MAX_DIFF_BYTES:
            diff = diff.encode("utf-8")[:MAX_DIFF_BYTES].decode("utf-8", errors="replace") + "\n… diff truncated …\n"
        return {"commit": record, "path": path, "diff": diff}

    def open_diff(self, project_id: int, project_root: Path, commit_hash: str, path: str, editor: str) -> dict[str, str]:
        record = self.commit_detail(project_id, project_root, commit_hash)
        _, repository = self._configured_repository(project_id, project_root)
        path = _relative_git_path(path)
        if path not in {item["path"] for item in record["files"]}:
            raise GitWorkflowError("That file was not changed by the selected commit")
        parent = (record.get("parents") or [""])[0]
        before = self._show_file(repository, parent, path) if parent else ""
        after = self._show_file(repository, commit_hash, path)
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix="agent-diff-", dir=self.artifact_root))
        name = Path(path).name or "changed-file"
        before_path, after_path = directory / f"before-{name}", directory / f"after-{name}"
        before_path.write_text(before, encoding="utf-8", errors="replace")
        after_path.write_text(after, encoding="utf-8", errors="replace")
        executable, arguments = self._editor_command(editor, before_path, after_path)
        try:
            subprocess.Popen([executable, *arguments], cwd=str(repository), close_fds=os.name != "nt")
        except OSError as exc:
            raise GitWorkflowError(f"Could not open {editor}: {exc}") from exc
        return {"editor": editor, "before": str(before_path), "after": str(after_path)}

    def _show_file(self, repository: Path, commit_hash: str, path: str) -> str:
        if not commit_hash:
            return ""
        result = self._run(repository, "show", f"{commit_hash}:{path}", check=False)
        return result.stdout if result.returncode == 0 else ""

    @staticmethod
    def _editor_command(editor: str, before: Path, after: Path) -> tuple[str, list[str]]:
        normalized = str(editor or "").strip().lower()
        if normalized == "vscode":
            executable = os.getenv("VSCODE_COMMAND", "").strip() or shutil.which("code")
            if not executable:
                raise GitWorkflowError("VS Code command-line launcher was not found. Install the 'code' command or set VSCODE_COMMAND")
            return executable, ["--diff", str(before), str(after)]
        if normalized == "pycharm":
            executable = os.getenv("PYCHARM_COMMAND", "").strip() or shutil.which("pycharm") or shutil.which("idea")
            if not executable:
                raise GitWorkflowError("PyCharm command-line launcher was not found. Install the 'pycharm' launcher or set PYCHARM_COMMAND")
            return executable, ["diff", str(before), str(after)]
        raise GitWorkflowError("Editor must be 'pycharm' or 'vscode'")

    def revert(self, project_id: int, project_root: Path, commit_hash: str) -> dict[str, Any]:
        configuration, repository = self._configured_repository(project_id, project_root)
        resolved = self._resolve_commit(repository, commit_hash)
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM agent_git_commits WHERE project_id=? AND commit_hash=?",
                (project_id, resolved),
            ).fetchone()
        record = self._record(row) if row else None
        if self._run(repository, "status", "--porcelain").stdout.strip():
            raise GitWorkflowError("The working tree must be clean before reverting a commit")
        main_branch = self._main_branch(configuration)
        self._checkout_main(repository, main_branch, configuration.get("remote", ""))
        target = self._resolve_commit(repository, (record.get("merge_hash") or resolved) if record else resolved)
        parent_count = len(self._run(repository, "show", "-s", "--format=%P", target).stdout.strip().split())
        args = ["revert", "--no-edit"]
        if parent_count > 1:
            args.extend(["-m", "1"])
        args.append(target)
        self._run(repository, *args, timeout=60)
        reverted_by = self._run(repository, "rev-parse", "HEAD").stdout.strip()
        if record:
            with self._connect() as db:
                db.execute("UPDATE agent_git_commits SET state='reverted' WHERE project_id=? AND commit_hash=?",
                           (project_id, resolved))
        return {"reverted": resolved, "revert_commit": reverted_by, "main_branch": main_branch}

    def _restore_branch(self, repository: Path, branch: str, head: str) -> None:
        if branch:
            self._run(repository, "checkout", "--no-guess", branch)
        elif head:
            self._run(repository, "checkout", "--detach", head)

    def _prepare_branch_operation(self, repository: Path, branch: str) -> tuple[str, str]:
        branch = self._validate_branch(repository, branch)
        if self._run(repository, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode:
            raise GitWorkflowError(f"Branch '{branch}' does not exist")
        if self._run(repository, "status", "--porcelain").stdout.strip():
            raise GitWorkflowError("The working tree must be clean before changing branch history")
        previous_branch = self._run(repository, "branch", "--show-current").stdout.strip()
        previous_head = self._run(repository, "rev-parse", "HEAD", check=False).stdout.strip()
        if previous_branch != branch:
            self._run(repository, "checkout", "--no-guess", branch)
        return previous_branch, previous_head

    def rebase(self, project_id: int, project_root: Path, commit_hash: str, branch: str) -> dict[str, Any]:
        """Rebase a selected local branch onto a selected commit, aborting conflicts safely."""
        _, repository = self._configured_repository(project_id, project_root)
        target = self._resolve_commit(repository, commit_hash)
        previous_branch, previous_head = self._prepare_branch_operation(repository, branch)
        branch = self._validate_branch(repository, branch)
        try:
            self._run(repository, "rebase", target, timeout=120)
        except GitWorkflowError as exc:
            self._run(repository, "rebase", "--abort", check=False)
            self._restore_branch(repository, previous_branch, previous_head)
            raise GitWorkflowError(
                f"Rebase of '{branch}' onto {target[:12]} failed and was aborted. {exc}"
            ) from exc
        head = self._run(repository, "rev-parse", f"refs/heads/{branch}").stdout.strip()
        self._restore_branch(repository, previous_branch, previous_head)
        return {"rebased": branch, "onto": target, "head": head, "current_branch": previous_branch or "(detached HEAD)"}

    def merge(self, project_id: int, project_root: Path, commit_hash: str, branch: str) -> dict[str, Any]:
        """Merge a selected commit into a selected local branch, aborting conflicts safely."""
        _, repository = self._configured_repository(project_id, project_root)
        target = self._resolve_commit(repository, commit_hash)
        previous_branch, previous_head = self._prepare_branch_operation(repository, branch)
        branch = self._validate_branch(repository, branch)
        try:
            self._run(repository, "merge", "--no-ff", "--no-edit", target, timeout=120)
        except GitWorkflowError as exc:
            self._run(repository, "merge", "--abort", check=False)
            self._restore_branch(repository, previous_branch, previous_head)
            raise GitWorkflowError(
                f"Merge of {target[:12]} into '{branch}' failed and was aborted. {exc}"
            ) from exc
        head = self._run(repository, "rev-parse", f"refs/heads/{branch}").stdout.strip()
        self._restore_branch(repository, previous_branch, previous_head)
        return {"merged": target, "target_branch": branch, "head": head, "current_branch": previous_branch or "(detached HEAD)"}

    def merge_into_all_branches(self, project_id: int, project_root: Path, commit_hash: str) -> dict[str, Any]:
        """Merge a selected commit into every local branch that does not contain it.

        Each merge runs in a temporary detached worktree so uncommitted changes in
        the user's primary worktree do not prevent branch-only maintenance.
        """
        _, repository = self._configured_repository(project_id, project_root)
        target = self._resolve_commit(repository, commit_hash)
        previous_branch = self._run(repository, "branch", "--show-current").stdout.strip()
        branches = [
            value.strip() for value in self._run(
                repository, "for-each-ref", "--format=%(refname:short)", "refs/heads",
            ).stdout.splitlines() if value.strip()
        ]
        skipped: list[str] = []
        candidates: list[str] = []
        for branch in branches:
            if self._run(
                repository, "merge-base", "--is-ancestor", target, f"refs/heads/{branch}", check=False,
            ).returncode == 0:
                skipped.append(branch)
            else:
                candidates.append(branch)
        merged: list[str] = []
        failed: list[dict[str, str]] = []
        for branch in candidates:
            worktree_path: Path | None = None
            try:
                self._validate_branch(repository, branch)
                old_head = self._run(repository, "rev-parse", f"refs/heads/{branch}").stdout.strip()
                worktree_path = Path(tempfile.mkdtemp(prefix="git-merge-all-"))
                self._run(repository, "worktree", "add", "--detach", "--quiet", str(worktree_path), old_head, timeout=60)
                try:
                    self._run(worktree_path, "merge", "--no-ff", "--no-edit", target, timeout=120)
                except GitWorkflowError as exc:
                    self._run(worktree_path, "merge", "--abort", check=False)
                    failed.append({"branch": branch, "error": str(exc)})
                else:
                    new_head = self._run(worktree_path, "rev-parse", "HEAD").stdout.strip()
                    self._run(repository, "update-ref", f"refs/heads/{branch}", new_head, old_head)
                    merged.append(branch)
            except GitWorkflowError as exc:
                failed.append({"branch": branch, "error": str(exc)})
            finally:
                if worktree_path is not None:
                    self._run(repository, "worktree", "remove", "--force", str(worktree_path), check=False)
                    shutil.rmtree(worktree_path, ignore_errors=True)
        return {
            "merged_commit": target, "merged": merged, "skipped": skipped, "failed": failed,
            "current_branch": previous_branch or "(detached HEAD)",
        }

    def consolidate_branches(self, project_id: int, project_root: Path) -> dict[str, Any]:
        """Integrate every divergent local branch into the configured main branch.

        Branch refs are retained so the operation preserves recovery points; the
        UI can present the resulting main history as the consolidated graph.
        """
        configuration, repository = self._configured_repository(project_id, project_root)
        main_branch = self._validate_branch(repository, self._main_branch(configuration))
        main_head = self._run(repository, "rev-parse", f"refs/heads/{main_branch}").stdout.strip()
        branches = [
            value.strip() for value in self._run(
                repository, "for-each-ref", "--format=%(refname:short)", "refs/heads",
            ).stdout.splitlines() if value.strip() and value.strip() != main_branch
        ]
        merged: list[str] = []
        skipped: list[str] = []
        failed: list[dict[str, str]] = []
        for branch in branches:
            worktree_path: Path | None = None
            try:
                self._validate_branch(repository, branch)
                branch_head = self._run(repository, "rev-parse", f"refs/heads/{branch}").stdout.strip()
                if self._run(
                    repository, "merge-base", "--is-ancestor", branch_head, main_head, check=False,
                ).returncode == 0:
                    skipped.append(branch)
                    continue
                worktree_path = Path(tempfile.mkdtemp(prefix="git-consolidate-"))
                self._run(repository, "worktree", "add", "--detach", "--quiet", str(worktree_path), main_head, timeout=60)
                try:
                    self._run(worktree_path, "merge", "--no-ff", "--no-edit", branch_head, timeout=120)
                except GitWorkflowError as exc:
                    self._run(worktree_path, "merge", "--abort", check=False)
                    failed.append({"branch": branch, "error": str(exc)})
                    continue
                new_head = self._run(worktree_path, "rev-parse", "HEAD").stdout.strip()
                self._run(repository, "update-ref", f"refs/heads/{main_branch}", new_head, main_head)
                main_head = new_head
                merged.append(branch)
            except GitWorkflowError as exc:
                failed.append({"branch": branch, "error": str(exc)})
            finally:
                if worktree_path is not None:
                    self._run(repository, "worktree", "remove", "--force", str(worktree_path), check=False)
                    shutil.rmtree(worktree_path, ignore_errors=True)
        return {
            "main_branch": main_branch, "head": main_head, "merged": merged,
            "skipped": skipped, "failed": failed, "consolidated": not failed,
        }

    def rollback(self, project_id: int, project_root: Path, commit_hash: str) -> dict[str, Any]:
        record = self.commit(project_id, commit_hash)
        configuration, repository = self._configured_repository(project_id, project_root)
        main_branch = self._main_branch(configuration)
        self._checkout_main(repository, main_branch, configuration.get("remote", ""))
        head = self._run(repository, "rev-parse", "HEAD", check=False).stdout.strip()
        target = record.get("merge_hash") or commit_hash
        if head != target:
            raise GitWorkflowError("Only the current HEAD merged agent change can be rolled back")
        parent = record.get("main_parent_hash") or record.get("parent_hash")
        if not parent:
            raise GitWorkflowError("The initial commit cannot be hard-rolled back; use Revert instead")
        self._run(repository, "reset", "--hard", parent, timeout=60)
        if record.get("agent_branch"):
            self._run(repository, "branch", "-f", record["agent_branch"], parent)
        with self._connect() as db:
            db.execute("UPDATE agent_git_commits SET state='rolled_back' WHERE project_id=? AND commit_hash=?",
                       (project_id, commit_hash))
        return {"rolled_back": commit_hash, "head": parent, "main_branch": main_branch}

    def push(self, project_id: int, project_root: Path, commit_hash: str, remote: str = "") -> dict[str, Any]:
        record = self.commit(project_id, commit_hash)
        configuration, repository = self._configured_repository(project_id, project_root)
        selected = str(remote or configuration.get("remote") or "gh").strip()
        remotes = self._run(repository, "remote").stdout.splitlines()
        if selected not in remotes:
            raise GitWorkflowError(f"Remote '{selected}' does not exist")
        branch = self._run(repository, "branch", "--show-current").stdout.strip()
        if not branch:
            raise GitWorkflowError("Check out a branch before pushing a shared-workspace change")
        self._run(repository, "push", selected, f"HEAD:refs/heads/{branch}", timeout=120)
        with self._connect() as db:
            db.execute("UPDATE agent_git_commits SET pushed=1 WHERE project_id=? AND commit_hash=?",
                       (project_id, commit_hash))
        return {"pushed": commit_hash, "remote": selected, "branch": branch}
