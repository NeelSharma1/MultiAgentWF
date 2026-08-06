from __future__ import annotations

import json
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
                enforce_relationships INTEGER NOT NULL DEFAULT 0,
                auto_approve_agent_actions INTEGER NOT NULL DEFAULT 0,
                allow_full_system_access INTEGER NOT NULL DEFAULT 0,
                active_workflow_memory_id INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            project_columns = {row["name"] for row in db.execute("PRAGMA table_info(projects)")}
            if "enforce_relationships" not in project_columns:
                db.execute("ALTER TABLE projects ADD COLUMN enforce_relationships INTEGER NOT NULL DEFAULT 0")
            if "auto_approve_agent_actions" not in project_columns:
                db.execute("ALTER TABLE projects ADD COLUMN auto_approve_agent_actions INTEGER NOT NULL DEFAULT 0")
            if "allow_full_system_access" not in project_columns:
                db.execute("ALTER TABLE projects ADD COLUMN allow_full_system_access INTEGER NOT NULL DEFAULT 0")
            if "active_workflow_memory_id" not in project_columns:
                db.execute("ALTER TABLE projects ADD COLUMN active_workflow_memory_id INTEGER NOT NULL DEFAULT 0")
            db.execute("""CREATE TABLE IF NOT EXISTS project_agent_layout (
                project_id INTEGER NOT NULL, role TEXT NOT NULL, x REAL NOT NULL, y REAL NOT NULL,
                parent_role TEXT NOT NULL DEFAULT '', PRIMARY KEY(project_id, role),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE)""")
            db.execute("""CREATE TABLE IF NOT EXISTS project_agent_edges (
                project_id INTEGER NOT NULL, source_role TEXT NOT NULL, target_role TEXT NOT NULL,
                relationship TEXT NOT NULL CHECK(relationship IN ('command','report')),
                PRIMARY KEY(project_id, source_role, target_role, relationship),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE)""")
            db.execute("""CREATE TABLE IF NOT EXISTS workflow_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
                layout_json TEXT NOT NULL, edges_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            db.execute("""CREATE TABLE IF NOT EXISTS project_agent_permissions (
                project_id INTEGER NOT NULL, role TEXT NOT NULL,
                allow_commands INTEGER NOT NULL DEFAULT 0,
                allow_file_edits INTEGER NOT NULL DEFAULT 0,
                allow_full_system_access INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(project_id, role),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE)""")
            permission_columns = {row["name"] for row in db.execute("PRAGMA table_info(project_agent_permissions)")}
            if "allow_full_system_access" not in permission_columns:
                db.execute(
                    "ALTER TABLE project_agent_permissions ADD COLUMN allow_full_system_access INTEGER NOT NULL DEFAULT 0"
                )
            db.execute("""CREATE TABLE IF NOT EXISTS workflow_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL,
                name TEXT NOT NULL, content TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(project_id, name),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE)""")
            db.execute("""CREATE TABLE IF NOT EXISTS agent_permission_requests (
                message_id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, role TEXT NOT NULL,
                scope TEXT NOT NULL CHECK(scope IN ('workspace','external')),
                reason TEXT NOT NULL, commands_json TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','approved','denied')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                resolved_at TEXT,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE)""")
            request_columns = {row["name"] for row in db.execute("PRAGMA table_info(agent_permission_requests)")}
            if "commands_json" not in request_columns:
                db.execute(
                    "ALTER TABLE agent_permission_requests ADD COLUMN commands_json TEXT NOT NULL DEFAULT '[]'"
                )
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

    def set_relationship_enforcement(self, project_id: int, enabled: bool) -> dict[str, Any]:
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE projects SET enforce_relationships=? WHERE id=?", (int(bool(enabled)), project_id),
            )
        if not cursor.rowcount:
            raise KeyError(f"Project {project_id} not found")
        return self.get(project_id)

    def set_auto_approve_agent_actions(self, project_id: int, enabled: bool) -> dict[str, Any]:
        return self.set_action_policy(project_id, enabled)

    def set_action_policy(
        self, project_id: int, auto_approve_agent_actions: bool, allow_full_system_access: bool | None = None,
    ) -> dict[str, Any]:
        assignments = ["auto_approve_agent_actions=?"]
        values: list[Any] = [int(bool(auto_approve_agent_actions))]
        if allow_full_system_access is not None:
            assignments.append("allow_full_system_access=?")
            values.append(int(bool(allow_full_system_access)))
        values.append(project_id)
        with self._connect() as db:
            cursor = db.execute(
                f"UPDATE projects SET {', '.join(assignments)} WHERE id=?", values,
            )
        if not cursor.rowcount:
            raise KeyError(f"Project {project_id} not found")
        return self.get(project_id)

    def agent_action_permissions(self, project_id: int, role: str) -> dict[str, Any]:
        project = self.get(project_id)
        with self._connect() as db:
            row = db.execute(
                "SELECT allow_commands,allow_file_edits,allow_full_system_access "
                "FROM project_agent_permissions WHERE project_id=? AND role=?",
                (project_id, role),
            ).fetchone()
        commands = bool(row["allow_commands"]) if row else False
        file_edits = bool(row["allow_file_edits"]) if row else False
        agent_full_system_access = bool(row["allow_full_system_access"]) if row else False
        autonomous = bool(project.get("auto_approve_agent_actions"))
        full_system_access = bool(project.get("allow_full_system_access")) or agent_full_system_access
        return {
            "role": role,
            "allow_commands": commands,
            "allow_file_edits": file_edits,
            "allow_full_system_access": agent_full_system_access,
            "effective_commands": full_system_access or autonomous or commands,
            "effective_file_edits": full_system_access or autonomous or file_edits,
            "full_system_access": full_system_access,
        }

    def action_permissions(self, project_id: int, roles: list[str]) -> dict[str, Any]:
        project = self.get(project_id)
        return {
            "auto_approve_agent_actions": bool(project.get("auto_approve_agent_actions")),
            "allow_full_system_access": bool(project.get("allow_full_system_access")),
            "agents": [self.agent_action_permissions(project_id, role) for role in roles],
        }

    def set_agent_action_permissions(
        self, project_id: int, role: str, allow_commands: bool, allow_file_edits: bool,
        allow_full_system_access: bool | None = None,
    ) -> dict[str, Any]:
        self.get(project_id)
        with self._connect() as db:
            existing = db.execute(
                "SELECT allow_full_system_access FROM project_agent_permissions WHERE project_id=? AND role=?",
                (project_id, role),
            ).fetchone()
            full_system_access = (
                bool(existing["allow_full_system_access"]) if allow_full_system_access is None and existing else
                bool(allow_full_system_access)
            )
            db.execute(
                """INSERT INTO project_agent_permissions(
                project_id,role,allow_commands,allow_file_edits,allow_full_system_access
                ) VALUES(?,?,?,?,?) ON CONFLICT(project_id,role) DO UPDATE SET
                allow_commands=excluded.allow_commands,allow_file_edits=excluded.allow_file_edits,
                allow_full_system_access=excluded.allow_full_system_access""",
                (project_id, role, int(bool(allow_commands)), int(bool(allow_file_edits)), int(full_system_access)),
            )
        return self.agent_action_permissions(project_id, role)

    def record_permission_request(
        self, project_id: int, role: str, message_id: int, scope: str, reason: str, commands: list[str],
    ) -> dict[str, Any]:
        if scope not in {"workspace", "external"}:
            raise ValueError("Permission request scope must be workspace or external")
        clean_commands = [str(command).strip() for command in commands if str(command).strip()]
        with self._connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO agent_permission_requests(
                    message_id,project_id,role,scope,reason,commands_json
                ) VALUES(?,?,?,?,?,?)""",
                (message_id, project_id, role, scope, reason.strip(), json.dumps(clean_commands)),
            )
        request = self.permission_request(project_id, role, message_id)
        if request is None:
            raise RuntimeError("Could not save permission request")
        return request

    def permission_request(self, project_id: int, role: str, message_id: int) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT message_id,project_id,role,scope,reason,commands_json,status,created_at,resolved_at
                FROM agent_permission_requests WHERE project_id=? AND role=? AND message_id=?""",
                (project_id, role, message_id),
            ).fetchone()
        if not row:
            return None
        request = dict(row)
        try:
            request["commands"] = [str(item) for item in json.loads(request.pop("commands_json")) if str(item)]
        except (TypeError, ValueError, json.JSONDecodeError):
            request["commands"] = []
        return request

    def resolve_permission_request(
        self, project_id: int, role: str, message_id: int, approved: bool,
    ) -> dict[str, Any]:
        request = self.permission_request(project_id, role, message_id)
        if request is None:
            raise KeyError("Permission request was not found")
        if approved and not request["commands"]:
            raise ValueError("Permission request did not include any commands to approve")
        with self._connect() as db:
            cursor = db.execute(
                """UPDATE agent_permission_requests SET status=?,resolved_at=CURRENT_TIMESTAMP
                WHERE project_id=? AND role=? AND message_id=? AND status='pending'""",
                ("approved" if approved else "denied", project_id, role, message_id),
            )
        if not cursor.rowcount:
            request = self.permission_request(project_id, role, message_id)
            if request is None:
                raise KeyError("Permission request was not found")
            raise ValueError(f"Permission request was already {request['status']}")
        request = self.permission_request(project_id, role, message_id)
        if request is None:
            raise RuntimeError("Could not resolve permission request")
        return request

    def workflow_memories(self, project_id: int) -> dict[str, Any]:
        project = self.get(project_id)
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM workflow_memories WHERE project_id=? ORDER BY name COLLATE NOCASE, id", (project_id,),
            ).fetchall()
        return {
            "active_memory_id": int(project.get("active_workflow_memory_id") or 0),
            "memories": [dict(row) for row in rows],
        }

    def workflow_memory(self, project_id: int, memory_id: int) -> dict[str, Any] | None:
        if not memory_id:
            return None
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM workflow_memories WHERE project_id=? AND id=?", (project_id, memory_id),
            ).fetchone()
        return dict(row) if row else None

    def save_workflow_memory(self, project_id: int, name: str, content: str, memory_id: int | None = None) -> dict[str, Any]:
        self.get(project_id)
        title, body = name.strip(), content.strip()
        if not title or not body:
            raise ValueError("Workflow memory name and content are required")
        if len(title) > 120 or len(body) > 100_000:
            raise ValueError("Workflow memory is too large")
        try:
            with self._connect() as db:
                if memory_id is None:
                    cursor = db.execute(
                        "INSERT INTO workflow_memories(project_id,name,content) VALUES(?,?,?)",
                        (project_id, title, body),
                    )
                    memory_id = int(cursor.lastrowid)
                else:
                    cursor = db.execute(
                        """UPDATE workflow_memories SET name=?,content=?,updated_at=CURRENT_TIMESTAMP
                        WHERE project_id=? AND id=?""", (title, body, project_id, memory_id),
                    )
                    if not cursor.rowcount:
                        raise KeyError(f"Workflow memory {memory_id} not found")
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"A workflow memory named {title!r} already exists") from exc
        memory = self.workflow_memory(project_id, int(memory_id))
        assert memory is not None
        return memory

    def set_active_workflow_memory(self, project_id: int, memory_id: int) -> dict[str, Any]:
        self.get(project_id)
        if memory_id and self.workflow_memory(project_id, memory_id) is None:
            raise KeyError(f"Workflow memory {memory_id} not found")
        with self._connect() as db:
            db.execute("UPDATE projects SET active_workflow_memory_id=? WHERE id=?", (memory_id, project_id))
        return self.workflow_memories(project_id)

    def delete_workflow_memory(self, project_id: int, memory_id: int) -> None:
        with self._connect() as db:
            cursor = db.execute("DELETE FROM workflow_memories WHERE project_id=? AND id=?", (project_id, memory_id))
            if not cursor.rowcount:
                raise KeyError(f"Workflow memory {memory_id} not found")
            db.execute(
                "UPDATE projects SET active_workflow_memory_id=0 WHERE id=? AND active_workflow_memory_id=?",
                (project_id, memory_id),
            )

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

    def workflow_templates(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM workflow_templates ORDER BY name COLLATE NOCASE, id").fetchall()
        return [self._workflow_template(row) for row in rows]

    def save_workflow_template(
        self, project_id: int, name: str, layout: list[dict[str, Any]], edges: list[dict[str, str]], roles: list[str],
    ) -> dict[str, Any]:
        self.get(project_id)
        title = name.strip()
        if not title:
            raise ValueError("A workflow template name is required")
        if len(title) > 120:
            raise ValueError("Workflow template names are limited to 120 characters")
        saved_layout = self.save_layout(project_id, layout, roles)
        saved_edges = self.save_edges(project_id, edges, roles)
        try:
            with self._connect() as db:
                cursor = db.execute(
                    "INSERT INTO workflow_templates(name,layout_json,edges_json) VALUES(?,?,?)",
                    (title, json.dumps(saved_layout), json.dumps(saved_edges)),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"A workflow template named {title!r} already exists") from exc
        return self.workflow_template(int(cursor.lastrowid))

    def workflow_template(self, template_id: int) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM workflow_templates WHERE id=?", (template_id,)).fetchone()
        if not row:
            raise KeyError(f"Workflow template {template_id} not found")
        return self._workflow_template(row)

    def apply_workflow_template(self, project_id: int, template_id: int, roles: list[str]) -> dict[str, Any]:
        template = self.workflow_template(template_id)
        available = set(roles)
        template_roles = {item["role"] for item in template["layout"]}
        matching_layout = [item for item in template["layout"] if item["role"] in available]
        matching_edges = [
            edge for edge in template["edges"]
            if edge["source_role"] in available and edge["target_role"] in available
        ]
        layout = self.save_layout(project_id, matching_layout, roles)
        edges = self.save_edges(project_id, matching_edges, roles)
        return {
            "template": template,
            "layout": layout,
            "edges": edges,
            "skipped_roles": sorted(template_roles - available),
        }

    def delete_workflow_template(self, template_id: int) -> None:
        with self._connect() as db:
            cursor = db.execute("DELETE FROM workflow_templates WHERE id=?", (template_id,))
        if not cursor.rowcount:
            raise KeyError(f"Workflow template {template_id} not found")

    @staticmethod
    def _workflow_template(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["layout"] = json.loads(item.pop("layout_json"))
        item["edges"] = json.loads(item.pop("edges_json"))
        return item
