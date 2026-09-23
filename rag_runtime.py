"""Background indexing coordination for the local RAG runtime."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any, Awaitable, Callable

from rag import RagStore

try:
    from watchfiles import awatch
except ImportError:  # pragma: no cover - uvicorn[standard] normally provides it
    awatch = None


@dataclass(frozen=True)
class IndexRequest:
    project_id: int
    job_id: str
    force: bool
    reason: str


class LocalRagIndexCoordinator:
    """Debounced, durable-job-backed indexing outside the chat request path."""

    def __init__(
        self,
        store: RagStore,
        indexer: Callable[..., Awaitable[dict[str, Any]]],
        list_projects: Callable[[], list[dict[str, Any]]],
        resolve_root: Callable[[dict[str, Any]], Path],
        *,
        reconcile_seconds: float = 60.0,
    ) -> None:
        self.store = store
        self.indexer = indexer
        self.list_projects = list_projects
        self.resolve_root = resolve_root
        self.reconcile_seconds = max(5.0, float(reconcile_seconds))
        self.queue: asyncio.Queue[IndexRequest] = asyncio.Queue()
        self._queued_projects: set[int] = set()
        self._worker: asyncio.Task | None = None
        self._reconciler: asyncio.Task | None = None
        self._watchers: dict[int, asyncio.Task] = {}
        self._last_change_at: dict[int, float] = {}
        self._closed = False
        self._cancelled_projects: set[int] = set()

    async def start(self) -> None:
        if self._worker and not self._worker.done():
            return
        self._closed = False
        self._worker = asyncio.create_task(self._run_worker(), name="rag-local-index-worker")
        self._reconciler = asyncio.create_task(self._run_reconciler(), name="rag-index-reconciler")
        for job in self.store.queued_jobs():
            project_id = int(job["project_id"])
            if project_id in self._queued_projects:
                continue
            self._queued_projects.add(project_id)
            await self.queue.put(IndexRequest(
                project_id, str(job["id"]), False, str(job.get("reason") or "restart_recovery"),
            ))
        await self.refresh_watchers()
        # A server restart must reconcile every enabled source tree.  The
        # indexer hashes documents and skips unchanged chunks, so this is safe
        # to run on every boot without re-embedding the whole project.
        for project in self.list_projects():
            if not bool(project.get("rag_enabled")):
                continue
            try:
                if not self.resolve_root(project).is_dir():
                    continue
            except Exception:
                continue
            await self.schedule(int(project["id"]), reason="startup_reconcile")

    async def stop(self) -> None:
        self._closed = True
        tasks = [task for task in [self._worker, self._reconciler, *self._watchers.values()] if task]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._worker = None
        self._reconciler = None
        self._watchers.clear()

    async def refresh_watchers(self) -> set[int]:
        """Refresh project watchers and report watchers recovered after failure."""
        enabled: dict[int, Path] = {}
        for project in self.list_projects():
            if not bool(project.get("rag_enabled")):
                continue
            try:
                root = self.resolve_root(project)
            except Exception:
                continue
            if root.is_dir():
                enabled[int(project["id"])] = root
        for project_id, task in list(self._watchers.items()):
            if project_id not in enabled:
                task.cancel()
                self._watchers.pop(project_id, None)
        if awatch is None:
            return set()
        recovered: set[int] = set()
        for project_id, root in enabled.items():
            previous = self._watchers.get(project_id)
            if previous is None or previous.done():
                self._watchers[project_id] = asyncio.create_task(
                    self._watch(project_id, root), name=f"rag-watch-{project_id}",
                )
                if previous is not None:
                    recovered.add(project_id)
        return recovered

    async def schedule(self, project_id: int, *, force: bool = False,
                       reason: str = "source_change") -> dict[str, Any]:
        self._cancelled_projects.discard(project_id)
        if project_id in self._queued_projects:
            latest = self.store.latest_active_job(project_id)
            if latest:
                return latest
        job = self.store.create_job(project_id, reason=reason)
        self._queued_projects.add(project_id)
        await self.queue.put(IndexRequest(project_id, str(job["id"]), force, reason))
        return job

    def cancel_project(self, project_id: int) -> None:
        self._cancelled_projects.add(project_id)
        self._queued_projects.discard(project_id)
        active = self.store.latest_active_job(project_id)
        if active:
            self.store._update_job(str(active["id"]), status="error", error="Indexing disabled")

    def status(self, project_id: int) -> dict[str, Any]:
        return {
            "mode": "background",
            "watching": project_id in self._watchers and not self._watchers[project_id].done(),
            "queued": project_id in self._queued_projects,
            "queue_depth": self.queue.qsize(),
            "last_change_at": self._last_change_at.get(project_id, 0.0),
            "reconcile_seconds": self.reconcile_seconds,
        }

    async def _watch(self, project_id: int, root: Path) -> None:
        assert awatch is not None
        try:
            async for changes in awatch(
                root, debounce=2000, step=500, recursive=True,
                watch_filter=self._watch_filter(root),
            ):
                if self._closed:
                    return
                if not changes:
                    continue
                self._last_change_at[project_id] = time.time()
                await self.schedule(project_id, reason="filesystem_change")
        except asyncio.CancelledError:
            raise
        except Exception:
            # Reconciliation remains active if native file watching fails.
            return

    @staticmethod
    def _watch_filter(root: Path) -> Callable[[Any, str], bool]:
        """Ignore MAW's local state without excluding project source files."""
        resolved_root = root.expanduser().resolve()

        def accepts(_change: Any, changed_path: str) -> bool:
            try:
                relative = Path(changed_path).resolve(strict=False).relative_to(resolved_root)
            except (OSError, ValueError):
                return False
            return not relative.parts or relative.parts[0].casefold() != "maw"

        return accepts

    async def _run_reconciler(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.reconcile_seconds)
                recovered = await self.refresh_watchers()
                # A restarted watcher may have missed a source update while it
                # was unavailable. Reconcile once, but do not periodically
                # probe/embed a stable project just to keep the watcher alive.
                for project_id in recovered:
                    await self.schedule(project_id, reason="watch_recovery")
            except asyncio.CancelledError:
                raise
            except Exception:
                continue

    async def _run_worker(self) -> None:
        while True:
            request = await self.queue.get()
            try:
                if request.project_id in self._cancelled_projects:
                    self.store._update_job(request.job_id, status="error", error="Indexing disabled")
                    continue
                await self.indexer(
                    request.project_id, force=request.force, job_id=request.job_id,
                )
            except asyncio.CancelledError:
                self.store._update_job(request.job_id, status="error", error="Server stopped during indexing")
                raise
            except Exception as exc:
                self.store._update_job(request.job_id, status="error", error=str(exc)[:1000])
            finally:
                self._queued_projects.discard(request.project_id)
                self.queue.task_done()
