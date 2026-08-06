from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
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
    """Project Git configuration plus durable, per-agent commit summaries."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self.artifact_root = Path(self.db_path).parent / "git-diffs"
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
        branch = ""
        if enabled and configuration:
            repository = self._repository(project_root) if project_root else None
            if repository:
                branch = self._agent_branch(repository, role)
                if self._run(repository, "rev-parse", "--verify", "HEAD", check=False).returncode == 0:
                    self._checkout_main(repository, self._main_branch(configuration))
                    self._prepare_agent_branch(repository, branch, self._main_branch(configuration))
                    self._checkout_main(repository, self._main_branch(configuration))
        with self._connect() as db:
            db.execute("""INSERT INTO project_agent_git_settings(project_id,role,enabled) VALUES(?,?,?)
                ON CONFLICT(project_id,role) DO UPDATE SET enabled=excluded.enabled""",
                       (project_id, role, int(enabled)))
        return {"project_id": project_id, "role": role, "enabled": bool(enabled), "branch": branch or role}

    def remove_agent(self, project_id: int, role: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM project_agent_git_settings WHERE project_id=? AND role=?", (project_id, role))

    def remove_project(self, project_id: int) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM project_agent_git_settings WHERE project_id=?", (project_id,))
            db.execute("DELETE FROM agent_git_commits WHERE project_id=?", (project_id,))
            db.execute("DELETE FROM project_git_workflows WHERE project_id=?", (project_id,))

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
            branches.append({
                "name": name, "head": short_hash, "upstream": upstream, "current": head == "*" or name == current,
                "main": name == main_branch,
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
            branch = self._agent_branch(repository, role)
            exists = self._run(repository, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0
            merged = bool(main_branch and exists and self._run(
                repository, "merge-base", "--is-ancestor", f"refs/heads/{branch}", f"refs/heads/{main_branch}", check=False,
            ).returncode == 0)
            agent_items.append({
                "role": role, "name": str(agent.get("name") or role), "enabled": self.agent_enabled(project_id, role),
                "branch": branch, "branch_exists": exists, "merged_into_main": merged,
            })
        agent_hashes = {
            value for record in self.agent_commits(project_id)
            for value in (record.get("commit_hash"), record.get("merge_hash")) if value
        }
        raw_commits = self._run(
            repository, "log", "--all", "--topo-order", "--date=short",
            "--pretty=format:%H%x1f%P%x1f%D%x1f%h%x1f%an%x1f%ad%x1f%s%x1e",
        ).stdout
        commits: list[dict[str, Any]] = []
        for record in raw_commits.split("\x1e"):
            values = record.strip().split("\x1f")
            if len(values) != 7 or not values[0]:
                continue
            commit_hash, parents, decorations, short_hash, author, date, subject = values
            commits.append({
                "hash": commit_hash, "short_hash": short_hash, "parents": parents.split() if parents else [],
                "decorations": decorations.strip(), "author": author, "date": date, "subject": subject,
                "agent_commit": commit_hash in agent_hashes,
            })
        return {
            **result, "branches": branches, "worktrees": worktrees, "agents": agent_items,
            "commits": commits, "commits_truncated": False,
        }

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

    def delete_branch(self, project_id: int, project_root: Path, name: str) -> dict[str, str]:
        configuration, repository = self._configured_repository(project_id, project_root)
        branch = self._validate_branch(repository, name)
        if branch == self._main_branch(configuration):
            raise GitWorkflowError("The configured main branch cannot be deleted")
        if self._run(repository, "branch", "--show-current").stdout.strip() == branch:
            raise GitWorkflowError("Check out another branch before deleting this branch")
        if self.agent_enabled(project_id, branch):
            raise GitWorkflowError("Disable this agent's Git workflow before deleting its branch")
        self._run(repository, "branch", "-d", branch)
        return {"deleted": branch}

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

    def _prepare_agent_branch(self, repository: Path, agent_branch: str, main_branch: str) -> None:
        """Start an agent branch at main only after its previous work is merged."""
        exists = self._run(repository, "show-ref", "--verify", "--quiet", f"refs/heads/{agent_branch}", check=False)
        if exists.returncode == 0:
            merged = self._run(repository, "merge-base", "--is-ancestor", f"refs/heads/{agent_branch}",
                               f"refs/heads/{main_branch}", check=False)
            if merged.returncode:
                raise GitWorkflowError(
                    f"Agent branch '{agent_branch}' has unmerged work. Resolve it before starting another run."
                )
            self._run(repository, "branch", "-f", agent_branch, f"refs/heads/{main_branch}")
            self._run(repository, "checkout", "--no-guess", agent_branch)
        else:
            self._run(repository, "checkout", "-b", agent_branch, f"refs/heads/{main_branch}")

    def _ensure_initial_main_commit(self, repository: Path, main_branch: str) -> None:
        if self._run(repository, "rev-parse", "--verify", "HEAD", check=False).returncode == 0:
            return
        self._checkout_main(repository, main_branch)
        self._run(repository, "commit", "--allow-empty", "-m", "Initialize agent workflow")

    def begin_agent_run(self, project_id: int, role: str, project_root: Path) -> dict[str, str]:
        if not self.agent_enabled(project_id, role):
            return {}
        configuration, repository = self._configured_repository(project_id, project_root)
        if self._run(repository, "status", "--porcelain").stdout.strip():
            raise GitWorkflowError("The shared working tree has uncommitted changes; resolve them before an agent run")
        name = self._run(repository, "config", "user.name", check=False).stdout.strip()
        email = self._run(repository, "config", "user.email", check=False).stdout.strip()
        if not name or not email:
            raise GitWorkflowError("Configure git user.name and user.email before Git-enabled agents can commit")
        main_branch = self._main_branch(configuration)
        self._checkout_main(repository, main_branch, configuration.get("remote", ""))
        self._ensure_initial_main_commit(repository, main_branch)
        agent_branch = self._agent_branch(repository, role)
        self._prepare_agent_branch(repository, agent_branch, main_branch)
        head = self._run(repository, "rev-parse", "--verify", "HEAD").stdout.strip()
        return {"repository": str(repository), "base_commit": head, "branch": agent_branch, "main_branch": main_branch}

    def finish_agent_run(self, project_id: int, role: str, run_id: str, project_root: Path,
                         user_message: str) -> dict[str, Any] | None:
        configuration, repository = self._configured_repository(project_id, project_root)
        agent_branch = self._agent_branch(repository, role)
        current = self._run(repository, "branch", "--show-current").stdout.strip()
        if current != agent_branch:
            raise GitWorkflowError("The agent changed the shared Git branch; no automatic commit was created")
        if not self._run(repository, "status", "--porcelain").stdout.strip():
            self._checkout_main(repository, self._main_branch(configuration), configuration.get("remote", ""))
            return None
        subject = " ".join(str(user_message or "").split())[:72] or "update workspace"
        message = f"agent({role}): {subject}"
        self._run(repository, "add", "-A")
        self._run(repository, "commit", "-m", message, timeout=60)
        commit_hash = self._run(repository, "rev-parse", "HEAD").stdout.strip()
        parent = self._run(repository, "rev-parse", "HEAD^", check=False).stdout.strip()
        files = self._file_summaries(repository, commit_hash)
        main_branch = self._main_branch(configuration)
        self._checkout_main(repository, main_branch, configuration.get("remote", ""))
        main_parent = self._run(repository, "rev-parse", "HEAD").stdout.strip()
        merge_message = f"Merge agent {role}: {subject}"
        try:
            self._run(repository, "merge", "--no-ff", f"refs/heads/{agent_branch}", "-m", merge_message, timeout=60)
        except GitWorkflowError as exc:
            raise GitWorkflowError(
                f"Automatic merge of '{agent_branch}' into '{main_branch}' failed. Resolve the Git merge conflict, then retry. {exc}"
            ) from exc
        merge_hash = self._run(repository, "rev-parse", "HEAD").stdout.strip()
        with self._connect() as db:
            db.execute("""INSERT OR IGNORE INTO agent_git_commits
                (project_id,role,run_id,commit_hash,parent_hash,merge_hash,main_parent_hash,agent_branch,message,files_json,state,pushed)
                VALUES(?,?,?,?,?,?,?,?,?,?,'committed',0)""",
                       (project_id, role, run_id, commit_hash, parent, merge_hash, main_parent, agent_branch,
                        message, json.dumps(files)))
        record = self.commit(project_id, commit_hash)
        record["main_branch"] = main_branch
        return record

    def _file_summaries(self, repository: Path, commit_hash: str) -> list[dict[str, Any]]:
        # --root ensures the first commit is summarized as a diff against an
        # empty tree, which is especially important for a newly initialized
        # agent workspace.
        numbers = self._run(repository, "diff-tree", "--root", "--no-commit-id", "--numstat", "-r", "--find-renames", commit_hash).stdout
        statuses = self._run(repository, "diff-tree", "--root", "--no-commit-id", "--name-status", "-r", "--find-renames", commit_hash).stdout
        status_by_path: dict[str, str] = {}
        for line in statuses.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                status_by_path[parts[-1]] = parts[0]
        files = []
        for line in numbers.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            additions, deletions = parts[0], parts[1]
            path = parts[-1]
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
        record = self.commit(project_id, commit_hash)
        _, repository = self._configured_repository(project_id, project_root)
        path = _relative_git_path(path)
        if path not in {item["path"] for item in record["files"]}:
            raise GitWorkflowError("That file was not changed by the selected agent commit")
        diff = self._run(repository, "show", "--format=", "--find-renames", "--unified=3", commit_hash, "--", path).stdout
        if len(diff.encode("utf-8")) > MAX_DIFF_BYTES:
            diff = diff.encode("utf-8")[:MAX_DIFF_BYTES].decode("utf-8", errors="replace") + "\n… diff truncated …\n"
        return {"commit": record, "path": path, "diff": diff}

    def open_diff(self, project_id: int, project_root: Path, commit_hash: str, path: str, editor: str) -> dict[str, str]:
        record = self.commit(project_id, commit_hash)
        _, repository = self._configured_repository(project_id, project_root)
        path = _relative_git_path(path)
        if path not in {item["path"] for item in record["files"]}:
            raise GitWorkflowError("That file was not changed by the selected agent commit")
        parent = record.get("parent_hash") or ""
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
        record = self.commit(project_id, commit_hash)
        configuration, repository = self._configured_repository(project_id, project_root)
        if self._run(repository, "status", "--porcelain").stdout.strip():
            raise GitWorkflowError("The working tree must be clean before reverting a commit")
        main_branch = self._main_branch(configuration)
        self._checkout_main(repository, main_branch, configuration.get("remote", ""))
        target = record.get("merge_hash") or commit_hash
        parent_count = len(self._run(repository, "show", "-s", "--format=%P", target).stdout.strip().split())
        args = ["revert", "--no-edit"]
        if parent_count > 1:
            args.extend(["-m", "1"])
        args.append(target)
        self._run(repository, *args, timeout=60)
        reverted_by = self._run(repository, "rev-parse", "HEAD").stdout.strip()
        with self._connect() as db:
            db.execute("UPDATE agent_git_commits SET state='reverted' WHERE project_id=? AND commit_hash=?",
                       (project_id, commit_hash))
        return {"reverted": commit_hash, "revert_commit": reverted_by, "main_branch": main_branch}

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
        main_branch = self._main_branch(configuration)
        main_ref = f"refs/heads/{main_branch}:refs/heads/{main_branch}"
        self._run(repository, "push", "--set-upstream", selected, main_ref, timeout=120)
        agent_branch = record.get("agent_branch") or self._agent_branch(repository, record["role"])
        agent_ref = f"refs/heads/{agent_branch}:refs/heads/{agent_branch}"
        self._run(repository, "push", "--set-upstream", selected, agent_ref, timeout=120)
        with self._connect() as db:
            db.execute("UPDATE agent_git_commits SET pushed=1 WHERE project_id=? AND commit_hash=?",
                       (project_id, commit_hash))
        return {"pushed": commit_hash, "remote": selected, "main_branch": main_branch, "agent_branch": agent_branch}
