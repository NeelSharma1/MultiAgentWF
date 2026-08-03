import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

import main


class FakeProjects:
    def __init__(self, root: Path):
        self.root = root

    def get(self, project_id: int):
        return {"id": project_id, "root_path": str(self.root)}


def test_code_execution_returns_stdout_and_runtime_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "projects", FakeProjects(tmp_path))

    success = asyncio.run(main.execute_code(main.CodeExecutionInput(
        code="print('hello from terminal')", language="python", project_id=42
    )))
    failure = asyncio.run(main.execute_code(main.CodeExecutionInput(
        code="raise RuntimeError('visible failure')", language="python", project_id=42
    )))

    assert success["ok"] is True
    assert success["stdout"].strip() == "hello from terminal"
    assert failure["ok"] is False
    assert "visible failure" in failure["stderr"]


def test_code_execution_rejects_non_executable_markdown(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "projects", FakeProjects(tmp_path))

    with pytest.raises(HTTPException) as error:
        asyncio.run(main.execute_code(main.CodeExecutionInput(
            code="# heading", language="markdown", project_id=42
        )))

    assert error.value.status_code == 422
