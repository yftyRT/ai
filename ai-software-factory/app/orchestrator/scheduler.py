"""Asyncio dependency-aware task scheduler.

* runs independent tasks concurrently, never more than `max_active` at once
* every state change is persisted BEFORE the work that depends on it starts
* retryable failures use exponential backoff with jitter (and do not hold an agent slot while waiting)
* per-task timeout, whole-project deadline, cancellation and budget gating
* failed tasks block their dependents (BLOCKED, with the reason)
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import Awaitable, Callable, Optional

from app.execution.workspace import WorkspaceError
from app.providers.base import ProviderError
from app.schemas.models import TERMINAL_TASK_STATES, Task, TaskState

Executor = Callable[[Task], Awaitable[None]]
Persist = Callable[[Task], Awaitable[None]]


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, ProviderError):
        return exc.retryable
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True
    if isinstance(exc, WorkspaceError):  # ownership/path violation: the model can fix it with feedback
        return True
    return False


def reset_interrupted(tasks: list[Task]) -> list[Task]:
    """Restart recovery: tasks that were mid-flight when the process died go back to PENDING."""
    changed = []
    for t in tasks:
        if t.state in (TaskState.RUNNING, TaskState.REVIEWING, TaskState.READY):
            t.state = TaskState.PENDING
            t.started_at = None
            t.error = (t.error + " | " if t.error else "") + "interrupted by restart; re-queued"
            changed.append(t)
    return changed


class TaskScheduler:
    def __init__(
        self,
        tasks: list[Task],
        executor: Executor,
        persist: Persist,
        *,
        max_active: int = 3,
        max_retries: int = 2,
        max_task_seconds: float = 1800,
        max_project_seconds: float = 4 * 3600,
        backoff_base_s: float = 2.0,
        backoff_cap_s: float = 60.0,
        can_start: Callable[[], bool] = lambda: True,
        on_event: Optional[Callable[[str, Task], Awaitable[None]]] = None,
        verifier: Optional[Callable[[Task], Awaitable[None]]] = None,
        clock: Callable[[], float] = time.time,
        rng: Callable[[], float] = random.random,
        poll_s: float = 0.05,
    ):
        self.tasks = {t.id: t for t in tasks}
        self.order = [t.id for t in tasks]
        self.executor = executor
        self.persist = persist
        self.max_active = max(1, max_active)
        self.max_retries = max_retries
        self.max_task_seconds = max_task_seconds
        self.max_project_seconds = max_project_seconds
        self.backoff_base_s = backoff_base_s
        self.backoff_cap_s = backoff_cap_s
        self.can_start = can_start
        self.on_event = on_event
        self.verifier = verifier
        self.clock = clock
        self.rng = rng
        self.poll_s = poll_s
        self._cancel = asyncio.Event()
        self._running: dict[str, asyncio.Task] = {}
        self._not_before: dict[str, float] = {}
        self.peak_active = 0
        self.backoff_delays: list[float] = []
        self.timed_out = False

    # ------------------------------------------------------------------ controls
    def cancel(self) -> None:
        self._cancel.set()

    def backoff_delay(self, attempt: int) -> float:
        """Exponential backoff with equal jitter: delay in [d/2, d], d = min(cap, base * 2**(attempt-1))."""
        d = min(self.backoff_cap_s, self.backoff_base_s * (2 ** max(0, attempt - 1)))
        return d / 2 + self.rng() * d / 2

    # ------------------------------------------------------------------ helpers
    async def _set(self, task: Task, state: TaskState, *, error: Optional[str] = None, event: str = "") -> None:
        task.state = state
        if error is not None:
            task.error = error
        await self.persist(task)  # persist before anything else happens
        if self.on_event and event:
            await self.on_event(event, task)

    def _deps_status(self, task: Task) -> str:
        states = [self.tasks[d].state for d in task.depends_on]
        if any(s in (TaskState.FAILED, TaskState.BLOCKED, TaskState.CANCELLED) for s in states):
            return "blocked"
        if all(s == TaskState.COMPLETED for s in states):
            return "ready"
        return "waiting"

    async def _promote(self) -> None:
        for tid in self.order:
            t = self.tasks[tid]
            if t.state == TaskState.PENDING:
                st = self._deps_status(t)
                if st == "blocked":
                    bad = [d for d in t.depends_on if self.tasks[d].state in (TaskState.FAILED, TaskState.BLOCKED, TaskState.CANCELLED)]
                    await self._set(t, TaskState.BLOCKED, error=f"blocked by failed dependency: {', '.join(bad)}", event="blocked")
                elif st == "ready":
                    await self._set(t, TaskState.READY)

    async def _block_unstarted(self, reason: str) -> None:
        for tid in self.order:
            t = self.tasks[tid]
            if t.state in (TaskState.PENDING, TaskState.READY):
                await self._set(t, TaskState.BLOCKED, error=reason, event="blocked")

    # ------------------------------------------------------------------ one task
    async def _attempt(self, task: Task) -> None:
        task.started_at = self.clock()
        task.ended_at = None
        await self._set(task, TaskState.RUNNING, event="started")
        try:
            await asyncio.wait_for(self.executor(task), timeout=self.max_task_seconds)
            await self._set(task, TaskState.REVIEWING, event="reviewing")
            if self.verifier is not None:
                await self.verifier(task)
        except asyncio.CancelledError:
            task.ended_at = self.clock()
            await self._set(task, TaskState.CANCELLED, error="cancelled", event="cancelled")
            raise
        except BaseException as exc:  # noqa: BLE001
            msg = str(exc) or type(exc).__name__
            if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) and not str(exc):
                msg = f"task exceeded {self.max_task_seconds:.0f}s"
            if is_retryable(exc) and task.retries < self.max_retries:
                task.retries += 1
                delay = self.backoff_delay(task.retries)
                self.backoff_delays.append(delay)
                self._not_before[task.id] = self.clock() + delay
                await self._set(task, TaskState.READY, error=f"attempt {task.retries} failed: {msg}", event="retry")
                return
            task.ended_at = self.clock()
            await self._set(task, TaskState.FAILED, error=msg, event="failed")
            return
        task.ended_at = self.clock()
        task.error = None
        await self._set(task, TaskState.COMPLETED, event="completed")

    # ------------------------------------------------------------------ main loop
    async def run(self) -> dict[str, int]:
        deadline = self.clock() + self.max_project_seconds
        while True:
            self._running = {k: v for k, v in self._running.items() if not v.done()}
            if self._cancel.is_set():
                await self._shutdown_running(TaskState.CANCELLED)
                for tid in self.order:
                    t = self.tasks[tid]
                    if t.state not in TERMINAL_TASK_STATES:
                        await self._set(t, TaskState.CANCELLED, error="project cancelled", event="cancelled")
                break
            if self.clock() > deadline:
                self.timed_out = True
                await self._shutdown_running(TaskState.FAILED, "project time limit reached")
                await self._block_unstarted("project time limit reached")
                break
            await self._promote()
            if not self.can_start():
                await self._block_unstarted("project budget reached: no new tasks are scheduled")
            now = self.clock()
            for tid in self.order:
                if len(self._running) >= self.max_active:
                    break
                t = self.tasks[tid]
                if t.state == TaskState.READY and tid not in self._running and self._not_before.get(tid, 0) <= now:
                    self._running[tid] = asyncio.create_task(self._attempt(t), name=f"task-{tid}")
                    self.peak_active = max(self.peak_active, len(self._running))
            pending_work = any(t.state in (TaskState.PENDING, TaskState.READY, TaskState.RUNNING, TaskState.REVIEWING)
                               for t in self.tasks.values())
            if not pending_work and not self._running:
                break
            if not self._running and not any(t.state == TaskState.READY for t in self.tasks.values()):
                # nothing running or ready but unfinished tasks remain: dependency deadlock guard
                await self._promote()
                if not any(t.state in (TaskState.READY, TaskState.RUNNING) for t in self.tasks.values()):
                    await self._block_unstarted("unsatisfiable dependencies")
                    break
            waiters = set(self._running.values())
            cancel_waiter = asyncio.ensure_future(self._cancel.wait())
            try:
                await asyncio.wait(waiters | {cancel_waiter}, timeout=self.poll_s, return_when=asyncio.FIRST_COMPLETED)
            finally:
                cancel_waiter.cancel()
        return self.tasks_counts()

    async def _shutdown_running(self, state: TaskState, reason: str = "") -> None:
        running = list(self._running.items())
        for _, atask in running:
            atask.cancel()
        for tid, atask in running:
            try:
                await atask
            except BaseException:  # noqa: BLE001 - cancellation expected
                pass
            t = self.tasks[tid]
            if state != TaskState.CANCELLED:
                t.ended_at = self.clock()
                await self._set(t, state, error=reason, event="failed")
        self._running.clear()

    def tasks_counts(self) -> dict[str, int]:
        c = {s.value: 0 for s in TaskState}
        for t in self.tasks.values():
            c[t.state.value] += 1
        return c
