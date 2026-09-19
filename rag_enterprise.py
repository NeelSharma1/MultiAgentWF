"""Enterprise RAG contracts and optional production adapters.

The application remains local-first.  This module contains the pieces that
must be shared by the SQLite runtime and a distributed deployment: principals,
ACL/policy evaluation, connector documents, reranking, grounding validation,
durable audit metadata, and the PostgreSQL/pgvector schema.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from typing import Any, Iterable, Literal, Protocol
import uuid


GroundingPolicy = Literal["advisory", "grounded", "strict"]


@dataclass(frozen=True)
class RequestPrincipal:
    tenant_id: str
    user_id: str
    group_ids: tuple[str, ...] = ()
    service_id: str = ""
    is_admin: bool = False

    @property
    def subjects(self) -> frozenset[str]:
        values = {f"user:{self.user_id}"}
        values.update(f"group:{item}" for item in self.group_ids)
        if self.service_id:
            values.add(f"service:{self.service_id}")
        if self.is_admin:
            values.add("role:admin")
        return frozenset(values)


@dataclass(frozen=True)
class SourceDocument:
    source_id: str
    uri: str
    path: str
    revision: str
    content: str
    content_hash: str = ""
    mime_type: str = "text/plain"
    language: str = ""
    branch: str = ""
    acl_subjects: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def normalized(self) -> "SourceDocument":
        digest = self.content_hash or hashlib.sha256(self.content.encode("utf-8", "ignore")).hexdigest()
        return SourceDocument(
            source_id=self.source_id.strip(), uri=self.uri.strip(), path=self.path.strip().lstrip("/"),
            revision=self.revision.strip(), content=self.content, content_hash=digest,
            mime_type=self.mime_type or "text/plain", language=self.language,
            branch=self.branch, acl_subjects=tuple(sorted(set(self.acl_subjects))),
            metadata=dict(self.metadata),
        )


@dataclass(frozen=True)
class PolicyFinding:
    category: str
    action: Literal["allow", "redact", "quarantine", "exclude"]
    detail: str


@dataclass(frozen=True)
class GroundingDecision:
    claim: str
    source_ids: tuple[str, ...]
    status: Literal["supported", "partial", "unsupported"]
    reason: str


class RagSecurityPolicy:
    """Fail-closed ACL and pre-embedding content policy.

    The built-in patterns are intentionally conservative.  Deployments may
    replace this object with a DLP service while preserving the same contract.
    """

    SECRET_PATTERNS = (
        ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
        ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
        ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
        ("password_assignment", re.compile(
            r"(?im)^\s*(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*[^\s#]{8,}\s*$"
        )),
    )
    PII_PATTERNS = (
        ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
        ("credit_card", re.compile(r"\b(?:\d[ -]*?){13,19}\b")),
    )

    def __init__(self, *, secret_action: str = "exclude", pii_action: str = "quarantine") -> None:
        if secret_action not in {"redact", "quarantine", "exclude"}:
            raise ValueError("secret_action must be redact, quarantine, or exclude")
        if pii_action not in {"redact", "quarantine", "exclude"}:
            raise ValueError("pii_action must be redact, quarantine, or exclude")
        self.secret_action = secret_action
        self.pii_action = pii_action

    @staticmethod
    def authorized(principal: RequestPrincipal, tenant_id: str, acl_subjects: Iterable[str]) -> bool:
        if not principal.tenant_id or principal.tenant_id != tenant_id:
            return False
        subjects = frozenset(str(item) for item in acl_subjects if str(item))
        if principal.is_admin:
            return True
        # No ACL is private/unknown in enterprise mode.  Local callers add an
        # explicit project subject rather than relying on an empty allow-list.
        return bool(subjects and principal.subjects.intersection(subjects))

    def inspect(self, text: str) -> list[PolicyFinding]:
        findings: list[PolicyFinding] = []
        for category, pattern in self.SECRET_PATTERNS:
            if pattern.search(text):
                findings.append(PolicyFinding(category, self.secret_action, f"Detected {category}"))
        for category, pattern in self.PII_PATTERNS:
            if pattern.search(text):
                findings.append(PolicyFinding(category, self.pii_action, f"Detected {category}"))
        return findings

    def apply(self, text: str) -> tuple[str, list[PolicyFinding]]:
        findings = self.inspect(text)
        if any(item.action in {"exclude", "quarantine"} for item in findings):
            return "", findings
        redacted = text
        for _, pattern in (*self.SECRET_PATTERNS, *self.PII_PATTERNS):
            redacted = pattern.sub("[REDACTED]", redacted)
        return redacted, findings


def reciprocal_rank_fusion(
    ranked_lists: Iterable[Iterable[str]], *, constant: int = 60,
) -> dict[str, float]:
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, item_id in enumerate(ranked, 1):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (constant + rank)
    return scores


def _terms(value: str) -> set[str]:
    raw = set(re.findall(r"[A-Za-z0-9_./-]{2,}", value.lower()))
    expanded = set(raw)
    for term in raw:
        for suffix in ("ation", "ing", "ments", "ment", "ed", "es", "s", "e"):
            if term.endswith(suffix) and len(term) - len(suffix) >= 4:
                expanded.add(term[:-len(suffix)])
    return expanded


def deterministic_rerank(query: str, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stable local reranker and fallback for an unavailable model service."""
    query_terms = _terms(query)
    rescored: list[tuple[float, int, dict[str, Any]]] = []
    for position, item in enumerate(results):
        excerpt_terms = _terms(f"{item.get('path', '')} {item.get('symbol', '')} {item.get('excerpt', '')}")
        lexical = len(query_terms & excerpt_terms) / max(1, len(query_terms))
        fused = float((item.get("scores") or {}).get("fused") or 0.0)
        score = lexical * 0.7 + fused * 0.3
        enriched = dict(item)
        enriched["scores"] = {**dict(item.get("scores") or {}), "rerank": round(score, 6)}
        rescored.append((score, -position, enriched))
    return [item for _, _, item in sorted(rescored, key=lambda value: (value[0], value[1]), reverse=True)]


def diversify_results(results: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    """Select relevant evidence while limiting file and overlapping-chunk dominance."""
    selected: list[dict[str, Any]] = []
    per_path: dict[str, int] = {}
    for item in results:
        path = str(item.get("path") or "")
        if per_path.get(path, 0) >= 2:
            continue
        start, end = int(item.get("start_line") or 0), int(item.get("end_line") or 0)
        if any(
            str(existing.get("path") or "") == path
            and not (end < int(existing.get("start_line") or 0) or start > int(existing.get("end_line") or 0))
            for existing in selected
        ):
            continue
        selected.append(item)
        per_path[path] = per_path.get(path, 0) + 1
        if len(selected) >= max(1, limit):
            break
    for index, item in enumerate(selected, 1):
        item["source_id"] = f"S{index}"
        item["rank"] = index
    return selected


class GroundingVerifier:
    CITATION_RE = re.compile(r"\[(S\d+)\]", re.IGNORECASE)
    SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")

    @classmethod
    def verify(cls, answer: str, sources: list[dict[str, Any]]) -> list[GroundingDecision]:
        source_map = {str(item.get("source_id") or "").upper(): item for item in sources}
        decisions: list[GroundingDecision] = []
        for raw_claim in cls.SENTENCE_RE.split(str(answer or "")):
            claim = raw_claim.strip()
            if not claim or len(_terms(claim)) < 3:
                continue
            cited = tuple(dict.fromkeys(match.upper() for match in cls.CITATION_RE.findall(claim)))
            if not cited:
                decisions.append(GroundingDecision(claim, (), "unsupported", "Claim has no project source citation"))
                continue
            missing = [source_id for source_id in cited if source_id not in source_map]
            if missing:
                decisions.append(GroundingDecision(
                    claim, cited, "unsupported", f"Unknown source identifiers: {', '.join(missing)}",
                ))
                continue
            claim_terms = _terms(cls.CITATION_RE.sub("", claim))
            evidence_terms = set().union(*(
                _terms(" ".join((
                    str(source_map[source_id].get("path") or ""),
                    str(source_map[source_id].get("symbol") or ""),
                    str(source_map[source_id].get("excerpt") or ""),
                ))) for source_id in cited
            ))
            overlap = len(claim_terms & evidence_terms) / max(1, len(claim_terms))
            status: Literal["supported", "partial", "unsupported"] = (
                "supported" if overlap >= 0.1 else "partial" if overlap >= 0.04 else "unsupported"
            )
            decisions.append(GroundingDecision(
                claim, cited, status, f"Lexical support score {overlap:.2f}",
            ))
        return decisions


class SourceConnector(ABC):
    @abstractmethod
    def validate(self) -> dict[str, Any]: ...

    @abstractmethod
    def checkpoint(self) -> str: ...

    @abstractmethod
    def documents(self, previous_checkpoint: str = "") -> Iterable[SourceDocument]: ...


class GitConnector(SourceConnector):
    """Read a local Git revision without checking out or modifying the repository."""

    def __init__(self, source_id: str, repository: str | Path, *, revision: str = "HEAD",
                 branch: str = "", acl_subjects: Iterable[str] = ()) -> None:
        self.source_id = source_id
        self.repository = Path(repository).expanduser().resolve()
        self.revision = revision or "HEAD"
        self.branch = branch
        self.acl_subjects = tuple(acl_subjects)

    def _git(self, *args: str, timeout: int = 30) -> str:
        process = subprocess.run(
            ["git", "-C", str(self.repository), *args], capture_output=True, text=True,
            timeout=timeout, check=False,
        )
        if process.returncode:
            raise ValueError(process.stderr.strip() or "Git command failed")
        return process.stdout

    def validate(self) -> dict[str, Any]:
        if not self.repository.is_dir():
            raise ValueError("Git connector repository does not exist")
        bare = self._git("rev-parse", "--is-bare-repository").strip() == "true"
        root = str(self.repository) if bare else self._git("rev-parse", "--show-toplevel").strip()
        revision = self._git("rev-parse", self.revision).strip()
        branch = self.branch or self._git("rev-parse", "--abbrev-ref", self.revision).strip()
        return {"ok": True, "root": root, "revision": revision, "branch": branch}

    @classmethod
    def materialize_remote(cls, source_id: str, remote_url: str, cache_root: str | Path,
                           **options: Any) -> "GitConnector":
        """Clone/fetch a read-only mirror using the host's configured Git credential helper."""
        remote_url = str(remote_url or "").strip()
        if not remote_url:
            raise ValueError("Remote Git URL is required")
        cache = Path(cache_root).expanduser().resolve()
        cache.mkdir(parents=True, exist_ok=True)
        repository = cache / hashlib.sha256(remote_url.encode()).hexdigest()[:24]
        environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        if repository.exists():
            command = ["git", "-C", str(repository), "fetch", "--prune", "origin"]
        else:
            command = ["git", "clone", "--mirror", remote_url, str(repository)]
        process = subprocess.run(
            command, capture_output=True, text=True, timeout=180, check=False, env=environment,
        )
        if process.returncode:
            raise ValueError(process.stderr.strip() or "Git mirror synchronization failed")
        return cls(source_id, repository, **options)

    def checkpoint(self) -> str:
        return self._git("rev-parse", self.revision).strip()

    def documents(self, previous_checkpoint: str = "") -> Iterable[SourceDocument]:
        revision = self.checkpoint()
        if previous_checkpoint and previous_checkpoint == revision:
            return []
        branch = self.branch or self._git("rev-parse", "--abbrev-ref", self.revision).strip()
        paths = [line for line in self._git("ls-tree", "-r", "--name-only", revision).splitlines() if line]
        output: list[SourceDocument] = []
        for path in paths:
            try:
                payload = subprocess.run(
                    ["git", "-C", str(self.repository), "show", f"{revision}:{path}"],
                    capture_output=True, timeout=30, check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if payload.returncode or len(payload.stdout) > 128_000 or b"\0" in payload.stdout[:8192]:
                continue
            text = payload.stdout.decode("utf-8", "replace")
            output.append(SourceDocument(
                source_id=self.source_id, uri=f"git://{self.source_id}/{path}", path=path,
                revision=revision, branch=branch, content=text,
                language=Path(path).suffix.lower().lstrip("."), acl_subjects=self.acl_subjects,
                metadata={"repository": str(self.repository), "blob_revision": revision},
            ).normalized())
        return output


class RagEnterpriseStore:
    """SQLite control-plane store used in local mode and by API tests."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS rag_sources(
                    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL DEFAULT 'local', project_id INTEGER NOT NULL,
                    kind TEXT NOT NULL, name TEXT NOT NULL, config_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'active', checkpoint TEXT NOT NULL DEFAULT '',
                    last_success_at TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_rag_sources_scope ON rag_sources(tenant_id,project_id);
                CREATE TABLE IF NOT EXISTS rag_retrieval_traces(
                    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL DEFAULT 'local', project_id INTEGER NOT NULL,
                    role TEXT NOT NULL DEFAULT '', principal_json TEXT NOT NULL DEFAULT '{}', query TEXT NOT NULL,
                    options_json TEXT NOT NULL DEFAULT '{}', candidates_json TEXT NOT NULL DEFAULT '[]',
                    results_json TEXT NOT NULL DEFAULT '[]', claims_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'running', generation_id TEXT NOT NULL DEFAULT '',
                    timings_json TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_rag_traces_scope ON rag_retrieval_traces(tenant_id,project_id,created_at);
                CREATE TABLE IF NOT EXISTS rag_retention_policies(
                    tenant_id TEXT NOT NULL, project_id INTEGER NOT NULL,
                    source_days INTEGER NOT NULL DEFAULT 30, trace_days INTEGER NOT NULL DEFAULT 30,
                    audit_days INTEGER NOT NULL DEFAULT 365, rollback_days INTEGER NOT NULL DEFAULT 7,
                    legal_hold INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(tenant_id,project_id)
                );
                CREATE TABLE IF NOT EXISTS rag_deletion_receipts(
                    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id INTEGER NOT NULL,
                    source_id TEXT NOT NULL DEFAULT '', target_hash TEXT NOT NULL,
                    status TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
            """)

    @staticmethod
    def _decode(row: sqlite3.Row | None, fields: Iterable[str]) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        for field_name in fields:
            try:
                result[field_name.removesuffix("_json")] = json.loads(result.pop(field_name) or "null")
            except (ValueError, TypeError):
                result[field_name.removesuffix("_json")] = None
        return result

    def create_source(self, tenant_id: str, project_id: int, kind: str, name: str,
                      config: dict[str, Any]) -> dict[str, Any]:
        source_id = uuid.uuid4().hex
        with self._connect() as db:
            db.execute(
                "INSERT INTO rag_sources(id,tenant_id,project_id,kind,name,config_json) VALUES(?,?,?,?,?,?)",
                (source_id, tenant_id, project_id, kind, name.strip(), json.dumps(config, default=str)),
            )
        return self.source(source_id, tenant_id) or {}

    def source(self, source_id: str, tenant_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM rag_sources WHERE id=? AND tenant_id=?", (source_id, tenant_id)).fetchone()
        return self._decode(row, ("config_json",))

    def sources(self, tenant_id: str, project_id: int) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM rag_sources WHERE tenant_id=? AND project_id=? ORDER BY created_at,id",
                (tenant_id, project_id),
            ).fetchall()
        return [self._decode(row, ("config_json",)) or {} for row in rows]

    def update_source(self, source_id: str, tenant_id: str, **values: Any) -> dict[str, Any] | None:
        allowed = {"name", "status", "checkpoint", "last_success_at", "last_error"}
        updates = {key: str(value) for key, value in values.items() if key in allowed}
        if "config" in values:
            updates["config_json"] = json.dumps(values["config"], default=str)
        if not updates:
            return self.source(source_id, tenant_id)
        assignments = ",".join(f"{key}=?" for key in updates)
        with self._connect() as db:
            db.execute(
                f"UPDATE rag_sources SET {assignments},updated_at=CURRENT_TIMESTAMP WHERE id=? AND tenant_id=?",
                (*updates.values(), source_id, tenant_id),
            )
        return self.source(source_id, tenant_id)

    def delete_source(self, source_id: str, tenant_id: str) -> bool:
        with self._connect() as db:
            cursor = db.execute("DELETE FROM rag_sources WHERE id=? AND tenant_id=?", (source_id, tenant_id))
        return bool(cursor.rowcount)

    def set_retention(self, tenant_id: str, project_id: int, *, source_days: int = 30,
                      trace_days: int = 30, audit_days: int = 365,
                      rollback_days: int = 7, legal_hold: bool = False) -> dict[str, Any]:
        values = [max(1, int(item)) for item in (source_days, trace_days, audit_days, rollback_days)]
        with self._connect() as db:
            db.execute(
                """INSERT INTO rag_retention_policies(
                tenant_id,project_id,source_days,trace_days,audit_days,rollback_days,legal_hold)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(tenant_id,project_id) DO UPDATE SET
                source_days=excluded.source_days,trace_days=excluded.trace_days,
                audit_days=excluded.audit_days,rollback_days=excluded.rollback_days,
                legal_hold=excluded.legal_hold,updated_at=CURRENT_TIMESTAMP""",
                (tenant_id, project_id, *values, int(bool(legal_hold))),
            )
        return self.retention(tenant_id, project_id)

    def retention(self, tenant_id: str, project_id: int) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM rag_retention_policies WHERE tenant_id=? AND project_id=?",
                (tenant_id, project_id),
            ).fetchone()
        if row:
            return dict(row)
        return self.set_retention(tenant_id, project_id)

    def apply_retention(self, tenant_id: str, project_id: int) -> dict[str, int]:
        policy = self.retention(tenant_id, project_id)
        if bool(policy.get("legal_hold")):
            return {"traces_deleted": 0}
        with self._connect() as db:
            cursor = db.execute(
                """DELETE FROM rag_retrieval_traces WHERE tenant_id=? AND project_id=?
                AND created_at < datetime('now', ?)""",
                (tenant_id, project_id, f"-{int(policy['trace_days'])} days"),
            )
        return {"traces_deleted": max(0, cursor.rowcount)}

    def deletion_receipt(self, tenant_id: str, project_id: int, source_id: str,
                         status: str, details: dict[str, Any]) -> dict[str, Any]:
        receipt_id = uuid.uuid4().hex
        target_hash = hashlib.sha256(
            f"{tenant_id}:{project_id}:{source_id}".encode()
        ).hexdigest()
        with self._connect() as db:
            db.execute(
                """INSERT INTO rag_deletion_receipts(
                id,tenant_id,project_id,source_id,target_hash,status,details_json)
                VALUES(?,?,?,?,?,?,?)""",
                (receipt_id, tenant_id, project_id, source_id, target_hash, status,
                 json.dumps(details, default=str)),
            )
            row = db.execute("SELECT * FROM rag_deletion_receipts WHERE id=?", (receipt_id,)).fetchone()
        return self._decode(row, ("details_json",)) or {}

    def delete_project(self, tenant_id: str, project_id: int) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM rag_sources WHERE tenant_id=? AND project_id=?", (tenant_id, project_id))
            db.execute("DELETE FROM rag_retrieval_traces WHERE tenant_id=? AND project_id=?", (tenant_id, project_id))
            db.execute("DELETE FROM rag_retention_policies WHERE tenant_id=? AND project_id=?", (tenant_id, project_id))

    def start_trace(self, tenant_id: str, project_id: int, role: str, principal: RequestPrincipal,
                    query: str, options: dict[str, Any]) -> str:
        trace_id = uuid.uuid4().hex
        with self._connect() as db:
            db.execute(
                """INSERT INTO rag_retrieval_traces(
                id,tenant_id,project_id,role,principal_json,query,options_json)
                VALUES(?,?,?,?,?,?,?)""",
                (trace_id, tenant_id, project_id, role, json.dumps(asdict(principal)), query,
                 json.dumps(options, default=str)),
            )
        return trace_id

    def finish_trace(self, trace_id: str, tenant_id: str, *, status: str,
                     candidates: list[dict[str, Any]] | None = None,
                     results: list[dict[str, Any]] | None = None,
                     claims: list[GroundingDecision] | None = None,
                     timings: dict[str, Any] | None = None, generation_id: str = "", error: str = "") -> None:
        with self._connect() as db:
            db.execute(
                """UPDATE rag_retrieval_traces SET status=?,candidates_json=?,results_json=?,claims_json=?,
                timings_json=?,generation_id=?,error=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND tenant_id=?""",
                (status, json.dumps(candidates or [], default=str), json.dumps(results or [], default=str),
                 json.dumps([asdict(item) for item in (claims or [])]), json.dumps(timings or {}, default=str),
                 generation_id, error[:1000], trace_id, tenant_id),
            )

    def trace(self, trace_id: str, tenant_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM rag_retrieval_traces WHERE id=? AND tenant_id=?", (trace_id, tenant_id),
            ).fetchone()
        return self._decode(row, (
            "principal_json", "options_json", "candidates_json", "results_json", "claims_json", "timings_json",
        ))

    def traces(self, tenant_id: str, project_id: int, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT * FROM rag_retrieval_traces WHERE tenant_id=? AND project_id=?
                ORDER BY created_at DESC,id DESC LIMIT ?""", (tenant_id, project_id, max(1, min(limit, 200))),
            ).fetchall()
        fields = ("principal_json", "options_json", "candidates_json", "results_json", "claims_json", "timings_json")
        return [self._decode(row, fields) or {} for row in rows]

    def metrics(self, tenant_id: str, project_id: int) -> dict[str, Any]:
        traces = self.traces(tenant_id, project_id, 200)
        latencies = sorted(
            float((item.get("timings") or {}).get("retrieval_ms") or 0) for item in traces
            if float((item.get("timings") or {}).get("retrieval_ms") or 0) > 0
        )
        claims = [claim for item in traces for claim in (item.get("claims") or [])]
        supported = sum(1 for claim in claims if claim.get("status") == "supported")
        percentile_index = max(0, math.ceil(len(latencies) * 0.95) - 1) if latencies else 0
        return {
            "trace_count": len(traces),
            "error_count": sum(1 for item in traces if item.get("status") == "error"),
            "empty_result_count": sum(1 for item in traces if not item.get("results")),
            "claim_count": len(claims),
            "grounded_claim_rate": round(supported / len(claims), 4) if claims else None,
            "retrieval_latency_p95_ms": latencies[percentile_index] if latencies else None,
        }


class PrincipalResolver:
    """Resolve local identities or verify HMAC-signed trusted-gateway headers."""

    def __init__(self, *, enterprise: bool | None = None, secret: str | None = None) -> None:
        enabled = os.getenv("RAG_ENTERPRISE_MODE", "0").strip().lower() in {"1", "true", "yes", "on"}
        self.enterprise = enabled if enterprise is None else enterprise
        self.secret = secret if secret is not None else os.getenv("RAG_TRUSTED_AUTH_SECRET", "")

    def resolve(self, headers: dict[str, str], tenant_id: str = "local") -> RequestPrincipal:
        if not self.enterprise:
            return RequestPrincipal(tenant_id or "local", "local-user", ("project-members",), is_admin=True)
        normalized = {str(key).lower(): str(value) for key, value in headers.items()}
        resolved_tenant = normalized.get("x-rag-tenant", "")
        user = normalized.get("x-rag-user", "")
        groups = tuple(sorted(item.strip() for item in normalized.get("x-rag-groups", "").split(",") if item.strip()))
        timestamp = normalized.get("x-rag-timestamp", "")
        signature = normalized.get("x-rag-signature", "")
        if not self.secret or not resolved_tenant or not user or not timestamp or not signature:
            raise PermissionError("Trusted RAG identity headers are required")
        try:
            if abs(time.time() - int(timestamp)) > 300:
                raise PermissionError("Trusted RAG identity headers expired")
        except ValueError as exc:
            raise PermissionError("Invalid trusted identity timestamp") from exc
        message = "\n".join((resolved_tenant, user, ",".join(groups), timestamp)).encode()
        expected = hmac.new(self.secret.encode(), message, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise PermissionError("Invalid trusted identity signature")
        if tenant_id and resolved_tenant != tenant_id:
            raise PermissionError("Identity tenant does not match the requested project")
        return RequestPrincipal(resolved_tenant, user, groups)


POSTGRES_RAG_SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS rag_generations(
    id uuid PRIMARY KEY, tenant_id text NOT NULL, project_id bigint NOT NULL,
    source_id text NOT NULL, revision text NOT NULL, embedding_fingerprint text NOT NULL,
    status text NOT NULL CHECK(status IN ('building','active','superseded','failed')),
    created_at timestamptz NOT NULL DEFAULT now(), activated_at timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS rag_one_active_generation
    ON rag_generations(tenant_id,project_id,source_id) WHERE status='active';
CREATE TABLE IF NOT EXISTS rag_documents_v2(
    id uuid PRIMARY KEY, generation_id uuid NOT NULL REFERENCES rag_generations(id) ON DELETE CASCADE,
    tenant_id text NOT NULL, project_id bigint NOT NULL, source_id text NOT NULL,
    uri text NOT NULL, path text NOT NULL, branch text NOT NULL DEFAULT '', revision text NOT NULL,
    content_hash text NOT NULL, metadata jsonb NOT NULL DEFAULT '{}', acl_subjects text[] NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS rag_chunks_v2(
    id uuid PRIMARY KEY, document_id uuid NOT NULL REFERENCES rag_documents_v2(id) ON DELETE CASCADE,
    generation_id uuid NOT NULL REFERENCES rag_generations(id) ON DELETE CASCADE,
    tenant_id text NOT NULL, project_id bigint NOT NULL, source_id text NOT NULL,
    path text NOT NULL, branch text NOT NULL DEFAULT '', revision text NOT NULL,
    start_line integer NOT NULL, end_line integer NOT NULL, symbol text NOT NULL DEFAULT '',
    content text NOT NULL, content_hash text NOT NULL, token_count integer NOT NULL,
    embedding vector, search_vector tsvector GENERATED ALWAYS AS
        (to_tsvector('simple', coalesce(path,'') || ' ' || coalesce(symbol,'') || ' ' || coalesce(content,''))) STORED,
    acl_subjects text[] NOT NULL, metadata jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS rag_chunks_v2_scope ON rag_chunks_v2(tenant_id,project_id,source_id,generation_id);
CREATE INDEX IF NOT EXISTS rag_chunks_v2_fts ON rag_chunks_v2 USING gin(search_vector);
CREATE INDEX IF NOT EXISTS rag_chunks_v2_acl ON rag_chunks_v2 USING gin(acl_subjects);
CREATE TABLE IF NOT EXISTS rag_jobs_v2(
    id uuid PRIMARY KEY, tenant_id text NOT NULL, project_id bigint NOT NULL, source_id text NOT NULL,
    idempotency_key text NOT NULL UNIQUE, status text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}', attempts integer NOT NULL DEFAULT 0,
    lease_owner text NOT NULL DEFAULT '', lease_expires_at timestamptz,
    available_at timestamptz NOT NULL DEFAULT now(), error text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS rag_jobs_v2_claim ON rag_jobs_v2(status,available_at,lease_expires_at);
"""


class VectorBackend(Protocol):
    async def initialize(self) -> None: ...
    async def search(self, principal: RequestPrincipal, project_id: int, query_vector: list[float],
                     query: str, *, limit: int, source_ids: tuple[str, ...], branch: str,
                     revision: str) -> list[dict[str, Any]]: ...
    async def delete_project(self, tenant_id: str, project_id: int) -> None: ...
    async def delete_source(self, tenant_id: str, project_id: int, source_id: str) -> None: ...
    async def apply_retention(self, tenant_id: str, project_id: int, rollback_days: int) -> int: ...


class PostgresVectorBackend:
    """Optional pgvector backend.  Importing the local app needs no PostgreSQL packages."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    @staticmethod
    def _driver():
        try:
            import psycopg  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional enterprise dependency
            raise RuntimeError("Install the enterprise extra to use PostgreSQL RAG") from exc
        return psycopg

    async def initialize(self) -> None:
        psycopg = self._driver()
        async with await psycopg.AsyncConnection.connect(self.dsn, autocommit=True) as connection:
            await connection.execute(POSTGRES_RAG_SCHEMA)
            dimensions = int(os.getenv("RAG_VECTOR_DIMENSIONS", "0") or 0)
            if dimensions:
                if dimensions < 1 or dimensions > 16_000:
                    raise ValueError("RAG_VECTOR_DIMENSIONS must be between 1 and 16000")
                cursor = await connection.execute(
                    """SELECT format_type(a.atttypid,a.atttypmod) FROM pg_attribute a
                    WHERE a.attrelid='rag_chunks_v2'::regclass AND a.attname='embedding'"""
                )
                current_type = (await cursor.fetchone())[0]
                if current_type != f"vector({dimensions})":
                    count_cursor = await connection.execute("SELECT COUNT(*) FROM rag_chunks_v2")
                    if int((await count_cursor.fetchone())[0]):
                        raise RuntimeError(
                            "RAG_VECTOR_DIMENSIONS differs from the populated PostgreSQL index; "
                            "run a controlled embedding migration"
                        )
                    await connection.execute(
                        f"ALTER TABLE rag_chunks_v2 ALTER COLUMN embedding TYPE vector({dimensions}) "
                        f"USING embedding::vector({dimensions})"
                    )
                await connection.execute(
                    "CREATE INDEX IF NOT EXISTS rag_chunks_v2_embedding_hnsw ON rag_chunks_v2 "
                    "USING hnsw (embedding vector_cosine_ops)"
                )

    @staticmethod
    def _vector(value: Iterable[float]) -> str:
        numbers = [float(item) for item in value]
        if not numbers or not all(math.isfinite(item) for item in numbers):
            raise ValueError("Embedding contains invalid values")
        return "[" + ",".join(f"{item:.9g}" for item in numbers) + "]"

    async def enqueue_job(self, tenant_id: str, project_id: int, source_id: str,
                          idempotency_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        psycopg = self._driver()
        job_id = uuid.uuid4()
        async with await psycopg.AsyncConnection.connect(self.dsn) as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """INSERT INTO rag_jobs_v2(id,tenant_id,project_id,source_id,idempotency_key,status,payload)
                    VALUES(%s,%s,%s,%s,%s,'queued',%s) ON CONFLICT(idempotency_key) DO UPDATE SET
                    payload=excluded.payload RETURNING *""",
                    (job_id, tenant_id, project_id, source_id, idempotency_key, json.dumps(payload)),
                )
                row = await cursor.fetchone()
                columns = [item.name for item in cursor.description]
            await connection.commit()
        return dict(zip(columns, row))

    async def claim_job(self, worker_id: str, lease_seconds: int = 300) -> dict[str, Any] | None:
        psycopg = self._driver()
        async with await psycopg.AsyncConnection.connect(self.dsn) as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """WITH candidate AS (
                    SELECT id FROM rag_jobs_v2 WHERE status='queued' AND available_at<=now()
                    AND (lease_expires_at IS NULL OR lease_expires_at<now())
                    ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)
                    UPDATE rag_jobs_v2 j SET status='running',attempts=attempts+1,lease_owner=%s,
                    lease_expires_at=now()+(%s * interval '1 second'),updated_at=now()
                    FROM candidate WHERE j.id=candidate.id RETURNING j.*""",
                    (worker_id, max(30, int(lease_seconds))),
                )
                row = await cursor.fetchone()
                columns = [item.name for item in cursor.description] if cursor.description else []
            await connection.commit()
        return dict(zip(columns, row)) if row else None

    async def finish_job(self, job_id: str, worker_id: str, *, error: str = "") -> bool:
        psycopg = self._driver()
        async with await psycopg.AsyncConnection.connect(self.dsn) as connection:
            cursor = await connection.execute(
                """UPDATE rag_jobs_v2 SET status=%s,error=%s,lease_owner='',lease_expires_at=NULL,
                updated_at=now() WHERE id=%s AND lease_owner=%s""",
                ("failed" if error else "completed", error[:1000], job_id, worker_id),
            )
            await connection.commit()
        return bool(cursor.rowcount)

    async def delete_project(self, tenant_id: str, project_id: int) -> None:
        psycopg = self._driver()
        async with await psycopg.AsyncConnection.connect(self.dsn) as connection:
            await connection.execute(
                "DELETE FROM rag_generations WHERE tenant_id=%s AND project_id=%s",
                (tenant_id, project_id),
            )
            await connection.execute(
                "DELETE FROM rag_jobs_v2 WHERE tenant_id=%s AND project_id=%s",
                (tenant_id, project_id),
            )
            await connection.commit()

    async def delete_source(self, tenant_id: str, project_id: int, source_id: str) -> None:
        psycopg = self._driver()
        async with await psycopg.AsyncConnection.connect(self.dsn) as connection:
            await connection.execute(
                "DELETE FROM rag_generations WHERE tenant_id=%s AND project_id=%s AND source_id=%s",
                (tenant_id, project_id, source_id),
            )
            await connection.execute(
                "DELETE FROM rag_jobs_v2 WHERE tenant_id=%s AND project_id=%s AND source_id=%s",
                (tenant_id, project_id, source_id),
            )
            await connection.commit()

    async def apply_retention(self, tenant_id: str, project_id: int, rollback_days: int) -> int:
        psycopg = self._driver()
        async with await psycopg.AsyncConnection.connect(self.dsn) as connection:
            cursor = await connection.execute(
                """DELETE FROM rag_generations WHERE tenant_id=%s AND project_id=%s
                AND status IN ('superseded','failed')
                AND created_at < now()-(%s * interval '1 day')""",
                (tenant_id, project_id, max(1, int(rollback_days))),
            )
            await connection.commit()
        return max(0, cursor.rowcount)

    async def publish_generation(self, principal: RequestPrincipal, project_id: int, source_id: str,
                                 revision: str, embedding_fingerprint: str,
                                 documents: Iterable[dict[str, Any]]) -> str:
        """Build a complete generation and switch it active in one transaction."""
        psycopg = self._driver()
        generation_id = uuid.uuid4()
        async with await psycopg.AsyncConnection.connect(self.dsn) as connection:
            try:
                await connection.execute(
                    """INSERT INTO rag_generations(id,tenant_id,project_id,source_id,revision,
                    embedding_fingerprint,status) VALUES(%s,%s,%s,%s,%s,%s,'building')""",
                    (generation_id, principal.tenant_id, project_id, source_id, revision,
                     embedding_fingerprint),
                )
                for document in documents:
                    document_id = uuid.uuid4()
                    acl = list(document.get("acl_subjects") or principal.subjects)
                    await connection.execute(
                        """INSERT INTO rag_documents_v2(id,generation_id,tenant_id,project_id,source_id,
                        uri,path,branch,revision,content_hash,metadata,acl_subjects)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (document_id, generation_id, principal.tenant_id, project_id, source_id,
                         document.get("uri", ""), document["path"], document.get("branch", ""), revision,
                         document["content_hash"], json.dumps(document.get("metadata") or {}), acl),
                    )
                    for chunk in document.get("chunks") or ():
                        await connection.execute(
                            """INSERT INTO rag_chunks_v2(id,document_id,generation_id,tenant_id,project_id,
                            source_id,path,branch,revision,start_line,end_line,symbol,content,content_hash,
                            token_count,embedding,acl_subjects,metadata)
                            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,%s,%s)""",
                            (uuid.uuid4(), document_id, generation_id, principal.tenant_id, project_id,
                             source_id, document["path"], document.get("branch", ""), revision,
                             chunk["start_line"], chunk["end_line"], chunk.get("symbol", ""),
                             chunk["content"], chunk["content_hash"], chunk["token_count"],
                             self._vector(chunk["embedding"]), acl, json.dumps(chunk.get("metadata") or {})),
                        )
                await connection.execute(
                    """UPDATE rag_generations SET status='superseded' WHERE tenant_id=%s AND project_id=%s
                    AND source_id=%s AND status='active'""",
                    (principal.tenant_id, project_id, source_id),
                )
                await connection.execute(
                    "UPDATE rag_generations SET status='active',activated_at=now() WHERE id=%s",
                    (generation_id,),
                )
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise
        return str(generation_id)

    async def search(self, principal: RequestPrincipal, project_id: int, query_vector: list[float],
                     query: str, *, limit: int = 8, source_ids: tuple[str, ...] = (), branch: str = "",
                     revision: str = "") -> list[dict[str, Any]]:
        psycopg = self._driver()
        filters = ["c.tenant_id=%s", "c.project_id=%s", "g.status='active'"]
        params: list[Any] = [principal.tenant_id, project_id]
        if not principal.is_admin:
            filters.append("c.acl_subjects && %s")
            params.append(list(principal.subjects))
        if source_ids:
            filters.append("c.source_id = ANY(%s)")
            params.append(list(source_ids))
        if branch:
            filters.append("c.branch=%s")
            params.append(branch)
        if revision:
            filters.append("c.revision=%s")
            params.append(revision)
        sql = f"""
            SELECT c.id,c.path,c.start_line,c.end_line,c.symbol,c.content,c.content_hash,c.source_id,
                   c.branch,c.revision,1-(c.embedding <=> %s::vector) semantic_score,
                   ts_rank_cd(c.search_vector,websearch_to_tsquery('simple',%s)) lexical_score
            FROM rag_chunks_v2 c JOIN rag_generations g ON g.id=c.generation_id
            WHERE {' AND '.join(filters)}
            ORDER BY ((c.embedding <=> %s::vector) -
                ts_rank_cd(c.search_vector,websearch_to_tsquery('simple',%s))) ASC LIMIT %s
        """
        # The vector/query appear twice in the expression.
        vector = self._vector(query_vector)
        query_params = [vector, query, *params, vector, query, max(1, min(limit, 20))]
        async with await psycopg.AsyncConnection.connect(self.dsn) as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(sql, query_params)
                rows = await cursor.fetchall()
                columns = [item.name for item in cursor.description]
        return [dict(zip(columns, row)) for row in rows]
