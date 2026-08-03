from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class ProjectStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '', root_path TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            db.execute("""CREATE TABLE IF NOT EXISTS project_agent_layout (
                project_id INTEGER NOT NULL, role TEXT NOT NULL, x REAL NOT NULL, y REAL NOT NULL,
                parent_role TEXT NOT NULL DEFAULT '', PRIMARY KEY(project_id, role),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE)""")
            db.execute("""CREATE TABLE IF NOT EXISTS project_agent_edges (
                project_id INTEGER NOT NULL, source_role TEXT NOT NULL, target_role TEXT NOT NULL,
                relationship TEXT NOT NULL CHECK(relationship IN ('command','report')),
                PRIMARY KEY(project_id, source_role, target_role, relationship),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE)""")
            db.execute("""INSERT OR IGNORE INTO project_agent_edges(project_id,source_role,target_role,relationship)
                SELECT project_id,role,parent_role,'report' FROM project_agent_layout WHERE parent_role != ''""")
            db.execute("UPDATE project_agent_layout SET parent_role='' WHERE parent_role != ''")
            if not db.execute("SELECT 1 FROM projects LIMIT 1").fetchone():
                db.execute("INSERT INTO projects(name, description, root_path) VALUES(?,?,?)",
                           ("MultiAgentWF", "The original multi-agent workspace", str(Path.cwd())))

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def list(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM projects ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows]

    def get(self, project_id: int) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            raise KeyError(f"Project {project_id} not found")
        return dict(row)

    def create(self, name: str, description: str = "", root_path: str = "") -> dict[str, Any]:
        name = name.strip()
        if not name:
            raise ValueError("Project name is required")
        root_path = str(Path(root_path).expanduser()) if root_path.strip() else ""
        with self._connect() as db:
            cursor = db.execute("INSERT INTO projects(name,description,root_path) VALUES(?,?,?)",
                                (name, description.strip(), root_path))
            project_id = int(cursor.lastrowid)
        return self.get(project_id)

    def delete(self, project_id: int) -> None:
        with self._connect() as db:
            cursor = db.execute("DELETE FROM projects WHERE id=?", (project_id,))
        if not cursor.rowcount:
            raise KeyError(f"Project {project_id} not found")

    def layout(self, project_id: int, roles: list[str]) -> list[dict[str, Any]]:
        self.get(project_id)
        with self._connect() as db:
            rows = {row["role"]: dict(row) for row in db.execute(
                "SELECT role,x,y FROM project_agent_layout WHERE project_id=?", (project_id,))}
        result = []
        for index, role in enumerate(roles):
            default = {"role": role, "x": 80 + (index % 3) * 250, "y": 55 + (index // 3) * 190}
            result.append(rows.get(role, default))
        return result

    def save_layout(self, project_id: int, items: list[dict[str, Any]], roles: list[str]) -> list[dict[str, Any]]:
        self.get(project_id)
        role_set = set(roles)
        for item in items:
            if item["role"] not in role_set:
                raise ValueError(f"Unknown agent role: {item['role']}")
            if not (0 <= float(item["x"]) <= 5000 and 0 <= float(item["y"]) <= 5000):
                raise ValueError("Agent position is outside the canvas")
        with self._connect() as db:
            for item in items:
                db.execute("""INSERT INTO project_agent_layout(project_id,role,x,y,parent_role)
                    VALUES(?,?,?,?,'') ON CONFLICT(project_id,role) DO UPDATE SET
                    x=excluded.x,y=excluded.y""",
                    (project_id, item["role"], float(item["x"]), float(item["y"])))
        return self.layout(project_id, roles)

    def edges(self, project_id: int) -> list[dict[str, str]]:
        self.get(project_id)
        with self._connect() as db:
            rows = db.execute("""SELECT source_role,target_role,relationship FROM project_agent_edges
                WHERE project_id=? ORDER BY relationship,source_role,target_role""", (project_id,)).fetchall()
        return [dict(row) for row in rows]

    def save_edges(self, project_id: int, edges: list[dict[str, str]], roles: list[str]) -> list[dict[str, str]]:
        self.get(project_id)
        role_set = set(roles)
        normalized = set()
        for edge in edges:
            source, target, relationship = edge["source_role"], edge["target_role"], edge["relationship"]
            if source not in role_set or target not in role_set:
                raise ValueError("Relationships must connect existing agents")
            if source == target:
                raise ValueError("An agent cannot connect to itself")
            if relationship not in {"command", "report"}:
                raise ValueError("Relationship must be command or report")
            normalized.add((source, target, relationship))
        with self._connect() as db:
            db.execute("DELETE FROM project_agent_edges WHERE project_id=?", (project_id,))
            db.executemany("""INSERT INTO project_agent_edges
                (project_id,source_role,target_role,relationship) VALUES(?,?,?,?)""",
                [(project_id, *edge) for edge in sorted(normalized)])
        return self.edges(project_id)

    def remove_agent(self, role: str, project_id: int | None = None) -> None:
        with self._connect() as db:
            if project_id is None:
                db.execute("DELETE FROM project_agent_layout WHERE role=?", (role,))
                db.execute("DELETE FROM project_agent_edges WHERE source_role=? OR target_role=?", (role, role))
            else:
                db.execute("DELETE FROM project_agent_layout WHERE project_id=? AND role=?", (project_id, role))
                db.execute(
                    "DELETE FROM project_agent_edges WHERE project_id=? AND (source_role=? OR target_role=?)",
                    (project_id, role, role),
                )
