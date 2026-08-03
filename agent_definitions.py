from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


BUILTIN_AGENTS = {
    "orchestrator": ("Orchestrator", "Clarify goals, plan work, coordinate specialists, reconcile results, and give the user a decisive next action."),
    "researcher": ("Researcher", "Investigate questions, distinguish evidence from inference, identify gaps, and publish concise findings."),
    "programmer": ("Programmer", "Design and implement robust software, explain tradeoffs, and produce testable technical work."),
    "reviewer": ("Reviewer", "Critically inspect plans and outputs for correctness, risk, missing cases, and unsupported claims."),
    "formatter": ("Formatter", "Transform material into a clear requested structure while preserving meaning and consistency."),
    "documenter": ("Documenter", "Create maintainable documentation, examples, decisions, and handoff notes for future readers."),
}


class AgentDefinitionStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS agent_definitions (
                role TEXT PRIMARY KEY, name TEXT NOT NULL, brief TEXT NOT NULL,
                instructions TEXT NOT NULL, built_in INTEGER NOT NULL DEFAULT 0)""")
            db.execute("CREATE TABLE IF NOT EXISTS agent_definition_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS agent_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, agent_name TEXT NOT NULL DEFAULT '', role_hint TEXT NOT NULL,
                brief TEXT NOT NULL, instructions TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            template_columns = {row[1] for row in db.execute("PRAGMA table_info(agent_templates)")}
            if "agent_name" not in template_columns:
                db.execute("ALTER TABLE agent_templates ADD COLUMN agent_name TEXT NOT NULL DEFAULT ''")
            if not db.execute("SELECT 1 FROM agent_definition_meta WHERE key='builtins_seeded'").fetchone():
                for role, (name, brief) in BUILTIN_AGENTS.items():
                    db.execute("""INSERT OR IGNORE INTO agent_definitions(role,name,brief,instructions,built_in)
                        VALUES(?,?,?,?,1)""", (role, name, brief, brief))
                db.execute("INSERT INTO agent_definition_meta VALUES('builtins_seeded','1')")
            db.execute("""CREATE TABLE IF NOT EXISTS project_agents (
                project_id INTEGER NOT NULL, role TEXT NOT NULL, name TEXT NOT NULL,
                brief TEXT NOT NULL, instructions TEXT NOT NULL, built_in INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(project_id, role),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE)""")
            self._migrate_existing_projects(db)

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
    def _projects_available(db: sqlite3.Connection) -> bool:
        return bool(db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='projects'"
        ).fetchone())

    def _migrate_existing_projects(self, db: sqlite3.Connection) -> None:
        """Copy legacy global definitions into workspaces that already exist.

        This preserves existing workspaces while ensuring projects created after
        the migration start with an intentionally empty team.
        """
        if not self._projects_available(db):
            return
        project_ids = [row[0] for row in db.execute("SELECT id FROM projects")]
        definitions = [
            tuple(row)
            for row in db.execute(
                "SELECT role,name,brief,instructions,built_in FROM agent_definitions"
            )
        ]
        for project_id in project_ids:
            db.executemany(
                """INSERT OR IGNORE INTO project_agents
                    (project_id,role,name,brief,instructions,built_in)
                    VALUES(?,?,?,?,?,?)""",
                [(project_id, *definition) for definition in definitions],
            )

    def list(self, project_id: int | None = None) -> list[dict]:
        with self._connect() as db:
            if project_id is not None and self._projects_available(db):
                rows = db.execute(
                    "SELECT role,name,brief,instructions,built_in FROM project_agents "
                    "WHERE project_id=? ORDER BY built_in DESC, rowid", (project_id,)
                ).fetchall()
            else:
                rows = db.execute("SELECT * FROM agent_definitions ORDER BY built_in DESC, rowid").fetchall()
        return [dict(row) for row in rows]

    def get(self, role: str, project_id: int | None = None) -> dict:
        with self._connect() as db:
            if project_id is not None and self._projects_available(db):
                row = db.execute(
                    "SELECT role,name,brief,instructions,built_in FROM project_agents "
                    "WHERE project_id=? AND role=?", (project_id, role)
                ).fetchone()
            else:
                row = db.execute("SELECT * FROM agent_definitions WHERE role=?", (role,)).fetchone()
        if not row:
            scope = f" in project {project_id}" if project_id is not None else ""
            raise KeyError(f"Unknown role: {role}{scope}")
        return dict(row)

    def save(self, name: str, brief: str, instructions: str, role: str = "",
             project_id: int | None = None) -> dict:
        name, brief, instructions = name.strip(), brief.strip(), instructions.strip()
        if not name or not brief or not instructions:
            raise ValueError("Name, role summary, and instructions are required")
        role = role.strip() or re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        if not role or not re.fullmatch(r"[a-z][a-z0-9_]{1,39}", role):
            raise ValueError("Role ID must be 2–40 lowercase letters, numbers, or underscores")
        with self._connect() as db:
            if project_id is not None and self._projects_available(db):
                exists = db.execute(
                    "SELECT 1 FROM project_agents WHERE project_id=? AND role=?", (project_id, role)
                ).fetchone()
                if exists:
                    raise ValueError(f"Role ID '{role}' already exists in this workspace")
                db.execute(
                    """INSERT INTO project_agents(project_id,role,name,brief,instructions,built_in)
                        VALUES(?,?,?,?,?,0)""",
                    (project_id, role, name, brief, instructions),
                )
            else:
                if db.execute("SELECT 1 FROM agent_definitions WHERE role=?", (role,)).fetchone():
                    raise ValueError(f"Role ID '{role}' already exists")
                db.execute("INSERT INTO agent_definitions VALUES(?,?,?,?,0)", (role, name, brief, instructions))
        return self.get(role, project_id)

    def delete(self, role: str, project_id: int | None = None) -> None:
        self.get(role, project_id)
        with self._connect() as db:
            if project_id is not None and self._projects_available(db):
                db.execute("DELETE FROM project_agents WHERE project_id=? AND role=?", (project_id, role))
            else:
                if db.execute("SELECT COUNT(*) FROM agent_definitions").fetchone()[0] <= 1:
                    raise ValueError("A team must keep at least one agent")
                db.execute("DELETE FROM agent_definitions WHERE role=?", (role,))

    def templates(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM agent_templates ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows]

    def save_template(self, role: str, template_name: str = "", project_id: int | None = None) -> dict:
        agent = self.get(role, project_id)
        name = template_name.strip() or f"{agent['name']} template"
        with self._connect() as db:
            cursor = db.execute("""INSERT INTO agent_templates(name,agent_name,role_hint,brief,instructions)
                VALUES(?,?,?,?,?)""", (name, agent["name"], agent["role"], agent["brief"], agent["instructions"]))
            row = db.execute("SELECT * FROM agent_templates WHERE id=?", (cursor.lastrowid,)).fetchone()
        return dict(row)

    def delete_project(self, project_id: int) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM project_agents WHERE project_id=?", (project_id,))
