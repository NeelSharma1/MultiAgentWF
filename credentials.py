from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import dotenv_values


ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


class LocalCredentialStore:
    """Store provider and skill credentials in an owner-only local env file.

    Values are intentionally available only to trusted server-side callers.
    The HTTP layer uses :meth:`configured` and never serializes the values.
    """
    def __init__(self, path: Path) -> None:
        self.path = path

    @staticmethod
    def validate_name(env_name: str) -> str:
        name = str(env_name or "").strip()
        if not ENV_NAME_RE.fullmatch(name):
            raise ValueError("Credential names must be uppercase environment variable names")
        return name

    def get(self, env_name: str) -> str | None:
        """Resolve one credential for an internal runner without exposing it to clients.

        Read the file first so a long-running MCP process observes credentials
        added by the web process after it was started, then fall back to the
        inherited process environment for deployments that inject secrets there.
        """
        name = self.validate_name(env_name)
        if self.path.exists():
            value = dotenv_values(self.path).get(name)
            if value is not None and str(value).strip():
                return str(value)
        value = os.getenv(name)
        return value if value and value.strip() else None

    def configured(self, env_name: str) -> bool:
        return bool(self.get(env_name))

    def values_for(self, env_names: list[str] | tuple[str, ...] | set[str]) -> dict[str, str]:
        """Return only the requested values to trusted server-side execution."""
        values: dict[str, str] = {}
        for env_name in env_names:
            name = self.validate_name(env_name)
            value = self.get(name)
            if value:
                values[name] = value
        return values

    def save(self, env_name: str, secret: str, *, export_env: bool = True) -> None:
        env_name = self.validate_name(env_name)
        secret = secret.strip()
        if not secret or "\n" in secret or "\r" in secret:
            raise ValueError("A non-empty single-line credential is required")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lines = self.path.read_text().splitlines() if self.path.exists() else []
        replacement = f"{env_name}={secret}"
        output, replaced = [], False
        for line in lines:
            if line.split("=", 1)[0].strip() == env_name:
                output.append(replacement); replaced = True
            else:
                output.append(line)
        if not replaced:
            output.append(replacement)
        self.path.write_text("\n".join(output) + "\n")
        self.path.chmod(0o600)
        if export_env:
            os.environ[env_name] = secret

    def remove(self, env_name: str, *, clear_env: bool = True) -> None:
        env_name = self.validate_name(env_name)
        if self.path.exists():
            lines = [line for line in self.path.read_text().splitlines() if line.split("=", 1)[0].strip() != env_name]
            self.path.write_text("\n".join(lines) + ("\n" if lines else ""))
            self.path.chmod(0o600)
        if clear_env:
            os.environ.pop(env_name, None)
