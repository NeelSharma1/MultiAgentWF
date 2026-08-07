from __future__ import annotations

import sqlite3
import uuid
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from shared_context import ROLES


PROVIDERS = {
    "openai": {
        "name": "OpenAI API",
        "default_model": "gpt-5-mini",
        "api_key_env": "OPENAI_API_KEY",
        "description": "OpenAI Responses API and native Agents SDK tools.",
    },
    "codex": {
        "name": "Codex subscription",
        "default_model": "",
        "api_key_env": "",
        "description": "Local Codex CLI authenticated with your ChatGPT account.",
    },
    "google": {
        "name": "Google Gemini",
        "default_model": "gemini-flash-latest",
        "api_key_env": "GEMINI_API_KEY",
        "description": "Gemini through Google's OpenAI-compatible endpoint.",
    },
    "anthropic": {
        "name": "Anthropic Claude",
        "default_model": "claude-sonnet-4-6",
        "api_key_env": "ANTHROPIC_API_KEY",
        "description": "Claude through Anthropic's OpenAI SDK compatibility endpoint.",
    },
    "compatible": {
        "name": "OpenAI-compatible",
        "default_model": "",
        "api_key_env": "LOCAL_API_KEY",
        "description": "Ollama, LM Studio, vLLM, or another compatible server.",
    },
}

INTER_AGENT_MESSAGE_KINDS = {"command", "report"}

# A compiled command is stored as plain text so it remains portable across
# providers and app versions.  The UI recognizes this small envelope and
# renders it as a command card, while provider adapters unwrap it before
# building a prompt.  Keep the legacy XML-like envelope below readable so
# messages created before this format change can still be delivered.
COMPILED_COMMAND_PREFIX = "COMPILED COMMAND REQUEST:"
COMPILED_COMMAND_SUFFIX = "END COMPILED COMMAND REQUEST"
LEGACY_COMPILED_COMMAND_PREFIX = "<compiled_command_request>"
LEGACY_COMPILED_COMMAND_SUFFIX = "</compiled_command_request>"


DEFAULTS = {
    role: {
        "role": role,
        "provider": "codex" if role in {"programmer", "reviewer"} else "openai",
        "model": "" if role in {"programmer", "reviewer"} else "gpt-5-mini",
        "base_url": "",
        "api_key_env": "",
    }
    for role in ROLES
}


class RuntimeConfigStore:
    def __init__(self, db_path: str | Path, *, recover_interrupted_runs: bool = True) -> None:
        """Open the runtime store.

        Only the main application process should perform process-restart
        recovery.  Provider helpers such as the MCP server share this
        database while an agent run is active; treating one of those helper
        processes as the application owner would incorrectly mark the live
        run as interrupted.
        """
        self.db_path = str(db_path)
        self.recover_interrupted_runs = recover_interrupted_runs
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_runtime_config (
                    role TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    base_url TEXT NOT NULL,
                    api_key_env TEXT NOT NULL,
                    reasoning_effort TEXT NOT NULL DEFAULT '',
                    context_window_tokens INTEGER NOT NULL DEFAULT 128000,
                    context_compaction_threshold INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS project_agent_runtime_config (
                    project_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    base_url TEXT NOT NULL,
                    api_key_env TEXT NOT NULL,
                    reasoning_effort TEXT NOT NULL DEFAULT '',
                    context_window_tokens INTEGER NOT NULL DEFAULT 128000,
                    context_compaction_threshold INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (project_id, role),
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                )
                """
            )
            # Google can keep retired models in the catalog even when inference rejects them.
            # Migrate both legacy global and project-scoped profiles; the latter
            # is what active chats normally read.
            for table in ("agent_runtime_config", "project_agent_runtime_config"):
                db.execute(
                    f"""UPDATE {table} SET model = 'models/gemini-flash-lite-latest'
                    WHERE provider = 'google' AND model IN (
                        'models/gemini-2.5-flash-lite', 'gemini-2.5-flash-lite'
                    )"""
                )
            for table in ("agent_runtime_config", "project_agent_runtime_config"):
                columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                if "reasoning_effort" not in columns:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN reasoning_effort TEXT NOT NULL DEFAULT ''")
                if "context_window_tokens" not in columns:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN context_window_tokens INTEGER NOT NULL DEFAULT 128000")
                if "context_compaction_threshold" not in columns:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN context_compaction_threshold INTEGER NOT NULL DEFAULT 0")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role TEXT NOT NULL,
                    speaker TEXT NOT NULL,
                    content TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    project_id INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    reply_to_id INTEGER,
                    source_role TEXT NOT NULL DEFAULT '',
                    message_kind TEXT NOT NULL DEFAULT '',
                    delivery_status TEXT NOT NULL DEFAULT '',
                    delivered_at TEXT,
                    delivery_run_id TEXT
                )
                """
            )
            message_columns = {row[1] for row in db.execute("PRAGMA table_info(conversation_messages)")}
            if "project_id" not in message_columns:
                db.execute("ALTER TABLE conversation_messages ADD COLUMN project_id INTEGER NOT NULL DEFAULT 1")
            if "reply_to_id" not in message_columns:
                db.execute("ALTER TABLE conversation_messages ADD COLUMN reply_to_id INTEGER")
            if "source_role" not in message_columns:
                db.execute("ALTER TABLE conversation_messages ADD COLUMN source_role TEXT NOT NULL DEFAULT ''")
            if "message_kind" not in message_columns:
                db.execute("ALTER TABLE conversation_messages ADD COLUMN message_kind TEXT NOT NULL DEFAULT ''")
            if "delivery_status" not in message_columns:
                db.execute("ALTER TABLE conversation_messages ADD COLUMN delivery_status TEXT NOT NULL DEFAULT ''")
            if "delivered_at" not in message_columns:
                db.execute("ALTER TABLE conversation_messages ADD COLUMN delivered_at TEXT")
            if "delivery_run_id" not in message_columns:
                db.execute("ALTER TABLE conversation_messages ADD COLUMN delivery_run_id TEXT")
            db.execute(
                """CREATE INDEX IF NOT EXISTS idx_conversation_agent_inbox
                ON conversation_messages(project_id, role, speaker, message_kind, delivery_status, id)"""
            )
            self._coalesce_pending_commands(db)
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_attachments (
                    id TEXT PRIMARY KEY,
                    project_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    message_id INTEGER,
                    name TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS codex_chat_sessions (
                    project_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT '',
                    reasoning_effort TEXT NOT NULL DEFAULT '',
                    compacted_message_id INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (project_id, role)
                )
                """
            )
            session_columns = {row[1] for row in db.execute("PRAGMA table_info(codex_chat_sessions)")}
            if "compacted_message_id" not in session_columns:
                db.execute("ALTER TABLE codex_chat_sessions ADD COLUMN compacted_message_id INTEGER NOT NULL DEFAULT 0")
            # API-backed providers do not retain a server-side conversation in the
            # same way as Codex.  Keep their compacted memory separately so the
            # next request can send a concise summary plus only newer turns.
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_context_summaries (
                    project_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    compacted_message_id INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (project_id, role)
                )
                """
            )
            # Keep the most recent provider-reported token accounting separate
            # from the transcript.  It is intentionally disposable metadata:
            # the transcript and compacted memory remain the source of truth
            # when a provider does not return usage details.
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_context_usage (
                    project_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT '',
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                    reasoning_output_tokens INTEGER NOT NULL DEFAULT 0,
                    context_tokens INTEGER NOT NULL DEFAULT 0,
                    context_window_tokens INTEGER NOT NULL DEFAULT 0,
                    context_window_exact INTEGER NOT NULL DEFAULT 0,
                    observed_message_id INTEGER NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT '',
                    exact INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (project_id, role)
                )
                """
            )
            usage_columns = {row[1] for row in db.execute("PRAGMA table_info(agent_context_usage)")}
            if "context_window_exact" not in usage_columns:
                db.execute("ALTER TABLE agent_context_usage ADD COLUMN context_window_exact INTEGER NOT NULL DEFAULT 0")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_runs (
                    id TEXT PRIMARY KEY,
                    project_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued','running','completed','error')),
                    input_json TEXT NOT NULL DEFAULT '',
                    result_json TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            chat_run_columns = {row[1] for row in db.execute("PRAGMA table_info(chat_runs)")}
            if "input_json" not in chat_run_columns:
                db.execute("ALTER TABLE chat_runs ADD COLUMN input_json TEXT NOT NULL DEFAULT ''")
            if self.recover_interrupted_runs:
                # A process restart cannot resume an in-memory provider task.
                # Make that state explicit instead of leaving the UI polling
                # forever. Queued requests are durable user work and remain
                # available to the dispatcher after a restart.
                timestamp = datetime.now(timezone.utc).isoformat()
                db.execute(
                    """UPDATE chat_runs SET status='error', error=?, updated_at=?
                    WHERE status='running'""",
                    ("The server restarted before this agent run finished. Send the prompt again.", timestamp),
                )
                # A process restart can interrupt the provider after messages
                # were claimed but before they were marked delivered. Requeue
                # those messages so the next prompt cannot silently miss them.
                db.execute(
                    """UPDATE conversation_messages SET delivery_status='pending', delivery_run_id=''
                    WHERE delivery_status='in_prompt'"""
                )
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
        if not self._projects_available(db):
            return
        configs = [
            tuple(row)
            for row in db.execute(
                """SELECT role,provider,model,base_url,api_key_env,reasoning_effort,
                context_window_tokens,context_compaction_threshold FROM agent_runtime_config"""
            )
        ]
        for project_id, in db.execute("SELECT id FROM projects"):
            db.executemany(
                """INSERT OR IGNORE INTO project_agent_runtime_config
                    (project_id,role,provider,model,base_url,api_key_env,reasoning_effort,
                    context_window_tokens,context_compaction_threshold)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                [(project_id, *config) for config in configs],
            )

    def _coalesce_pending_commands(self, db: sqlite3.Connection) -> None:
        """Normalize pending commands left by an older app process."""
        groups = db.execute(
            """SELECT project_id, role, source_role FROM conversation_messages
            WHERE speaker='agent' AND message_kind='command' AND delivery_status='pending'
            GROUP BY project_id, role, source_role HAVING COUNT(*) > 1"""
        ).fetchall()
        for group in groups:
            rows = db.execute(
                """SELECT id, content FROM conversation_messages
                WHERE project_id=? AND role=? AND source_role=?
                  AND speaker='agent' AND message_kind='command' AND delivery_status='pending'
                ORDER BY id ASC""",
                (group["project_id"], group["role"], group["source_role"]),
            ).fetchall()
            if len(rows) < 2:
                continue
            compiled_content, _ = self._compile_command_content([str(row["content"]) for row in rows])
            first_id = int(rows[0]["id"])
            db.execute("UPDATE conversation_messages SET content=? WHERE id=?", (compiled_content, first_id))
            db.execute(
                "DELETE FROM conversation_messages WHERE id IN ({})".format(",".join("?" for _ in rows[1:])),
                tuple(int(row["id"]) for row in rows[1:]),
            )

    def get(self, role: str, project_id: int = 1) -> dict[str, str]:
        with self._connect() as db:
            if self._projects_available(db):
                row = db.execute(
                    "SELECT role,provider,model,base_url,api_key_env,reasoning_effort,context_window_tokens,context_compaction_threshold "
                    "FROM project_agent_runtime_config WHERE project_id=? AND role=?",
                    (project_id, role),
                ).fetchone()
            else:
                row = db.execute("SELECT * FROM agent_runtime_config WHERE role = ?", (role,)).fetchone()
        result = dict(row) if row else DEFAULTS.get(role, {"role": role, "provider": "codex", "model": "gpt-5.6-terra", "base_url": "", "api_key_env": ""}).copy()
        result.setdefault("reasoning_effort", "")
        result.setdefault("context_window_tokens", 128000)
        result.setdefault("context_compaction_threshold", 0)
        return result

    def list(self, roles: list[str] | None = None, project_id: int = 1) -> list[dict[str, str]]:
        return [self.get(role, project_id) for role in (roles or list(ROLES))]

    def save(self, role: str, provider: str, model: str, base_url: str, api_key_env: str,
             reasoning_effort: str = "", project_id: int = 1, context_window_tokens: int = 128000,
             context_compaction_threshold: int = 0) -> dict[str, str]:
        if provider not in PROVIDERS:
            raise ValueError(f"Unknown provider: {provider}")
        model = model.strip()
        if provider != "codex" and not model:
            raise ValueError("A model name is required")
        if provider == "compatible" and not base_url.strip():
            raise ValueError("A base URL is required for an OpenAI-compatible provider")
        context_window_tokens = max(1_000, min(int(context_window_tokens), 10_000_000))
        context_compaction_threshold = int(context_compaction_threshold)
        if context_compaction_threshold not in {0, 50, 60, 70, 80, 90, 95}:
            raise ValueError("Unsupported context compaction threshold")
        with self._connect() as db:
            values = (
                provider, model, base_url.strip(), api_key_env.strip(), reasoning_effort.strip(),
                context_window_tokens, context_compaction_threshold,
            )
            if self._projects_available(db):
                db.execute(
                    """INSERT INTO project_agent_runtime_config
                        (project_id,role,provider,model,base_url,api_key_env,reasoning_effort,
                        context_window_tokens,context_compaction_threshold)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(project_id,role) DO UPDATE SET provider=excluded.provider,
                        model=excluded.model, base_url=excluded.base_url, api_key_env=excluded.api_key_env,
                        reasoning_effort=excluded.reasoning_effort,
                        context_window_tokens=excluded.context_window_tokens,
                        context_compaction_threshold=excluded.context_compaction_threshold""",
                    (project_id, role, *values),
                )
            else:
                db.execute(
                    """INSERT INTO agent_runtime_config(
                    role, provider, model, base_url, api_key_env, reasoning_effort,
                    context_window_tokens,context_compaction_threshold)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(role) DO UPDATE SET provider=excluded.provider, model=excluded.model,
                    base_url=excluded.base_url, api_key_env=excluded.api_key_env, reasoning_effort=excluded.reasoning_effort,
                    context_window_tokens=excluded.context_window_tokens,
                    context_compaction_threshold=excluded.context_compaction_threshold""",
                    (role, *values),
                )
        return self.get(role, project_id)

    def add_message(self, role: str, speaker: str, content: str, provider: str, model: str,
                    project_id: int = 1, reply_to_id: int | None = None,
                    source_role: str = "", message_kind: str = "") -> dict[str, Any]:
        source_role = source_role.strip()
        message_kind = message_kind.strip().lower()
        if message_kind and message_kind not in INTER_AGENT_MESSAGE_KINDS:
            raise ValueError("Inter-agent message kind must be command or report")
        if message_kind and not source_role:
            raise ValueError("Inter-agent messages require a source role")
        delivery_status = "pending" if message_kind else ""
        with self._connect() as db:
            cursor = db.execute(
                """INSERT INTO conversation_messages(
                    role, speaker, content, provider, model, project_id, reply_to_id,
                    source_role, message_kind, delivery_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (role, speaker, content, provider, model, project_id, reply_to_id,
                 source_role, message_kind, delivery_status),
            )
            row = db.execute("SELECT * FROM conversation_messages WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return dict(row)

    @staticmethod
    def _command_parts(content: str) -> list[str]:
        """Unwrap a previously compiled command so later commands do not nest wrappers."""
        text = str(content or "").strip()
        envelope = None
        for prefix, suffix in (
            (COMPILED_COMMAND_PREFIX, COMPILED_COMMAND_SUFFIX),
            (LEGACY_COMPILED_COMMAND_PREFIX, LEGACY_COMPILED_COMMAND_SUFFIX),
        ):
            if text.startswith(prefix) and text.endswith(suffix):
                envelope = (prefix, suffix)
                break
        if envelope is None:
            return [text]
        prefix, suffix = envelope
        body = text[len(prefix):-len(suffix)].strip()
        parts: list[str] = []
        current: list[str] = []
        for line in body.splitlines():
            if line.startswith("- "):
                if current:
                    parts.append("\n".join(current).strip())
                current = [line[2:]]
            elif current:
                current.append(line)
        if current:
            parts.append("\n".join(current).strip())
        return [item for item in parts if item] or [body]

    @classmethod
    def command_parts(cls, content: str) -> list[str]:
        """Return individual commands from either current or legacy storage."""
        return cls._command_parts(content)

    @classmethod
    def _compile_command_content(cls, contents: list[str]) -> tuple[str, int]:
        parts: list[str] = []
        for content in contents:
            parts.extend(cls._command_parts(content))
        parts = [item for item in parts if item]
        if len(parts) <= 1:
            return (parts[0] if parts else "", len(parts))
        compiled = COMPILED_COMMAND_PREFIX + "\n" + "\n".join(
            f"- {item}" for item in parts
        ) + "\n" + COMPILED_COMMAND_SUFFIX
        return compiled, len(parts)

    def send_agent_message(self, sender_role: str, recipient_role: str, content: str,
                           message_kind: str, project_id: int = 1) -> dict[str, Any]:
        """Persist an inter-agent message in the recipient's project-scoped chat."""
        sender_role, recipient_role, content = sender_role.strip(), recipient_role.strip(), content.strip()
        message_kind = message_kind.strip().lower()
        if not sender_role or not recipient_role:
            raise ValueError("Sender and recipient roles are required")
        if sender_role == recipient_role:
            raise ValueError("An agent cannot send an inter-agent message to itself")
        if not content:
            raise ValueError("Inter-agent message content is required")
        if message_kind not in INTER_AGENT_MESSAGE_KINDS:
            raise ValueError("Inter-agent message kind must be command or report")
        with self._connect() as db:
            cursor = db.execute(
                """INSERT INTO conversation_messages(
                    role, speaker, content, provider, model, project_id,
                    source_role, message_kind, delivery_status
                ) VALUES (?, 'agent', ?, 'internal', '', ?, ?, ?, 'pending')""",
                (recipient_role, content, project_id, sender_role, message_kind),
            )
            message_id = int(cursor.lastrowid)
            compiled_count = 1
            if message_kind == "command":
                # Only commands that have not entered a provider prompt are
                # replaceable. Once the first command is in_prompt/delivered,
                # a later command must remain a separate queued request.
                rows = db.execute(
                    """SELECT id, content FROM conversation_messages
                    WHERE role=? AND project_id=? AND speaker='agent'
                      AND source_role=? AND message_kind='command'
                      AND delivery_status='pending'
                    ORDER BY id ASC""",
                    (recipient_role, project_id, sender_role),
                ).fetchall()
                if len(rows) > 1:
                    compiled_content, compiled_count = self._compile_command_content(
                        [str(row["content"]) for row in rows]
                    )
                    first_id = int(rows[0]["id"])
                    db.execute(
                        "UPDATE conversation_messages SET content=? WHERE id=?",
                        (compiled_content, first_id),
                    )
                    db.execute(
                        "DELETE FROM conversation_messages WHERE id IN ({})".format(
                            ",".join("?" for _ in rows[1:])
                        ),
                        tuple(int(row["id"]) for row in rows[1:]),
                    )
                    message_id = first_id
            row = db.execute(
                "SELECT * FROM conversation_messages WHERE id=?", (message_id,)
            ).fetchone()
        result = dict(row)
        result["compiled"] = compiled_count > 1
        result["compiled_count"] = compiled_count
        return result

    def pending_agent_messages(self, recipient_role: str, project_id: int = 1,
                               limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT * FROM conversation_messages
                WHERE role=? AND project_id=? AND speaker='agent'
                  AND message_kind IN ('command','report')
                  AND delivery_status='pending'
                ORDER BY id ASC LIMIT ?""",
                (recipient_role, project_id, max(1, min(int(limit), 500))),
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_agent_recipients(self) -> list[dict[str, Any]]:
        """Return each project/agent pair with work waiting in its inbox."""
        with self._connect() as db:
            rows = db.execute(
                """SELECT project_id, role, MIN(id) AS first_message_id
                FROM conversation_messages
                WHERE speaker='agent' AND message_kind IN ('command','report')
                  AND delivery_status='pending'
                GROUP BY project_id, role
                ORDER BY first_message_id ASC"""
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_pending_agent_messages(self, recipient_role: str, project_id: int = 1,
                                     delivery_run_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
        """Claim queued messages so only the next provider prompt synthesizes them."""
        run_id = delivery_run_id.strip() or uuid.uuid4().hex
        with self._connect() as db:
            rows = db.execute(
                """SELECT * FROM conversation_messages
                WHERE role=? AND project_id=? AND speaker='agent'
                  AND message_kind IN ('command','report') AND delivery_status='pending'
                ORDER BY id ASC LIMIT ?""",
                (recipient_role, project_id, max(1, min(int(limit), 500))),
            ).fetchall()
            ids = [int(row["id"]) for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                db.execute(
                    f"UPDATE conversation_messages SET delivery_status='in_prompt', delivery_run_id=? "
                    f"WHERE id IN ({placeholders})",
                    (run_id, *ids),
                )
        claimed = [dict(row) for row in rows]
        for item in claimed:
            item["delivery_status"] = "in_prompt"
            item["delivery_run_id"] = run_id
        return claimed

    def mark_agent_messages_delivered(self, message_ids: list[int], delivery_run_id: str) -> None:
        ids = [int(item) for item in message_ids]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        timestamp = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute(
                f"""UPDATE conversation_messages SET delivery_status='delivered', delivered_at=?
                WHERE id IN ({placeholders}) AND delivery_run_id=?""",
                (timestamp, *ids, delivery_run_id),
            )

    def release_agent_messages(self, message_ids: list[int], delivery_run_id: str) -> None:
        ids = [int(item) for item in message_ids]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as db:
            db.execute(
                f"""UPDATE conversation_messages SET delivery_status='pending', delivery_run_id=''
                WHERE id IN ({placeholders}) AND delivery_run_id=?""",
                (*ids, delivery_run_id),
            )

    def agent_inbox(self, recipient_role: str, project_id: int = 1,
                    include_delivered: bool = True, limit: int = 100) -> list[dict[str, Any]]:
        statuses = ("pending", "in_prompt", "delivered") if include_delivered else ("pending", "in_prompt")
        placeholders = ",".join("?" for _ in statuses)
        with self._connect() as db:
            rows = db.execute(
                f"""SELECT * FROM conversation_messages
                WHERE role=? AND project_id=? AND speaker='agent'
                  AND message_kind IN ('command','report')
                  AND delivery_status IN ({placeholders})
                ORDER BY id DESC LIMIT ?""",
                (recipient_role, project_id, *statuses, max(1, min(int(limit), 500))),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def history(self, role: str, limit: int = 20, project_id: int = 1) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM conversation_messages WHERE role = ? AND project_id = ? ORDER BY id DESC LIMIT ?",
                (role, project_id, limit)
            ).fetchall()
        messages = [dict(row) for row in reversed(rows)]
        if not messages:
            return messages
        ids = [item["id"] for item in messages]
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as db:
            attachments = db.execute(
                f"SELECT id, message_id, name, mime_type, size FROM chat_attachments WHERE message_id IN ({placeholders}) ORDER BY created_at",
                ids,
            ).fetchall()
        by_message: dict[int, list[dict[str, Any]]] = {}
        for row in attachments:
            item = dict(row)
            by_message.setdefault(item.pop("message_id"), []).append(item)
        for item in messages:
            item["attachments"] = by_message.get(item["id"], [])
        return messages

    def context_messages(self, role: str, project_id: int = 1, after_message_id: int = 0) -> list[dict[str, Any]]:
        """Return transcript entries contributing to the active context estimate."""
        with self._connect() as db:
            rows = db.execute(
                """SELECT id, content FROM conversation_messages
                WHERE role=? AND project_id=? AND id>? ORDER BY id""",
                (role, project_id, max(0, int(after_message_id))),
            ).fetchall()
        return [dict(row) for row in rows]

    def message(self, message_id: int, role: str, project_id: int) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM conversation_messages WHERE id=? AND role=? AND project_id=?",
                (message_id, role, project_id),
            ).fetchone()
        return dict(row) if row else None

    def create_attachment(self, project_id: int, role: str, name: str, mime_type: str,
                          size: int, path: str) -> dict[str, Any]:
        attachment_id = uuid.uuid4().hex
        with self._connect() as db:
            db.execute(
                """INSERT INTO chat_attachments(id, project_id, role, name, mime_type, size, path)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (attachment_id, project_id, role, name, mime_type, size, path),
            )
        return self.attachment(attachment_id)  # type: ignore[return-value]

    def attachment(self, attachment_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM chat_attachments WHERE id=?", (attachment_id,)).fetchone()
        return dict(row) if row else None

    def attachments_for(self, role: str, project_id: int | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM chat_attachments WHERE role=?"
        params: tuple[Any, ...] = (role,)
        if project_id is not None:
            query += " AND project_id=?"
            params = (role, project_id)
        with self._connect() as db:
            rows = db.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def delete_pending_attachment(self, attachment_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM chat_attachments WHERE id=? AND message_id IS NULL", (attachment_id,)
            ).fetchone()
            if row:
                db.execute("DELETE FROM chat_attachments WHERE id=?", (attachment_id,))
        return dict(row) if row else None

    def pending_attachments(self, ids: list[str], role: str, project_id: int) -> list[dict[str, Any]]:
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as db:
            rows = db.execute(
                f"""SELECT * FROM chat_attachments WHERE id IN ({placeholders})
                AND role=? AND project_id=? AND message_id IS NULL""",
                (*ids, role, project_id),
            ).fetchall()
        found = {row["id"]: dict(row) for row in rows}
        if len(found) != len(set(ids)):
            raise ValueError("One or more attachments are missing, already sent, or belong to another chat")
        return [found[item_id] for item_id in ids]

    def attach_to_message(self, ids: list[str], message_id: int) -> None:
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as db:
            db.execute(
                f"UPDATE chat_attachments SET message_id=? WHERE id IN ({placeholders}) AND message_id IS NULL",
                (message_id, *ids),
            )

    def clear_history(self, role: str, project_id: int = 1) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM conversation_messages WHERE role = ? AND project_id = ?", (role, project_id))
            db.execute("DELETE FROM codex_chat_sessions WHERE role = ? AND project_id = ?", (role, project_id))
            db.execute("DELETE FROM agent_context_summaries WHERE role = ? AND project_id = ?", (role, project_id))
            db.execute("DELETE FROM agent_context_usage WHERE role = ? AND project_id = ?", (role, project_id))
            db.execute("DELETE FROM chat_attachments WHERE role = ? AND project_id = ?", (role, project_id))

    def codex_session(self, role: str, project_id: int = 1) -> dict[str, str] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM codex_chat_sessions WHERE role = ? AND project_id = ?",
                (role, project_id),
            ).fetchone()
        return dict(row) if row else None

    def save_codex_session(self, role: str, project_id: int, session_id: str,
                           model: str = "", reasoning_effort: str = "") -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO codex_chat_sessions(project_id, role, session_id, model, reasoning_effort)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id, role) DO UPDATE SET session_id=excluded.session_id,
                model=excluded.model, reasoning_effort=excluded.reasoning_effort,
                updated_at=CURRENT_TIMESTAMP""",
                (project_id, role, session_id, model, reasoning_effort),
            )

    def mark_codex_context_compacted(self, role: str, project_id: int, message_id: int) -> None:
        with self._connect() as db:
            db.execute(
                """UPDATE codex_chat_sessions SET compacted_message_id=?,updated_at=CURRENT_TIMESTAMP
                WHERE project_id=? AND role=?""",
                (max(0, int(message_id)), project_id, role),
            )

    def context_summary(self, role: str, project_id: int = 1) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM agent_context_summaries WHERE role=? AND project_id=?",
                (role, project_id),
            ).fetchone()
        return dict(row) if row else None

    def save_context_summary(self, role: str, project_id: int, summary: str, message_id: int) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO agent_context_summaries(project_id,role,summary,compacted_message_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(project_id,role) DO UPDATE SET summary=excluded.summary,
                compacted_message_id=excluded.compacted_message_id,updated_at=CURRENT_TIMESTAMP""",
                (project_id, role, summary.strip(), max(0, int(message_id))),
            )

    def context_usage(self, role: str, project_id: int = 1) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM agent_context_usage WHERE role=? AND project_id=?",
                (role, project_id),
            ).fetchone()
        return dict(row) if row else None

    def save_context_usage(
        self, role: str, project_id: int, provider: str, model: str,
        input_tokens: int = 0, output_tokens: int = 0, total_tokens: int = 0,
        cached_input_tokens: int = 0, reasoning_output_tokens: int = 0,
        context_tokens: int = 0, context_window_tokens: int = 0,
        observed_message_id: int = 0, source: str = "", exact: bool = False,
        context_window_exact: bool = False,
    ) -> None:
        values = (
            project_id, role, provider, model or "", max(0, int(input_tokens)), max(0, int(output_tokens)),
            max(0, int(total_tokens)), max(0, int(cached_input_tokens)), max(0, int(reasoning_output_tokens)),
            max(0, int(context_tokens)), max(0, int(context_window_tokens)), int(bool(context_window_exact)),
            max(0, int(observed_message_id)), source or "", int(bool(exact)),
        )
        with self._connect() as db:
            db.execute(
                """INSERT INTO agent_context_usage(
                    project_id,role,provider,model,input_tokens,output_tokens,total_tokens,
                    cached_input_tokens,reasoning_output_tokens,context_tokens,context_window_tokens,
                    context_window_exact,observed_message_id,source,exact
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id,role) DO UPDATE SET provider=excluded.provider,
                    model=excluded.model,input_tokens=excluded.input_tokens,
                    output_tokens=excluded.output_tokens,total_tokens=excluded.total_tokens,
                    cached_input_tokens=excluded.cached_input_tokens,
                    reasoning_output_tokens=excluded.reasoning_output_tokens,
                    context_tokens=excluded.context_tokens,
                    context_window_tokens=excluded.context_window_tokens,
                    context_window_exact=excluded.context_window_exact,
                    observed_message_id=excluded.observed_message_id,
                    source=excluded.source,exact=excluded.exact,updated_at=CURRENT_TIMESTAMP""",
                values,
            )

    def clear_context_usage(self, role: str, project_id: int = 1) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM agent_context_usage WHERE role=? AND project_id=?", (role, project_id))

    def clear_codex_session(self, role: str, project_id: int = 1) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM codex_chat_sessions WHERE role = ? AND project_id = ?", (role, project_id))

    @staticmethod
    def _chat_run_record(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if not row:
            return None
        result = dict(row)
        raw_result = result.pop("result_json", "")
        raw_input = result.pop("input_json", "")
        try:
            result["result"] = json.loads(raw_result) if raw_result else None
        except json.JSONDecodeError:
            result["result"] = None
        try:
            result["request"] = json.loads(raw_input) if raw_input else None
        except json.JSONDecodeError:
            result["request"] = None
        return result

    def create_chat_run(self, role: str, project_id: int,
                        request: dict[str, Any] | None = None) -> dict[str, Any]:
        run_id = uuid.uuid4().hex
        timestamp = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute(
                """INSERT INTO chat_runs(id, project_id, role, status, input_json, created_at, updated_at)
                VALUES (?, ?, ?, 'queued', ?, ?, ?)""",
                (run_id, project_id, role, json.dumps(request or {}, default=str), timestamp, timestamp),
            )
        return self.chat_run(run_id)  # type: ignore[return-value]

    def claim_queued_chat_run(self, run_id: str) -> dict[str, Any] | None:
        """Atomically promote one durable queued request to a running task."""
        timestamp = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            cursor = db.execute(
                """UPDATE chat_runs SET status='running', updated_at=?
                WHERE id=? AND status='queued'""",
                (timestamp, run_id),
            )
        return self.chat_run(run_id) if cursor.rowcount else None

    def update_chat_run(self, run_id: str, status: str, result: dict[str, Any] | None = None,
                        error: str = "") -> dict[str, Any]:
        if status not in {"queued", "running", "completed", "error"}:
            raise ValueError(f"Unknown chat run status: {status}")
        timestamp = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            cursor = db.execute(
                """UPDATE chat_runs SET status=?, result_json=?, error=?, updated_at=? WHERE id=?""",
                (status, json.dumps(result, default=str) if result is not None else "", error, timestamp, run_id),
            )
        if not cursor.rowcount:
            raise KeyError(f"Chat run {run_id} not found")
        return self.chat_run(run_id)  # type: ignore[return-value]

    def chat_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM chat_runs WHERE id=?", (run_id,)).fetchone()
        return self._chat_run_record(row)

    def queued_chat_runs(self) -> list[dict[str, Any]]:
        """Return durable user prompts waiting behind a provider run."""
        with self._connect() as db:
            rows = db.execute(
                """SELECT * FROM chat_runs WHERE status='queued'
                ORDER BY created_at ASC"""
            ).fetchall()
        records = []
        for row in rows:
            record = self._chat_run_record(row)
            if record is not None:
                records.append(record)
        return records

    def running_chat_run(self, role: str, project_id: int) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT * FROM chat_runs WHERE role=? AND project_id=?
                AND status='running' ORDER BY created_at DESC LIMIT 1""",
                (role, project_id),
            ).fetchone()
        return self._chat_run_record(row)

    def active_chat_user_message_ids(self, role: str, project_id: int) -> set[int]:
        """Return user-message rows reserved by queued or running chat runs.

        Human prompts are persisted as soon as they are submitted so a queued
        request survives a browser or server restart.  Provider adapters build
        their prompt from the transcript, so those future-turn rows must be
        excluded until their own run starts; otherwise a prompt typed while an
        agent is busy could leak into the response for the work already in
        progress.
        """
        with self._connect() as db:
            rows = db.execute(
                """SELECT input_json FROM chat_runs
                WHERE role=? AND project_id=? AND status IN ('queued','running')""",
                (role, project_id),
            ).fetchall()
        message_ids: set[int] = set()
        for row in rows:
            raw = row["input_json"] or ""
            try:
                request = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                continue
            value = request.get("user_message_id") if isinstance(request, dict) else None
            try:
                if value:
                    message_ids.add(int(value))
            except (TypeError, ValueError):
                continue
        return message_ids

    def active_chat_run(self, role: str, project_id: int) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT * FROM chat_runs WHERE role=? AND project_id=?
                AND status IN ('queued','running')
                ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, created_at ASC LIMIT 1""",
                (role, project_id),
            ).fetchone()
        if not row:
            return None
        return self._chat_run_record(row)

    def latest_chat_run(self, role: str, project_id: int) -> dict[str, Any] | None:
        """Return the most recent run, including completed and failed runs."""
        with self._connect() as db:
            row = db.execute(
                """SELECT * FROM chat_runs WHERE role=? AND project_id=?
                ORDER BY created_at DESC LIMIT 1""",
                (role, project_id),
            ).fetchone()
        if not row:
            return None
        return self._chat_run_record(row)

    def remove_agent(self, role: str, project_id: int | None = None) -> None:
        with self._connect() as db:
            if project_id is not None and self._projects_available(db):
                db.execute("DELETE FROM project_agent_runtime_config WHERE project_id=? AND role=?", (project_id, role))
                db.execute(
                    "DELETE FROM conversation_messages WHERE project_id=? AND (role=? OR source_role=?)",
                    (project_id, role, role),
                )
                db.execute("DELETE FROM codex_chat_sessions WHERE project_id=? AND role=?", (project_id, role))
                db.execute("DELETE FROM chat_attachments WHERE project_id=? AND role=?", (project_id, role))
                db.execute("DELETE FROM chat_runs WHERE project_id=? AND role=?", (project_id, role))
            else:
                db.execute("DELETE FROM agent_runtime_config WHERE role=?", (role,))
                db.execute("DELETE FROM conversation_messages WHERE role=? OR source_role=?", (role, role))
                db.execute("DELETE FROM codex_chat_sessions WHERE role=?", (role,))
                db.execute("DELETE FROM chat_attachments WHERE role=?", (role,))
                db.execute("DELETE FROM chat_runs WHERE role=?", (role,))

    def remove_project(self, project_id: int) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM project_agent_runtime_config WHERE project_id=?", (project_id,))
            db.execute("DELETE FROM conversation_messages WHERE project_id=?", (project_id,))
            db.execute("DELETE FROM codex_chat_sessions WHERE project_id=?", (project_id,))
            db.execute("DELETE FROM chat_attachments WHERE project_id=?", (project_id,))
            db.execute("DELETE FROM chat_runs WHERE project_id=?", (project_id,))
