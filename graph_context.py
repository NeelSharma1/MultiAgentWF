"""Persistent project/file graph metadata.

The graph is intentionally small and provider-neutral: a project is scanned into
project, folder, and file nodes, and each node stores canonical keywords and a
fixed-size numeric fingerprint. Provider-generated metadata can replace the
deterministic fingerprint without changing the storage or graph shape.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

VECTOR_SIZE = 16
ROLES = (
    "researcher",
    "coder",
    "architect",
    "reviewer",
    "orchestrator",
    "engineering_narrative_steward",
)
MAX_TEXT_BYTES = 128_000
MAX_KEYWORDS = 24
DEFAULT_IGNORED_NAMES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        "data",
        ".env",
        ".env.local",
    }
)
TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")
STOPWORDS = frozenset(
    {
        "about",
        "after",
        "again",
        "also",
        "from",
        "have",
        "into",
        "only",
        "that",
        "their",
        "there",
        "these",
        "this",
        "with",
    }
)


class GraphContextError(ValueError):
    """Raised when graph metadata or a project path is invalid."""


def safe_relative_path(project_root: Path, requested: str | None) -> tuple[Path, str]:
    """Resolve a client path without allowing it to escape ``project_root``."""

    root = project_root.expanduser().resolve()
    raw_text = requested or "."
    raw = Path(raw_text)
    if raw.is_absolute() or "\\" in raw_text or "\x00" in raw_text:
        raise GraphContextError("path must be relative to the project root")
    candidate = (root / raw).resolve()
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise GraphContextError("path escapes project root") from exc
    return candidate, relative.as_posix() or "."


def _keywords(text: str) -> list[str]:
    counts: dict[str, int] = {}
    for token in TOKEN_RE.findall(text.lower()):
        if token in STOPWORDS:
            continue
        counts[token] = counts.get(token, 0) + 1
    return sorted(counts, key=lambda token: (-counts[token], token))[:MAX_KEYWORDS]


def _vector(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8", "ignore")).digest()
    values = []
    for index in range(0, VECTOR_SIZE * 2, 2):
        value = int.from_bytes(digest[index : index + 2], "big") / 65535.0
        values.append(round(value * 2.0 - 1.0, 6))
    return values


def deterministic_graph_metadata(path: str, source: str) -> dict[str, list[Any]]:
    """Return stable, local metadata when a provider is unavailable."""

    material = f"{path}\n{source}"
    return {"keywords": _keywords(material), "vector": _vector(material)}


def _canonical_keywords(values: Iterable[str]) -> list[str]:
    cleaned = {
        str(value).strip().lower()
        for value in values
        if str(value).strip()
    }
    return sorted(cleaned)[:MAX_KEYWORDS]


def _canonical_vector(values: Iterable[float]) -> list[float]:
    vector = [float(value) for value in values]
    if len(vector) != VECTOR_SIZE or not all(math.isfinite(value) for value in vector):
        raise ValueError(f"vector must contain {VECTOR_SIZE} finite numbers")
    return vector


class GraphContextStore:
    """SQLite graph store shared by the web app, agents, and MCP tools."""

    VECTOR_DIMENSIONS = VECTOR_SIZE
    MAX_FILES = 5_000
    MAX_BYTES = 8 * 1024 * 1024
    MAX_TEXT_CHARS = MAX_TEXT_BYTES

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            legacy = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='context_items'"
            ).fetchone()
            archived = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='context_items_legacy'"
            ).fetchone()
            if legacy and not archived:
                db.execute("ALTER TABLE context_items RENAME TO context_items_legacy")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS graph_nodes (
                    id TEXT PRIMARY KEY,
                    project_id INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    name TEXT NOT NULL,
                    node_type TEXT NOT NULL CHECK(node_type IN ('project', 'folder', 'file')),
                    parent_id TEXT,
                    keywords TEXT NOT NULL DEFAULT '[]',
                    vector TEXT NOT NULL DEFAULT '[]',
                    content_hash TEXT,
                    size INTEGER,
                    modified_at REAL,
                    source TEXT NOT NULL DEFAULT 'scanner',
                    model TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(project_id, path),
                    FOREIGN KEY(parent_id) REFERENCES graph_nodes(id) ON DELETE CASCADE
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_graph_nodes_project_path "
                "ON graph_nodes(project_id, path)"
            )
            columns = {
                row["name"]
                for row in db.execute("PRAGMA table_info(graph_nodes)").fetchall()
            }
            if "source" not in columns:
                db.execute("ALTER TABLE graph_nodes ADD COLUMN source TEXT NOT NULL DEFAULT 'scanner'")
            if "model" not in columns:
                db.execute("ALTER TABLE graph_nodes ADD COLUMN model TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["keywords"] = json.loads(result["keywords"] or "[]")
        result["vector"] = json.loads(result["vector"] or "[]")
        result["vector_hash"] = hashlib.sha256(
            json.dumps(result["vector"], separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return result

    def list_nodes(
        self, project_id: int, parent_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._connect() as db:
            if parent_id is None:
                rows = db.execute(
                    "SELECT * FROM graph_nodes WHERE project_id=? ORDER BY path",
                    (project_id,),
                ).fetchall()
            else:
                rows = db.execute(
                    """
                    SELECT * FROM graph_nodes
                    WHERE project_id=? AND parent_id=?
                    ORDER BY path
                    """,
                    (project_id, parent_id),
                ).fetchall()
        return [self._decode(row) for row in rows]  # type: ignore[list-item]

    def list(self, project_id: int = 1, path: str | None = None) -> list[dict[str, Any]]:
        """Compatibility-shaped graph listing used by the app and tests."""

        nodes = self.list_nodes(project_id)
        selected = self.normalize_path(path)
        if not selected:
            return nodes
        prefix = f"{selected}/"
        return [node for node in nodes if node["path"] == selected or node["path"].startswith(prefix)]

    def get_node(self, node_id: str, project_id: int) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM graph_nodes WHERE id=? AND project_id=?",
                (node_id, project_id),
            ).fetchone()
        return self._decode(row)

    def get(self, node_id: str, project_id: int = 1) -> dict[str, Any] | None:
        """Return one node; retained for the MCP read-tool shape."""

        return self.get_node(str(node_id), project_id)

    def get_path(self, project_id: int, path: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM graph_nodes WHERE project_id=? AND path=?",
                (project_id, path),
            ).fetchone()
        return self._decode(row)

    def get_by_path(
        self, path: str, project_id: int = 1
    ) -> dict[str, Any] | None:
        return self.get_path(project_id, path)

    @staticmethod
    def normalize_path(value: str | None) -> str:
        """Normalize a client-relative graph path without filesystem access."""

        raw = (value or "").strip()
        lowered = raw.lower()
        if (
            raw.startswith(("/", "~"))
            or "\\" in raw
            or "\x00" in raw
            or "%2e" in lowered
            or "%2f" in lowered
            or "%5c" in lowered
        ):
            raise GraphContextError("path must be relative")
        parts = [part for part in raw.split("/") if part not in {"", "."}]
        if any(part == ".." for part in parts):
            raise GraphContextError("path traversal is not allowed")
        return "/".join(parts)

    @staticmethod
    def fallback_metadata(text: str) -> dict[str, list[Any]]:
        return deterministic_graph_metadata("", text)

    @staticmethod
    def normalize_metadata(metadata: dict[str, Any], material: str = "") -> dict[str, Any]:
        """Validate provider output and safely fall back to deterministic metadata."""

        fallback = deterministic_graph_metadata("", material)
        try:
            keywords = _canonical_keywords(metadata.get("keywords") or [])
            vector = _canonical_vector(metadata.get("vector") or [])
        except (TypeError, ValueError):
            keywords, vector = fallback["keywords"], fallback["vector"]
        return {
            "keywords": keywords or fallback["keywords"],
            "vector": vector,
            "source": str(metadata.get("source") or "deterministic")[:40],
            "model": str(metadata.get("model") or "")[:160],
        }

    @staticmethod
    def resolve_project_root(value: str | Path) -> Path:
        root = Path(value).expanduser().resolve()
        if not root.is_dir():
            raise GraphContextError("configured project folder does not exist")
        return root

    def upsert_node(
        self,
        *,
        project_id: int,
        path: str,
        name: str,
        node_type: str,
        parent_id: str | None = None,
        keywords: Iterable[str] = (),
        vector: Iterable[float] = (),
        content_hash: str | None = None,
        size: int | None = None,
        modified_at: float | None = None,
        source: str = "scanner",
        model: str = "",
    ) -> dict[str, Any]:
        if node_type not in {"project", "folder", "file"}:
            raise ValueError("invalid graph node type")
        now = time.time()
        node_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"graph:{project_id}:{path}"))
        keyword_json = json.dumps(_canonical_keywords(keywords))
        vector_values = list(vector)
        vector_json = json.dumps(
            _canonical_vector(vector_values) if vector_values else []
        )
        with self._connect() as db:
            existing = db.execute(
                "SELECT keywords, vector, content_hash, source, model "
                "FROM graph_nodes WHERE project_id=? AND path=?",
                (project_id, path),
            ).fetchone()
            # A refresh should update structure and content fingerprints, not
            # discard an AI or user-authored representation for unchanged data.
            if (
                existing is not None
                and existing["content_hash"] == content_hash
                and existing["source"] in {"provider", "manual", "agent"}
            ):
                keyword_json = existing["keywords"]
                vector_json = existing["vector"]
                source = existing["source"]
                model = existing["model"]
            db.execute(
                """
                INSERT INTO graph_nodes
                    (id, project_id, path, name, node_type, parent_id,
                     keywords, vector, content_hash, size, modified_at, source, model,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, path) DO UPDATE SET
                    name=excluded.name,
                    node_type=excluded.node_type,
                    parent_id=excluded.parent_id,
                    keywords=excluded.keywords,
                    vector=excluded.vector,
                    content_hash=excluded.content_hash,
                    size=excluded.size,
                    modified_at=excluded.modified_at,
                    source=excluded.source,
                    model=excluded.model,
                    updated_at=excluded.updated_at
                """,
                (
                    node_id,
                    project_id,
                    path,
                    name,
                    node_type,
                    parent_id,
                    keyword_json,
                    vector_json,
                    content_hash,
                    size,
                    modified_at,
                    source[:40],
                    model[:160],
                    now,
                    now,
                ),
            )
        result = self.get_node(node_id, project_id)
        if result is None:
            raise RuntimeError("graph node was not persisted")
        return result

    def update_metadata(
        self,
        node_id: str,
        project_id: int,
        *,
        keywords: Iterable[str] | None = None,
        vector: Iterable[float] | None = None,
        source: str = "manual",
        model: str = "",
    ) -> dict[str, Any] | None:
        current = self.get_node(str(node_id), project_id)
        if current is None:
            return None
        keyword_json = json.dumps(_canonical_keywords(keywords if keywords is not None else current["keywords"]))
        vector_json = json.dumps(_canonical_vector(vector if vector is not None else current["vector"]))
        with self._connect() as db:
            db.execute(
                """
                UPDATE graph_nodes
                SET keywords=?, vector=?, source=?, model=?, updated_at=?
                WHERE id=? AND project_id=?
                """,
                (keyword_json, vector_json, source[:40], model[:160], time.time(), str(node_id), project_id),
            )
        return self.get_node(node_id, project_id)

    def update_metadata_for_path(
        self,
        *,
        project_id: int,
        path: str,
        keywords: Iterable[str],
        vector: Iterable[float],
    ) -> dict[str, Any] | None:
        node = self.get_path(project_id, path)
        if node is None:
            return None
        return self.update_metadata(
            node_id=node["id"],
            project_id=project_id,
            keywords=keywords,
            vector=vector,
        )

    def delete_node(self, node_id: str, project_id: int) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                "DELETE FROM graph_nodes WHERE id=? AND project_id=?",
                (node_id, project_id),
            )
        return cursor.rowcount > 0

    def delete(self, node_id: str, project_id: int = 1) -> bool:
        return self.delete_node(str(node_id), project_id)

    def delete_project(self, project_id: int) -> int:
        with self._connect() as db:
            cursor = db.execute(
                "DELETE FROM graph_nodes WHERE project_id=?", (project_id,)
            )
        return cursor.rowcount

    def apply_generated_metadata(
        self,
        node_id: str,
        project_id: int,
        expected_content_hash: str | None,
        metadata: dict[str, Iterable[Any]],
    ) -> dict[str, Any] | None:
        """Apply AI metadata only when the scanned file has not changed."""

        node = self.get_node(node_id, project_id)
        if node is None or node.get("content_hash") != expected_content_hash:
            return None
        return self.update_metadata(
            node_id=node_id,
            project_id=project_id,
            keywords=metadata.get("keywords", []),
            vector=metadata.get("vector", []),
            source=str(metadata.get("source") or "provider"),
            model=str(metadata.get("model") or ""),
        )

    def content_for_node(self, project_root: Path, node: dict[str, Any]) -> str:
        """Return bounded, safe source material for metadata generation."""

        root = self.resolve_project_root(project_root)
        path = self.normalize_path(str(node.get("path") or ""))
        if node.get("node_type") == "file":
            target, _ = safe_relative_path(root, path or ".")
            if not target.is_file() or target.is_symlink():
                raise GraphContextError("graph file is no longer available")
            data = target.read_bytes()[:MAX_TEXT_BYTES]
            if b"\0" in data:
                return f"Binary file: {path}"
            return data.decode("utf-8", "ignore")
        children = self.list(node["project_id"], path)
        lines = [f"{item['node_type']}: {item['path'] or '.'}" for item in children[:250]]
        return "\n".join(lines) or f"{node.get('node_type', 'project')}: ."

    def scan(
        self,
        project_root: Path,
        project_id: int = 1,
        max_files: int = MAX_FILES,
        *,
        ignored_names: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        root = self.resolve_project_root(project_root)
        max_files = max(1, min(int(max_files), self.MAX_FILES))
        ignored = DEFAULT_IGNORED_NAMES | frozenset(ignored_names or set())

        root_metadata = deterministic_graph_metadata("", root.name)
        self.upsert_node(
            project_id=project_id,
            path="",
            name=root.name or ".",
            node_type="project",
            keywords=root_metadata["keywords"],
            vector=root_metadata["vector"],
        )

        paths = sorted(
            (
                path
                for path in root.rglob("*")
                if not path.is_symlink()
                and not any(
                    part in ignored for part in path.relative_to(root).parts
                )
            ),
            key=lambda path: (len(path.relative_to(root).parts), path.as_posix()),
        )
        scanned_files = 0
        for path in paths:
            relative = path.relative_to(root).as_posix()
            parent_path = "" if "/" not in relative else relative.rsplit("/", 1)[0]
            parent = self.get_path(project_id, parent_path)
            parent_id = parent["id"] if parent else None
            if path.is_dir():
                metadata = deterministic_graph_metadata(relative, relative)
                self.upsert_node(
                    project_id=project_id,
                    path=relative,
                    name=path.name,
                    node_type="folder",
                    parent_id=parent_id,
                    keywords=metadata["keywords"],
                    vector=metadata["vector"],
                )
                continue
            if not path.is_file():
                continue
            if scanned_files >= max_files:
                break
            try:
                data = path.read_bytes()[:MAX_TEXT_BYTES]
                stat = path.stat()
            except OSError:
                continue
            source = "" if b"\0" in data else data.decode("utf-8", "ignore")
            metadata = deterministic_graph_metadata(relative, source)
            self.upsert_node(
                project_id=project_id,
                path=relative,
                name=path.name,
                node_type="file",
                parent_id=parent_id,
                keywords=metadata["keywords"],
                vector=metadata["vector"],
                content_hash=hashlib.sha256(data).hexdigest(),
                size=stat.st_size,
                modified_at=stat.st_mtime,
                source="scanner",
            )
            scanned_files += 1
        nodes = self.list_nodes(project_id)
        return {
            "nodes": nodes,
            "files": sum(node["node_type"] == "file" for node in nodes),
            "folders": sum(node["node_type"] == "folder" for node in nodes),
            "projects": sum(node["node_type"] == "project" for node in nodes),
        }
