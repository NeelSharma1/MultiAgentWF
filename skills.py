from __future__ import annotations

import asyncio
import io
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable, Iterator
from urllib.parse import urlparse

import httpx
import yaml


ACP_SKILL_FORMAT = "agent-skills/v1"
SKILL_MARKETPLACE_URL = "https://skillsmp.com"
SKILL_LANGUAGE_ALIASES = {
    "py": "python",
    "python3": "python",
    "js": "javascript",
    "node": "javascript",
    "mjs": "javascript",
    "cjs": "javascript",
    "sh": "shell",
    "bash": "shell",
    "zsh": "shell",
    "ps": "powershell",
    "ps1": "powershell",
    "rb": "ruby",
    "bat": "batch",
    "cmd": "batch",
    "md": "none",
    "markdown": "none",
}
SKILL_LANGUAGES = {"python", "javascript", "shell", "powershell", "ruby", "batch", "none"}
SKILL_TYPES = {
    "general",
    "development",
    "research",
    "documentation",
    "automation",
    "data",
    "security",
    "productivity",
    "frontend",
    "backend",
    "devops",
    "ai-agents",
    "other",
}
SKILL_PLATFORMS = {"any", "macos", "linux", "windows"}
SKILL_RUN_TIMEOUT = 30
SKILL_OUTPUT_LIMIT = 200_000
SKILL_SAFE_ENV_KEYS = {
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TMPDIR", "TMP", "TEMP", "LANG",
    "VIRTUAL_ENV", "PYTHONPATH", "SYSTEMROOT", "SystemRoot", "COMSPEC", "PATHEXT", "APPDATA",
    "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "GIT_CONFIG_NOSYSTEM",
}


def _configured_size_limit(env_name: str) -> int:
    """Read an optional marketplace archive limit in bytes.

    A value of ``0``, ``unlimited`` or an unset variable means no size limit.
    The limits remain configurable for deployments that want a resource cap,
    but large ACP skill packages are accepted by default.
    """
    raw = os.getenv(env_name, "").strip().lower()
    if not raw or raw in {"0", "none", "unlimited", "inf", "infinite"}:
        return 0
    try:
        megabytes = float(raw)
        if megabytes <= 0:
            return 0
        return int(megabytes * 1024 * 1024)
    except (ValueError, OverflowError):
        return 0


SKILL_DOWNLOAD_LIMIT = _configured_size_limit("SKILLS_MAX_DOWNLOAD_MB")
SKILL_UNCOMPRESSED_LIMIT = _configured_size_limit("SKILLS_MAX_UNCOMPRESSED_MB")
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SKILL_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
SKILL_SECRET_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
SKILL_RUNTIME_ENV_NAMES = {
    "SKILL_INPUT_JSON", "SKILL_NAME", "SKILL_SLUG", "SKILL_VERSION", "SKILL_PLATFORM",
}
SCRIPT_EXTENSIONS = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".ps1": "powershell",
    ".rb": "ruby",
    ".bat": "batch",
    ".cmd": "batch",
}


def normalize_skill_language(value: str) -> str:
    raw = str(value or "").strip().lower().removeprefix("language-")
    return SKILL_LANGUAGE_ALIASES.get(raw, raw or "none")


def normalize_skill_platform(value: str) -> str:
    raw = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "*": "any",
        "default": "any",
        "all": "any",
        "darwin": "macos",
        "mac": "macos",
        "osx": "macos",
        "win": "windows",
        "win32": "windows",
        "gnu-linux": "linux",
    }
    return aliases.get(raw, raw if raw in SKILL_PLATFORMS else "any")


def current_skill_platform() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    return "linux"


def normalize_skill_type(value: str) -> str:
    raw = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "docs": "documentation",
        "documenter": "documentation",
        "dev": "development",
        "ai": "ai-agents",
        "tools": "automation",
    }
    raw = aliases.get(raw, raw)
    return raw if raw in SKILL_TYPES else "other" if raw else "general"


def normalize_skill_version(value: str) -> str:
    raw = str(value or "").strip().removeprefix("v") or "1.0.0"
    if not SKILL_VERSION_RE.fullmatch(raw):
        raise ValueError("Skill version may contain only letters, numbers, dots, underscores, plus signs, and hyphens")
    return raw


def skill_slug(value: str) -> str:
    """Return the legacy-friendly local ID used by the app database."""
    return re.sub(r"[^a-z0-9_-]+", "_", str(value or "").strip().lower()).strip("_-")[:80]


def acp_skill_name(value: str) -> str:
    """Normalize an app ID to the portable Agent Skills name contract."""
    raw = re.sub(r"[_\s]+", "-", str(value or "").strip().lower())
    raw = re.sub(r"[^a-z0-9-]+", "-", raw).strip("-")
    raw = re.sub(r"-+", "-", raw)[:64].strip("-")
    if not raw or not SKILL_NAME_RE.fullmatch(raw):
        raise ValueError("ACP skill name must contain lowercase letters, numbers, and single hyphens")
    return raw


def _schema(value: Any, field_name: str) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field_name} must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a JSON object")
    return value


def _json_object(value: Any) -> dict[str, Any]:
    try:
        return _schema(value, "Schema")
    except ValueError:
        return {}


def normalize_skill_secret_refs(value: Any, *, field_name: str = "required_secrets") -> list[dict[str, Any]]:
    """Normalize secret *references* without accepting or persisting values.

    A package can declare a reference as ``"WEATHER_API_KEY"`` or as an
    object with ``name``, ``label``, ``description`` and ``required`` fields.
    Only those descriptive fields are kept in the ACP manifest; a marketplace
    package can never bring a credential value with it.
    """
    if value is None or value == "" or value == []:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field_name} must be a JSON array of secret references") from exc
    if isinstance(value, dict):
        # Be forgiving of a single declaration in an editor or imported
        # package, while keeping the persisted shape an array.
        value = [value]
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a JSON array of secret references")
    if len(value) > 100:
        raise ValueError(f"{field_name} may contain at most 100 references")
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, str):
            name, label, description, required = item, item, "", True
        elif isinstance(item, dict):
            name = item.get("name") or item.get("env") or item.get("key") or ""
            label = item.get("label") or name
            description = item.get("description") or ""
            required = item.get("required", True)
        else:
            raise ValueError(f"Each {field_name} item must be a string or object")
        name = str(name or "").strip().upper()
        if not SKILL_SECRET_NAME_RE.fullmatch(name):
            raise ValueError(f"Invalid skill secret name '{name or '<empty>'}'")
        if name in SKILL_RUNTIME_ENV_NAMES or name in {"PATH", "HOME", "PWD", "SHELL"}:
            raise ValueError(f"Skill secret name '{name}' is reserved by the runner")
        if name in seen:
            continue
        if isinstance(required, str):
            required = required.strip().lower() not in {"", "0", "false", "no", "off"}
        seen.add(name)
        refs.append({
            "name": name,
            "label": str(label or name).strip()[:160] or name,
            "description": str(description or "").strip()[:500],
            "required": bool(required),
        })
    return refs


def skill_secret_refs(skill: dict[str, Any]) -> list[dict[str, Any]]:
    """Read declarations from a public skill object or its ACP manifest."""
    direct = skill.get("required_secrets")
    if direct is not None:
        try:
            return normalize_skill_secret_refs(direct)
        except ValueError:
            return []
    manifest = skill.get("manifest") or {}
    metadata = manifest.get("metadata") if isinstance(manifest, dict) else {}
    if not isinstance(metadata, dict):
        return []
    raw = _metadata_value(metadata, "agent_team_required_secrets", "required_secrets", "secrets")
    try:
        return normalize_skill_secret_refs(raw)
    except ValueError:
        return []


def skill_secret_names(skill: dict[str, Any]) -> list[str]:
    return [ref["name"] for ref in skill_secret_refs(skill)]


def _version_key(version: str) -> tuple[Any, ...]:
    parts = re.split(r"[._+-]", str(version or ""))
    # Keep numeric and textual components comparable even when a package uses
    # versions such as ``1.0.0-rc1`` alongside stable numeric releases.
    return tuple((0, int(part)) if part.isdigit() else (1, part.lower()) for part in parts)


def _yaml_manifest(manifest: dict[str, Any], body: str) -> str:
    return "---\n" + yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True).strip() + "\n---\n\n" + body.strip() + "\n"


def parse_skill_md(text: str, *, expected_name: str = "") -> dict[str, Any]:
    """Parse and validate the portable SKILL.md frontmatter/body pair."""
    match = re.match(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)(.*)\Z", text, re.DOTALL)
    if not match:
        raise ValueError("ACP skill packages must start with YAML frontmatter in SKILL.md")
    try:
        frontmatter = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid SKILL.md YAML frontmatter: {exc}") from exc
    if not isinstance(frontmatter, dict):
        raise ValueError("SKILL.md frontmatter must be a YAML object")
    name = acp_skill_name(frontmatter.get("name") or expected_name)
    if expected_name and name != acp_skill_name(expected_name):
        raise ValueError(f"SKILL.md name '{name}' does not match the requested skill '{expected_name}'")
    description = str(frontmatter.get("description") or "").strip()
    if not description:
        raise ValueError("SKILL.md frontmatter requires a description")
    if len(description) > 1024:
        raise ValueError("SKILL.md description must be 1024 characters or fewer")
    metadata = frontmatter.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError("SKILL.md metadata must be a YAML object")
    metadata_values: dict[str, str] = {}
    for key, value in metadata.items():
        if isinstance(value, (dict, list)):
            metadata_values[str(key)] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        else:
            metadata_values[str(key)] = str(value)
    return {
        "name": name,
        "description": description,
        "license": str(frontmatter.get("license") or "").strip(),
        "compatibility": str(frontmatter.get("compatibility") or "").strip(),
        "allowed_tools": str(frontmatter.get("allowed-tools") or "").strip(),
        "metadata": metadata_values,
        "frontmatter": frontmatter,
        "body": match.group(2).strip(),
    }


def _metadata_value(metadata: dict[str, str], *keys: str, default: str = "") -> str:
    for key in keys:
        if str(metadata.get(key, "")).strip():
            return str(metadata[key]).strip()
    return default


def _script_extension(language: str) -> str:
    return {
        "python": ".py",
        "javascript": ".js",
        "shell": ".sh",
        "powershell": ".ps1",
        "ruby": ".rb",
        "batch": ".bat",
    }.get(normalize_skill_language(language), ".txt")


def _safe_relative_path(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("Skill resource paths must stay inside the skill package")
    return candidate


class SkillStore:
    """ACP/Agent-Skills package library plus project-scoped assignments.

    The SQLite database is now an index and assignment graph.  The portable
    skill itself lives at ``data/skills/<slug>/<version>/<platform>/SKILL.md``
    with optional resources and scripts beside it.  Legacy database-only
    skills are migrated into that package shape on first open.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self.library_root = Path(self.db_path).parent / "skills"
        self.library_root.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS skills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slug TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    input_schema TEXT NOT NULL DEFAULT '{}',
                    output_schema TEXT NOT NULL DEFAULT '{}',
                    language TEXT NOT NULL DEFAULT 'none',
                    script TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL DEFAULT 'human',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    skill_type TEXT NOT NULL DEFAULT 'general',
                    source TEXT NOT NULL DEFAULT 'local',
                    source_url TEXT NOT NULL DEFAULT '',
                    author TEXT NOT NULL DEFAULT '',
                    license TEXT NOT NULL DEFAULT '',
                    compatibility TEXT NOT NULL DEFAULT '',
                    output_format TEXT NOT NULL DEFAULT 'text',
                    package_path TEXT NOT NULL DEFAULT '',
                    acp_name TEXT NOT NULL DEFAULT ''
                )"""
            )
            self._ensure_columns(db)
            db.execute(
                """CREATE TABLE IF NOT EXISTS skill_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    skill_id INTEGER NOT NULL,
                    version TEXT NOT NULL,
                    platform TEXT NOT NULL DEFAULT 'any',
                    package_path TEXT NOT NULL,
                    manifest_json TEXT NOT NULL DEFAULT '{}',
                    body TEXT NOT NULL DEFAULT '',
                    input_schema TEXT NOT NULL DEFAULT '{}',
                    output_schema TEXT NOT NULL DEFAULT '{}',
                    language TEXT NOT NULL DEFAULT 'none',
                    script TEXT NOT NULL DEFAULT '',
                    output_format TEXT NOT NULL DEFAULT 'text',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(skill_id, version, platform),
                    FOREIGN KEY(skill_id) REFERENCES skills(id) ON DELETE CASCADE
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS project_skill_assignments (
                    project_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    skill_id INTEGER NOT NULL,
                    position INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(project_id, role, skill_id),
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                    FOREIGN KEY(skill_id) REFERENCES skills(id) ON DELETE CASCADE
                )"""
            )
        self._migrate_legacy_skills()

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

    @staticmethod
    def _ensure_columns(db: sqlite3.Connection) -> None:
        columns = {row[1] for row in db.execute("PRAGMA table_info(skills)").fetchall()}
        additions = {
            # Older workspace databases predate timestamps on skills.  Use a
            # constant default for ALTER TABLE (SQLite rejects CURRENT_TIMESTAMP
            # there), then backfill the existing rows below.
            "created_at": "TEXT NOT NULL DEFAULT ''",
            "updated_at": "TEXT NOT NULL DEFAULT ''",
            "skill_type": "TEXT NOT NULL DEFAULT 'general'",
            "source": "TEXT NOT NULL DEFAULT 'local'",
            "source_url": "TEXT NOT NULL DEFAULT ''",
            "author": "TEXT NOT NULL DEFAULT ''",
            "license": "TEXT NOT NULL DEFAULT ''",
            "compatibility": "TEXT NOT NULL DEFAULT ''",
            "output_format": "TEXT NOT NULL DEFAULT 'text'",
            "package_path": "TEXT NOT NULL DEFAULT ''",
            "acp_name": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in additions.items():
            if name not in columns:
                db.execute(f"ALTER TABLE skills ADD COLUMN {name} {definition}")
        db.execute("UPDATE skills SET created_at=CURRENT_TIMESTAMP WHERE created_at IS NULL OR created_at='' ")
        db.execute("UPDATE skills SET updated_at=created_at WHERE updated_at IS NULL OR updated_at='' ")

    @staticmethod
    def _row_value(row: sqlite3.Row | dict[str, Any], key: str, default: Any = "") -> Any:
        try:
            return row[key]  # type: ignore[index]
        except (KeyError, IndexError):
            return default

    def _relative_package_path(self, slug: str, version: str, platform: str) -> str:
        return str(Path(slug) / version / platform)

    def _package_dir(self, relative_path: str) -> Path:
        candidate = _safe_relative_path(relative_path)
        package = (self.library_root / candidate).resolve()
        library = self.library_root.resolve()
        if library != package and library not in package.parents:
            raise ValueError("Skill package path escaped the library")
        return package

    def _default_body(self, name: str, summary: str, inputs: dict[str, Any], outputs: dict[str, Any], script: str) -> str:
        body = [f"# {name}", "", summary]
        if inputs:
            body += ["", "## Inputs", "", "```json", json.dumps(inputs, indent=2, ensure_ascii=False), "```"]
        if outputs:
            body += ["", "## Outputs", "", "```json", json.dumps(outputs, indent=2, ensure_ascii=False), "```"]
        if script:
            body += ["", "## Execution", "", "If execution is needed, use the bundled script with one JSON object on stdin."]
        return "\n".join(body)

    def _manifest(
        self,
        *,
        acp_name: str,
        summary: str,
        version: str,
        platform: str,
        skill_type: str,
        language: str,
        inputs: dict[str, Any],
        outputs: dict[str, Any],
        output_format: str,
        license_name: str,
        compatibility: str,
        allowed_tools: str,
        metadata: dict[str, Any] | None,
        required_secrets: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        custom = {
            "agent_team_protocol": "acp",
            "agent_team_type": skill_type,
            "agent_team_version": version,
            "agent_team_platform": platform,
            "agent_team_language": normalize_skill_language(language),
            "agent_team_output_format": output_format or "text",
            "agent_team_input_schema": json.dumps(inputs, ensure_ascii=False, separators=(",", ":")),
            "agent_team_output_schema": json.dumps(outputs, ensure_ascii=False, separators=(",", ":")),
        }
        for key, value in (metadata or {}).items():
            if str(key).strip().lower() in {"required_secrets", "agent_team_required_secrets", "secrets"}:
                continue
            custom[str(key)] = str(value)
        # Keep only non-sensitive declarations in the portable package.  The
        # actual values live in the local credential store and are never part
        # of SKILL.md, SQLite metadata, prompts, or marketplace archives.
        custom["agent_team_required_secrets"] = json.dumps(
            normalize_skill_secret_refs(required_secrets or []), ensure_ascii=False, separators=(",", ":")
        )
        result: dict[str, Any] = {
            "name": acp_name,
            "description": summary[:1024],
            "metadata": custom,
        }
        if license_name:
            result["license"] = license_name
        if compatibility:
            result["compatibility"] = compatibility[:500]
        if allowed_tools:
            result["allowed-tools"] = allowed_tools
        return result

    def _write_package(
        self,
        *,
        slug: str,
        name: str,
        summary: str,
        version: str,
        platform: str,
        skill_type: str,
        language: str,
        script: str,
        inputs: dict[str, Any],
        outputs: dict[str, Any],
        output_format: str,
        license_name: str,
        compatibility: str,
        allowed_tools: str,
        metadata: dict[str, Any] | None,
        required_secrets: list[dict[str, Any]] | None = None,
        body: str,
    ) -> tuple[str, dict[str, Any], str]:
        version = normalize_skill_version(version)
        platform = normalize_skill_platform(platform)
        acp_name = acp_skill_name(slug)
        package_rel = self._relative_package_path(slug, version, platform)
        package_dir = self._package_dir(package_rel)
        package_dir.mkdir(parents=True, exist_ok=True)
        body = str(body or "").strip() or self._default_body(name, summary, inputs, outputs, script)
        manifest = self._manifest(
            acp_name=acp_name,
            summary=summary,
            version=version,
            platform=platform,
            skill_type=skill_type,
            language=language,
            inputs=inputs,
            outputs=outputs,
            output_format=output_format,
            license_name=license_name,
            compatibility=compatibility,
            allowed_tools=allowed_tools,
            metadata=metadata,
            required_secrets=required_secrets,
        )
        (package_dir / "SKILL.md").write_text(_yaml_manifest(manifest, body), encoding="utf-8")
        scripts_dir = package_dir / "scripts"
        scripts_dir.mkdir(exist_ok=True)
        for existing in scripts_dir.glob("run.*"):
            existing.unlink(missing_ok=True)
        if script.strip():
            (scripts_dir / f"run{_script_extension(language)}").write_text(script.strip() + "\n", encoding="utf-8")
        return package_rel, manifest, body

    def _migrate_legacy_skills(self) -> None:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM skills").fetchall()
            for row in rows:
                if db.execute("SELECT 1 FROM skill_versions WHERE skill_id=? LIMIT 1", (row["id"],)).fetchone():
                    continue
                slug = skill_slug(row["slug"]) or skill_slug(row["name"]) or f"skill_{row['id']}"
                version, platform = "1.0.0", "any"
                inputs, outputs = _json_object(row["input_schema"]), _json_object(row["output_schema"])
                language = normalize_skill_language(row["language"])
                package_rel, manifest, body = self._write_package(
                    slug=slug, name=row["name"], summary=row["summary"], version=version, platform=platform,
                    skill_type=normalize_skill_type(self._row_value(row, "skill_type", "general")),
                    language=language, script=row["script"], inputs=inputs, outputs=outputs,
                    output_format=self._row_value(row, "output_format", "text"),
                    license_name=self._row_value(row, "license", ""), compatibility=self._row_value(row, "compatibility", ""),
                    allowed_tools="", metadata=None, required_secrets=[], body="",
                )
                db.execute(
                    """INSERT INTO skill_versions
                       (skill_id,version,platform,package_path,manifest_json,body,input_schema,output_schema,language,script,output_format)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (row["id"], version, platform, package_rel, json.dumps(manifest), body,
                     json.dumps(inputs), json.dumps(outputs), language, row["script"], self._row_value(row, "output_format", "text")),
                )
                db.execute(
                    "UPDATE skills SET package_path=?,acp_name=?,slug=?,skill_type=?,source=? WHERE id=?",
                    (package_rel, acp_skill_name(slug), slug, normalize_skill_type(self._row_value(row, "skill_type", "general")),
                     self._row_value(row, "source", "local") or "local", row["id"]),
                )

    def _select_version(self, rows: list[sqlite3.Row], platform: str = "") -> sqlite3.Row | None:
        if not rows:
            return None
        wanted = normalize_skill_platform(platform or current_skill_platform())
        preferred = [row for row in rows if row["platform"] == wanted]
        fallback = [row for row in rows if row["platform"] == "any"]
        candidates = preferred or fallback or rows
        return sorted(candidates, key=lambda row: _version_key(row["version"]), reverse=True)[0]

    def _versions(self, skill_id: int, *, include_body: bool = False) -> list[dict[str, Any]]:
        with self._connect() as db:
            columns = "id,version,platform,package_path,language,output_format,updated_at,manifest_json"
            if include_body:
                columns += ",body,script,input_schema,output_schema"
            rows = db.execute(
                f"SELECT {columns} FROM skill_versions "
                "WHERE skill_id=? ORDER BY version DESC, platform",
                (skill_id,),
            ).fetchall()
        versions = []
        for row in rows:
            item = dict(row)
            manifest = json.loads(item.pop("manifest_json", "{}") or "{}")
            item["required_secrets"] = skill_secret_refs({"manifest": manifest})
            if include_body:
                item["body"] = item.get("body", "")
                item["script"] = item.get("script", "")
                item["inputs"] = _json_object(item.pop("input_schema", "{}"))
                item["outputs"] = _json_object(item.pop("output_schema", "{}"))
            versions.append(item)
        return sorted(versions, key=lambda item: (_version_key(item.get("version", "")), item.get("platform", "")), reverse=True)

    def _public(self, row: sqlite3.Row | dict[str, Any], *, project_id: int | None = None,
                role: str | None = None, platform: str = "", include_body: bool = True) -> dict[str, Any]:
        skill_id = int(row["id"])
        with self._connect() as db:
            variants = db.execute("SELECT * FROM skill_versions WHERE skill_id=?", (skill_id,)).fetchall()
            assignments = db.execute(
                "SELECT project_id,role,position FROM project_skill_assignments WHERE skill_id=? "
                "AND (? IS NULL OR project_id=?) ORDER BY position,role",
                (skill_id, project_id, project_id),
            ).fetchall()
        selected = self._select_version(list(variants), platform)
        base = dict(row)
        result: dict[str, Any] = {
            "id": skill_id,
            "slug": base["slug"],
            "acp_name": base.get("acp_name") or acp_skill_name(base["slug"]),
            "name": base["name"],
            "summary": base["summary"],
            "description": base["summary"],
            "skill_type": normalize_skill_type(base.get("skill_type", "general")),
            "type": normalize_skill_type(base.get("skill_type", "general")),
            "source": base.get("source", "local") or "local",
            "source_url": base.get("source_url", "") or "",
            "author": base.get("author", "") or "",
            "license": base.get("license", "") or "",
            "compatibility": base.get("compatibility", "") or "",
            "allowed_tools": "",
            "assigned_roles": [assignment["role"] for assignment in assignments],
            "assigned": bool(role and any(assignment["role"] == role for assignment in assignments)),
            "created_at": base.get("created_at", ""),
            "updated_at": base.get("updated_at", ""),
            "versions": self._versions(skill_id, include_body=include_body),
            "format": ACP_SKILL_FORMAT,
            "protocol": "acp",
            "is_acp": True,
            "required_secrets": [],
        }
        if selected:
            manifest = json.loads(selected["manifest_json"] or "{}")
            result.update({
                "version": selected["version"],
                "platform": selected["platform"],
                "package_path": str(self._package_dir(selected["package_path"])),
                "inputs": _json_object(selected["input_schema"]),
                "outputs": _json_object(selected["output_schema"]),
                "language": normalize_skill_language(selected["language"]),
                "output_format": selected["output_format"] or "text",
                "manifest": manifest,
                "required_secrets": skill_secret_refs({"manifest": manifest}),
            })
            result["allowed_tools"] = str(manifest.get("allowed-tools") or "")
            if include_body:
                result["script"] = selected["script"]
                result["body"] = selected["body"]
                result["skill_md"] = (self._package_dir(selected["package_path"]) / "SKILL.md").read_text(
                    encoding="utf-8", errors="replace"
                )
        else:
            result.update({
                "version": "1.0.0", "platform": "any", "package_path": base.get("package_path", ""),
                "inputs": _json_object(base.get("input_schema", "{}")), "outputs": _json_object(base.get("output_schema", "{}")),
                "language": normalize_skill_language(base.get("language", "none")),
                "output_format": base.get("output_format", "text") or "text", "manifest": {},
            })
            if include_body:
                result["script"] = base.get("script", "")
        return result

    def _validate(
        self, name: str, summary: str, language: str, script: str, inputs: Any, outputs: Any, slug: str = "",
        *, version: str = "1.0.0", platform: str = "any", skill_type: str = "general",
    ) -> tuple[str, str, str, str, dict[str, Any], dict[str, Any], str, str, str]:
        name, summary, script = str(name).strip(), str(summary).strip(), str(script or "").strip()
        if not name or not summary:
            raise ValueError("Skill name and description are required")
        language = normalize_skill_language(language)
        if language not in SKILL_LANGUAGES:
            raise ValueError(f"Unsupported skill language: {language or 'none'}")
        local_slug = skill_slug(slug or name)
        if not local_slug or not re.fullmatch(r"[a-z][a-z0-9_-]{1,79}", local_slug):
            raise ValueError("Skill ID must be 2–80 lowercase letters, numbers, hyphens, or underscores")
        acp_skill_name(local_slug)
        return (
            name, summary[:1024], language, local_slug, _schema(inputs, "Inputs"), _schema(outputs, "Outputs"),
            normalize_skill_version(version), normalize_skill_platform(platform), normalize_skill_type(skill_type),
        )

    def save(
        self, name: str, summary: str, inputs: Any, outputs: Any, language: str, script: str = "",
        slug: str = "", skill_id: int | None = None, created_by: str = "human", *, version: str = "1.0.0",
        platform: str = "any", skill_type: str = "general", body: str = "", output_format: str = "text",
        license_name: str = "", compatibility: str = "", allowed_tools: str = "", source: str = "local",
        source_url: str = "", author: str = "", metadata: dict[str, Any] | None = None,
        required_secrets: Any = None,
    ) -> dict[str, Any]:
        name, summary, language, local_slug, input_schema, output_schema, version, platform, skill_type = self._validate(
            name, summary, language, script, inputs, outputs, slug, version=version, platform=platform, skill_type=skill_type
        )
        body = str(body or "")
        license_name = str(license_name or "")
        compatibility = str(compatibility or "")
        allowed_tools = str(allowed_tools or "")
        source = str(source or "local")
        source_url = str(source_url or "")
        author = str(author or "")
        output_format = str(output_format or "text").strip().lower()[:40] or "text"
        if required_secrets is None:
            required_secrets = _metadata_value(
                metadata or {}, "agent_team_required_secrets", "required_secrets", "secrets"
            )
        required_secrets = normalize_skill_secret_refs(required_secrets)
        timestamp = datetime.now(timezone.utc).isoformat()
        package_rel, manifest, body = self._write_package(
            slug=local_slug, name=name, summary=summary, version=version, platform=platform, skill_type=skill_type,
            language=language, script=script, inputs=input_schema, outputs=output_schema, output_format=output_format,
            license_name=license_name, compatibility=compatibility, allowed_tools=allowed_tools, metadata=metadata,
            required_secrets=required_secrets, body=body,
        )
        acp_name = acp_skill_name(local_slug)
        with self._connect() as db:
            duplicate = db.execute(
                "SELECT id FROM skills WHERE slug=? AND (? IS NULL OR id != ?)", (local_slug, skill_id, skill_id)
            ).fetchone()
            if duplicate:
                raise ValueError(f"Skill ID '{local_slug}' already exists")
            if skill_id is None:
                cursor = db.execute(
                    """INSERT INTO skills
                       (slug,name,summary,input_schema,output_schema,language,script,created_by,updated_at,
                        skill_type,source,source_url,author,license,compatibility,output_format,package_path,acp_name)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (local_slug, name, summary, json.dumps(input_schema), json.dumps(output_schema), language, script,
                     str(created_by or "human").strip(), timestamp, skill_type, source or "local", source_url or "",
                     author or "", license_name or "", compatibility or "", output_format, package_rel, acp_name),
                )
                skill_id = int(cursor.lastrowid)
            else:
                if not db.execute("SELECT id FROM skills WHERE id=?", (skill_id,)).fetchone():
                    raise KeyError(f"Skill {skill_id} not found")
                db.execute(
                    """UPDATE skills SET slug=?,name=?,summary=?,input_schema=?,output_schema=?,language=?,script=?,
                       created_by=?,updated_at=?,skill_type=?,source=?,source_url=?,author=?,license=?,compatibility=?,
                       output_format=?,package_path=?,acp_name=? WHERE id=?""",
                    (local_slug, name, summary, json.dumps(input_schema), json.dumps(output_schema), language, script,
                     str(created_by or "human").strip(), timestamp, skill_type, source or "local", source_url or "",
                     author or "", license_name or "", compatibility or "", output_format, package_rel, acp_name, skill_id),
                )
            db.execute(
                """INSERT INTO skill_versions
                   (skill_id,version,platform,package_path,manifest_json,body,input_schema,output_schema,language,script,output_format,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(skill_id,version,platform) DO UPDATE SET package_path=excluded.package_path,
                   manifest_json=excluded.manifest_json,body=excluded.body,input_schema=excluded.input_schema,
                   output_schema=excluded.output_schema,language=excluded.language,script=excluded.script,
                   output_format=excluded.output_format,updated_at=excluded.updated_at""",
                (skill_id, version, platform, package_rel, json.dumps(manifest), body, json.dumps(input_schema),
                 json.dumps(output_schema), language, script, output_format, timestamp),
            )
        return self.get(int(skill_id), platform=platform)  # type: ignore[return-value]

    def get(self, skill_id: int, *, platform: str = "", include_body: bool = True,
            project_id: int | None = None, role: str | None = None) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM skills WHERE id=?", (skill_id,)).fetchone()
        return self._public(row, project_id=project_id, role=role, platform=platform, include_body=include_body) if row else None

    def get_by_slug(self, slug: str, *, platform: str = "") -> dict[str, Any] | None:
        local_slug = skill_slug(slug)
        with self._connect() as db:
            row = db.execute("SELECT * FROM skills WHERE slug=? OR acp_name=?", (local_slug, acp_skill_name(slug))).fetchone()
        return self._public(row, platform=platform) if row else None

    def list(
        self, project_id: int | None = None, role: str | None = None, *, query: str = "", skill_type: str = "",
        sort: str = "name", platform: str = "",
    ) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM skills").fetchall()
        needle = query.strip().lower()
        wanted_type = normalize_skill_type(skill_type) if skill_type else ""
        result = []
        for row in rows:
            item = self._public(row, project_id=project_id, role=role, platform=platform, include_body=False)
            haystack = " ".join(str(item.get(key, "")) for key in ("name", "summary", "slug", "author", "source")).lower()
            if needle and needle not in haystack:
                continue
            if wanted_type and item["type"] != wanted_type:
                continue
            result.append(item)
        if sort == "type":
            result.sort(key=lambda item: (item["type"], item["name"].lower()))
        elif sort == "recent":
            result.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        elif sort == "source":
            result.sort(key=lambda item: (item["source"], item["name"].lower()))
        else:
            result.sort(key=lambda item: item["name"].lower())
        return result

    def assigned(self, project_id: int, role: str) -> list[dict[str, Any]]:
        return [item for item in self.list(project_id, role) if item["assigned"]]

    def is_assigned(self, skill_id: int, project_id: int, role: str) -> bool:
        with self._connect() as db:
            return bool(db.execute(
                "SELECT 1 FROM project_skill_assignments WHERE skill_id=? AND project_id=? AND role=?",
                (skill_id, project_id, role),
            ).fetchone())

    def assign(self, skill_id: int, project_id: int, roles: list[str]) -> list[dict[str, Any]]:
        if not self.get(skill_id, include_body=False):
            raise KeyError(f"Skill {skill_id} not found")
        clean_roles = list(dict.fromkeys(role.strip() for role in roles if role.strip()))
        with self._connect() as db:
            db.execute("DELETE FROM project_skill_assignments WHERE project_id=? AND skill_id=?", (project_id, skill_id))
            db.executemany(
                "INSERT INTO project_skill_assignments(project_id,role,skill_id,position) VALUES(?,?,?,?)",
                [(project_id, role, skill_id, position) for position, role in enumerate(clean_roles)],
            )
        return self.list(project_id)

    def delete(self, skill_id: int) -> None:
        with self._connect() as db:
            paths = [row["package_path"] for row in db.execute("SELECT package_path FROM skill_versions WHERE skill_id=?", (skill_id,))]
            cursor = db.execute("DELETE FROM skills WHERE id=?", (skill_id,))
        if cursor.rowcount == 0:
            raise KeyError(f"Skill {skill_id} not found")
        for path in paths:
            package = self._package_dir(path)
            if package.exists():
                shutil.rmtree(package)

    def remove_agent(self, project_id: int, role: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM project_skill_assignments WHERE project_id=? AND role=?", (project_id, role))

    def summaries(self, project_id: int, role: str) -> list[dict[str, Any]]:
        return [
            {
                "id": item["id"], "slug": item["slug"], "acp_name": item["acp_name"], "name": item["name"],
                "summary": item["summary"], "description": item["description"], "type": item["type"],
                "version": item.get("version", "1.0.0"), "platform": item.get("platform", "any"),
                "package_path": item.get("package_path", ""), "inputs": item.get("inputs", {}),
                "outputs": item.get("outputs", {}), "output_format": item.get("output_format", "text"),
                "required_secrets": item.get("required_secrets", []),
                "format": ACP_SKILL_FORMAT, "protocol": "acp",
            }
            for item in self.assigned(project_id, role)
        ]

    def load_assigned(self, skill_id: int, project_id: int, role: str, *, platform: str = "") -> dict[str, Any] | None:
        if not self.is_assigned(skill_id, project_id, role):
            return None
        skill = self.get(skill_id, platform=platform)
        if not skill:
            return None
        return {
            "id": skill["id"], "name": skill["name"], "slug": skill["slug"], "format": ACP_SKILL_FORMAT,
            "protocol": "acp",
            "version": skill["version"], "platform": skill["platform"], "manifest": skill["manifest"],
            "required_secrets": skill.get("required_secrets", []),
            "body": skill.get("body", ""), "skill_md": skill.get("skill_md", ""),
        }

    def read_resource(self, skill_id: int, project_id: int, role: str, relative_path: str) -> dict[str, Any]:
        if not self.is_assigned(skill_id, project_id, role):
            return {"ok": False, "error": "Skill is not assigned to this agent in this workspace"}
        skill = self.get(skill_id, include_body=False)
        if not skill:
            return {"ok": False, "error": f"Skill {skill_id} was not found"}
        relative = _safe_relative_path(relative_path)
        path = (Path(skill["package_path"]) / relative).resolve()
        package = Path(skill["package_path"]).resolve()
        if package != path and package not in path.parents:
            return {"ok": False, "error": "Skill resource path escaped the package"}
        if not path.is_file():
            return {"ok": False, "error": f"Skill resource '{relative_path}' was not found"}
        if path.stat().st_size > SKILL_OUTPUT_LIMIT:
            return {"ok": False, "error": "Skill resource is too large to return"}
        return {"ok": True, "skill_id": skill_id, "path": str(relative), "content": path.read_text(encoding="utf-8", errors="replace")}

    def materialize(self, project_root: Path, project_id: int, role: str) -> list[dict[str, Any]]:
        destination_root = project_root.resolve() / ".agents" / "skills"
        destination_root.mkdir(parents=True, exist_ok=True)
        materialized = []
        for summary in self.summaries(project_id, role):
            skill = self.get(summary["id"], include_body=False)
            if not skill or not skill.get("package_path"):
                continue
            source = Path(skill["package_path"]).resolve()
            destination = destination_root / skill["acp_name"]
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(source, destination)
            materialized.append({"id": skill["id"], "name": skill["name"], "path": str(destination)})
        return materialized

    def package_archive(self, skill_id: int, *, platform: str = "") -> bytes:
        skill = self.get(skill_id, platform=platform, include_body=False)
        if not skill:
            raise KeyError(f"Skill {skill_id} not found")
        root = Path(skill["package_path"]).resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"Skill package does not exist: {root}")
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in root.rglob("*"):
                if path.is_file():
                    archive.write(path, Path(skill["acp_name"]) / path.relative_to(root))
        return output.getvalue()

    def _script_candidates(self, root: Path) -> list[tuple[str, Path]]:
        candidates: list[tuple[str, Path]] = []
        scripts_root = root / "scripts"
        if not scripts_root.is_dir():
            return candidates
        for path in sorted(scripts_root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in SCRIPT_EXTENSIONS:
                continue
            platform = "any"
            for part in path.relative_to(scripts_root).parts[:-1]:
                normalized = normalize_skill_platform(part)
                if normalized != "any":
                    platform = normalized
            candidates.append((platform, path))
        # A package can contain helper modules alongside its entrypoint.  Pick
        # one run.* entrypoint per platform and leave the rest as resources.
        selected: dict[str, Path] = {}
        for platform, path in candidates:
            current = selected.get(platform)
            if current is None or (path.name.lower().startswith("run.") and not current.name.lower().startswith("run.")):
                selected[platform] = path
        return [(platform, path) for platform, path in sorted(selected.items())]

    def import_skill_directory(
        self, root: str | Path, *, source: str = "import", source_url: str = "", marketplace_id: str = "",
        expected_name: str = "",
    ) -> dict[str, Any]:
        source_root = Path(root).resolve()
        manifest_path = source_root / "SKILL.md"
        if not manifest_path.is_file():
            raise ValueError("The imported package does not contain SKILL.md at its root")
        parsed = parse_skill_md(manifest_path.read_text(encoding="utf-8", errors="replace"), expected_name=expected_name)
        metadata = parsed["metadata"]
        local_slug = skill_slug(parsed["name"]) or parsed["name"].replace("-", "_")
        version = normalize_skill_version(_metadata_value(metadata, "version", "agent_team_version", default="1.0.0"))
        skill_type = normalize_skill_type(_metadata_value(metadata, "type", "category", "agent_team_type", default="general"))
        input_schema = _json_object(_metadata_value(metadata, "input_schema", "agent_team_input_schema"))
        output_schema = _json_object(_metadata_value(metadata, "output_schema", "agent_team_output_schema"))
        output_format = _metadata_value(metadata, "output_format", "agent_team_output_format", default="text")
        language = normalize_skill_language(_metadata_value(metadata, "language", "agent_team_language", default="none"))
        required_secrets = normalize_skill_secret_refs(
            _metadata_value(metadata, "agent_team_required_secrets", "required_secrets", "secrets")
        )
        scripts = self._script_candidates(source_root)
        if scripts:
            language = SCRIPT_EXTENSIONS[scripts[0][1].suffix.lower()]
        variants = scripts or [(normalize_skill_platform(_metadata_value(metadata, "platform", "agent_team_platform")), None)]
        existing = self.get_by_slug(local_slug)
        saved: dict[str, Any] | None = None
        safe_metadata = {
            key: value for key, value in metadata.items()
            if str(key).strip().lower() not in {"required_secrets", "agent_team_required_secrets", "secrets"}
        }
        if marketplace_id:
            safe_metadata["marketplace_id"] = marketplace_id
        for platform, script_path in variants:
            selected_script = script_path.read_text(encoding="utf-8", errors="replace") if script_path else ""
            variant_language = SCRIPT_EXTENSIONS.get(script_path.suffix.lower(), language) if script_path else language
            saved = self.save(
                parsed["name"].replace("-", " ").title(), parsed["description"], input_schema, output_schema,
                variant_language, selected_script, local_slug, existing["id"] if existing else None, "marketplace",
                version=version, platform=platform, skill_type=skill_type, body=parsed["body"], output_format=output_format,
                license_name=parsed["license"], compatibility=parsed["compatibility"], allowed_tools=parsed["allowed_tools"],
                source=source, source_url=source_url, author=_metadata_value(metadata, "author", default=""),
                required_secrets=required_secrets,
                metadata=safe_metadata,
            )
            destination = Path(saved["package_path"])
            shutil.copytree(source_root, destination, dirs_exist_ok=True)
            # Re-write the manifest produced by the store instead of copying
            # marketplace frontmatter verbatim. This strips any accidental
            # secret-value fields while preserving the imported body/resources.
            sanitized_manifest = saved.get("manifest") or {}
            (destination / "SKILL.md").write_text(
                _yaml_manifest(sanitized_manifest, parsed["body"]), encoding="utf-8"
            )
            existing = saved
        if not saved:
            raise ValueError("The imported ACP skill did not contain a usable version")
        return self.get(saved["id"]) or saved

    async def search_marketplace(
        self, query: str, *, category: str = "", sort: str = "stars", page: int = 1, limit: int = 20,
    ) -> dict[str, Any]:
        query = query.strip()
        if not query:
            raise ValueError("Marketplace search requires a query")
        params: dict[str, Any] = {"q": query, "page": max(1, page), "limit": min(50, max(1, limit)), "sortBy": sort if sort in {"stars", "recent"} else "stars"}
        if category:
            params["category"] = category
        headers = {"User-Agent": "MultiAgentWF/0.1 (Agent Skills client)"}
        api_key = os.getenv("SKILLSMP_API_KEY", "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        base_url = (os.getenv("SKILLS_MARKETPLACE_URL", "").strip() or SKILL_MARKETPLACE_URL).rstrip("/")
        async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers=headers) as client:
            response = await client.get(f"{base_url}/api/v1/skills/search", params=params)
            if response.status_code >= 400:
                try:
                    detail = response.json().get("error", {}).get("message", response.text)
                except (ValueError, AttributeError):
                    detail = response.text
                raise ValueError(f"Skills marketplace returned HTTP {response.status_code}: {detail}")
            payload = response.json()
        data = payload.get("data", payload)
        skills = []
        for item in data.get("skills", []) if isinstance(data, dict) else []:
            skills.append({
                "id": str(item.get("id", "")), "name": item.get("name", ""), "author": item.get("author", ""),
                "description": item.get("description", ""), "type": item.get("category", "marketplace") or "marketplace",
                "content_language": item.get("contentLanguage", ""), "github_url": item.get("githubUrl", ""),
                "skill_url": item.get("skillUrl", ""), "stars": item.get("stars", 0), "updated_at": item.get("updatedAt", 0),
            })
        return {"skills": skills, "pagination": data.get("pagination", {}) if isinstance(data, dict) else {}}

    async def install_marketplace_skill(
        self, source_url: str, *, marketplace_id: str = "", expected_name: str = "",
        progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        parsed = urlparse(source_url)
        if parsed.hostname not in {"github.com", "www.github.com"}:
            raise ValueError("Marketplace installs currently accept GitHub repository URLs only")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 2:
            raise ValueError("Marketplace source URL must identify a GitHub repository")
        owner, repository = parts[0], parts[1].removesuffix(".git")
        archive_url = f"https://api.github.com/repos/{owner}/{repository}/zipball"
        async with httpx.AsyncClient(timeout=45, follow_redirects=True, headers={"User-Agent": "MultiAgentWF/0.1"}) as client:
            if progress is None or not hasattr(client, "stream"):
                response = await client.get(archive_url)
                response.raise_for_status()
                if SKILL_DOWNLOAD_LIMIT and len(response.content) > SKILL_DOWNLOAD_LIMIT:
                    limit_mb = SKILL_DOWNLOAD_LIMIT / (1024 * 1024)
                    raise ValueError(f"Marketplace skill archive is larger than the configured {limit_mb:g} MB limit")
                archive_bytes = response.content
                if progress is not None:
                    await progress({"phase": "download", "received": len(archive_bytes), "total": len(archive_bytes)})
            else:
                chunks: list[bytes] = []
                received = 0
                async with client.stream("GET", archive_url) as response:
                    response.raise_for_status()
                    content_length = response.headers.get("content-length", "")
                    try:
                        total_download = max(0, int(content_length))
                    except (TypeError, ValueError):
                        total_download = 0
                    await progress({"phase": "download", "received": 0, "total": total_download})
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        chunks.append(chunk)
                        received += len(chunk)
                        if SKILL_DOWNLOAD_LIMIT and received > SKILL_DOWNLOAD_LIMIT:
                            limit_mb = SKILL_DOWNLOAD_LIMIT / (1024 * 1024)
                            raise ValueError(f"Marketplace skill archive is larger than the configured {limit_mb:g} MB limit")
                        await progress({"phase": "download", "received": received, "total": total_download})
                archive_bytes = b"".join(chunks)
                if total_download <= 0:
                    await progress({"phase": "download", "received": received, "total": received})
        with tempfile.TemporaryDirectory(prefix="agent-team-marketplace-") as temp_dir:
            unpack_root = Path(temp_dir) / "package"
            unpack_root.mkdir()
            total = 0
            try:
                with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
                    members = archive.infolist()
                    uncompressed_total = sum(member.file_size for member in members)
                    if progress is not None:
                        await progress({"phase": "extract", "received": 0, "total": uncompressed_total})
                    extracted = 0
                    for member in members:
                        relative = PurePosixPath(member.filename)
                        if relative.is_absolute() or ".." in relative.parts:
                            raise ValueError("Marketplace archive contained an unsafe path")
                        total += member.file_size
                        if SKILL_UNCOMPRESSED_LIMIT and total > SKILL_UNCOMPRESSED_LIMIT:
                            limit_mb = SKILL_UNCOMPRESSED_LIMIT / (1024 * 1024)
                            raise ValueError(
                                f"Marketplace skill archive expands beyond the configured {limit_mb:g} MB limit"
                            )
                        target = unpack_root.joinpath(*relative.parts).resolve()
                        if unpack_root.resolve() not in target.parents and target != unpack_root.resolve():
                            raise ValueError("Marketplace archive escaped its extraction folder")
                        if member.is_dir():
                            target.mkdir(parents=True, exist_ok=True)
                        else:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_bytes(archive.read(member))
                        extracted += member.file_size
                        if progress is not None:
                            await progress({"phase": "extract", "received": extracted, "total": uncompressed_total})
            except zipfile.BadZipFile as exc:
                raise ValueError("Marketplace source did not return a valid ZIP skill repository") from exc
            candidates = list(unpack_root.rglob("SKILL.md"))
            if not candidates:
                raise ValueError("Marketplace repository does not contain a SKILL.md skill package")
            expected = skill_slug(expected_name or "")
            candidates.sort(key=lambda path: (0 if expected and skill_slug(path.parent.name) == expected else 1, len(path.parts)))
            if progress is not None:
                await progress({"phase": "install", "received": 0, "total": 0})
            installed = self.import_skill_directory(
                candidates[0].parent, source="marketplace", source_url=source_url,
                marketplace_id=marketplace_id, expected_name=expected_name or "",
            )
            if progress is not None:
                await progress({"phase": "complete", "received": 1, "total": 1})
                installed = self.get(installed["id"], include_body=False) or installed
            return installed


def skill_command(language: str, script: str) -> list[str]:
    language = normalize_skill_language(language)
    if not script.strip():
        raise ValueError("This ACP skill contains instructions/resources but no executable script")
    if language == "python":
        return [sys.executable, "-c", script]
    if language == "javascript":
        executable = shutil.which("node")
        if not executable:
            raise ValueError("Node.js is not installed or is not available on the server PATH")
        return [executable, "-e", script]
    if language == "shell":
        executable = shutil.which("sh")
        if not executable:
            raise ValueError("A POSIX shell is not available on the server PATH")
        return [executable, "-c", script]
    if language == "powershell":
        executable = shutil.which("pwsh") or shutil.which("powershell")
        if not executable:
            raise ValueError("PowerShell is not installed or is not available on the server PATH")
        return [executable, "-NoProfile", "-NonInteractive", "-Command", script]
    if language == "ruby":
        executable = shutil.which("ruby")
        if not executable:
            raise ValueError("Ruby is not installed or is not available on the server PATH")
        return [executable, "-e", script]
    if language == "batch":
        if os.name != "nt":
            raise ValueError("Batch skills can only run on Windows")
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", script]
    raise ValueError(f"Unsupported executable skill language: {language or 'none'}")


def _kill_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        process.kill()
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()


def _skill_environment(
    skill: dict[str, Any], secret_values: dict[str, str] | None, cwd: Path,
) -> tuple[dict[str, str], list[str], dict[str, str]]:
    """Build a minimal child environment and inject only declared secrets."""
    refs = skill_secret_refs(skill)
    provided: dict[str, str] = {}
    supplied = secret_values or {}
    for ref in refs:
        name = ref["name"]
        value = supplied.get(name)
        if value is None:
            value = os.getenv(name)
        if value is not None and str(value).strip():
            provided[name] = str(value)
    missing = [ref["name"] for ref in refs if ref.get("required", True) and ref["name"] not in provided]
    environment = {
        key: value for key, value in os.environ.items()
        if key in SKILL_SAFE_ENV_KEYS or key.startswith("LC_")
    }
    environment.update(provided)
    environment["PWD"] = str(cwd)
    return environment, missing, provided


def _redact_skill_output(value: str, secret_values: dict[str, str]) -> str:
    """Prevent a helper that accidentally prints a credential from echoing it."""
    redacted = value
    for secret in secret_values.values():
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def run_skill_script(
    skill: dict[str, Any], inputs: dict[str, Any], cwd: Path, *, secret_values: dict[str, str] | None = None,
) -> dict[str, Any]:
    if not isinstance(inputs, dict):
        raise ValueError("Skill inputs must be a JSON object")
    schema = skill.get("inputs") or {}
    required = schema.get("required", []) if isinstance(schema, dict) else []
    missing = [key for key in required if key not in inputs]
    if missing:
        raise ValueError(f"Missing required skill inputs: {', '.join(map(str, missing))}")
    environment, missing_secrets, resolved_secrets = _skill_environment(skill, secret_values, cwd)
    if missing_secrets:
        raise ValueError(
            "Missing required skill secrets: " + ", ".join(missing_secrets) + ". "
            "Configure them in the Skills editor before running this skill."
        )
    if not str(skill.get("script", "")).strip():
        return {
            "ok": False, "skill_id": skill.get("id"), "skill": skill.get("name", "skill"),
            "version": skill.get("version", "1.0.0"), "platform": skill.get("platform", current_skill_platform()),
            "output_format": skill.get("output_format", "text"), "language": skill.get("language", "none"),
            "cwd": str(cwd), "exit_code": None, "timed_out": False, "output": None, "stdout": "",
            "stderr": "This ACP skill has no executable script; load its SKILL.md instructions instead.",
            "required_secrets": [ref["name"] for ref in skill_secret_refs(skill)], "missing_secrets": [],
        }
    command = skill_command(skill["language"], skill["script"])
    serialized = json.dumps(inputs, ensure_ascii=False)
    environment.update({
        "SKILL_INPUT_JSON": serialized, "SKILL_NAME": str(skill.get("name", "skill")),
        "SKILL_SLUG": str(skill.get("slug", "")), "SKILL_VERSION": str(skill.get("version", "1.0.0")),
        "SKILL_PLATFORM": str(skill.get("platform", current_skill_platform())),
    })
    process = subprocess.Popen(
        command, cwd=str(cwd), env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, start_new_session=os.name != "nt",
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(serialized, timeout=SKILL_RUN_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        _kill_process(process)
        stdout, stderr = process.communicate()
        stdout = stdout or (exc.stdout or "")
        stderr = stderr or (exc.stderr or "")
    if isinstance(stdout, bytes):
        stdout = stdout.decode(errors="replace")
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="replace")
    stdout = _redact_skill_output((stdout or ""), resolved_secrets)[:SKILL_OUTPUT_LIMIT]
    stderr = _redact_skill_output((stderr or ""), resolved_secrets)[:SKILL_OUTPUT_LIMIT]
    if timed_out:
        stderr = f"{stderr}\nSkill timed out after {SKILL_RUN_TIMEOUT} seconds.".strip()
    raw = stdout.strip()
    try:
        output: Any = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        output = raw
    return {
        "ok": process.returncode == 0 and not timed_out, "skill_id": skill.get("id"),
        "skill": skill.get("name", "skill"), "version": skill.get("version", "1.0.0"),
        "platform": skill.get("platform", current_skill_platform()), "output_format": skill.get("output_format", "text"),
        "language": skill.get("language", "none"), "cwd": str(cwd), "exit_code": process.returncode,
        "timed_out": timed_out, "output": output, "stdout": stdout, "stderr": stderr,
        "required_secrets": [ref["name"] for ref in skill_secret_refs(skill)], "missing_secrets": [],
    }


async def run_skill_script_async(
    skill: dict[str, Any], inputs: dict[str, Any], cwd: Path, *, secret_values: dict[str, str] | None = None,
) -> dict[str, Any]:
    return await asyncio.to_thread(run_skill_script, skill, inputs, cwd, secret_values=secret_values)
