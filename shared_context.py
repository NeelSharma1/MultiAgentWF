from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


ROLES = ("orchestrator", "researcher", "programmer", "reviewer", "formatter", "documenter")


class ContextStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS context_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    roles TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL,
                    project_id INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(context_items)")}
            if "project_id" not in columns:
                db.execute("ALTER TABLE context_items ADD COLUMN project_id INTEGER NOT NULL DEFAULT 1")

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["roles"] = json.loads(result["roles"])
        return result

    def list(self, role: str | None = None, project_id: int = 1) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM context_items WHERE project_id=? ORDER BY updated_at DESC, id DESC",
                (project_id,),
            ).fetchall()
        items = [self._row(row) for row in rows]
        if role:
            items = [item for item in items if not item["roles"] or role in item["roles"]]
        return items

    def get(self, item_id: int, project_id: int = 1) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM context_items WHERE id = ? AND project_id = ?", (item_id, project_id)
            ).fetchone()
        return self._row(row) if row else None

    def save(self, title: str, content: str, roles: list[str], item_id: int | None = None,
             project_id: int = 1) -> dict[str, Any]:
        title, content = title.strip(), content.strip()
        if not title or not content:
            raise ValueError("Title and content are required")
        timestamp = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(sorted(set(roles)))
        with self._connect() as db:
            if item_id is None:
                cursor = db.execute(
                    "INSERT INTO context_items(title, content, roles, updated_at, project_id) VALUES (?, ?, ?, ?, ?)",
                    (title, content, payload, timestamp, project_id),
                )
                item_id = int(cursor.lastrowid)
            else:
                cursor = db.execute(
                    "UPDATE context_items SET title = ?, content = ?, roles = ?, updated_at = ? "
                    "WHERE id = ? AND project_id = ?",
                    (title, content, payload, timestamp, item_id, project_id),
                )
                if cursor.rowcount == 0:
                    raise KeyError(f"Context item {item_id} not found")
        return self.get(item_id, project_id)  # type: ignore[return-value]

    def delete(self, item_id: int, project_id: int = 1) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                "DELETE FROM context_items WHERE id = ? AND project_id = ?", (item_id, project_id)
            )
        return cursor.rowcount > 0

    def delete_project(self, project_id: int) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM context_items WHERE project_id=?", (project_id,))
