"""Remaining-time estimate from the dependency graph, MEASURED task durations and the current queue.
Never invents precision: with no history it says so; with little history it returns a wide range."""
from __future__ import annotations

import heapq
import time
from typing import Optional

from app.schemas.models import Task, TaskState


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def estimate_remaining(tasks: list[Task], max_active: int, now: Optional[float] = None) -> Optional[tuple[float, float]]:
    """Return (low_s, high_s) or None when there is no measured history yet."""
    now = time.time() if now is None else now
    done = [t.duration for t in tasks if t.state == TaskState.COMPLETED and t.duration is not None]
    if not done:
        return None
    overall = _mean(done)
    by_role: dict[str, list[float]] = {}
    for t in tasks:
        if t.state == TaskState.COMPLETED and t.duration is not None:
            by_role.setdefault(t.agent, []).append(t.duration)

    def est(t: Task) -> float:
        return _mean(by_role.get(t.agent, [])) or overall

    remaining = [t for t in tasks if t.state in (TaskState.PENDING, TaskState.READY, TaskState.RUNNING, TaskState.REVIEWING)]
    if not remaining:
        return 0.0, 0.0
    workers = [0.0] * max(1, max_active)  # time at which each worker becomes free
    heapq.heapify(workers)
    finish: dict[str, float] = {t.id: 0.0 for t in tasks if t.state == TaskState.COMPLETED}
    by_id = {t.id: t for t in tasks}
    for t in remaining:  # running tasks first
        if t.state in (TaskState.RUNNING, TaskState.REVIEWING):
            elapsed = max(0.0, now - (t.started_at or now))
            left = max(est(t) - elapsed, est(t) * 0.1)
            w = heapq.heappop(workers)
            finish[t.id] = w + left
            heapq.heappush(workers, finish[t.id])
    todo = [t for t in remaining if t.id not in finish]
    guard = 0
    while todo and guard < 10_000:
        guard += 1
        progressed = False
        for t in list(todo):
            if all(d in finish for d in t.depends_on if d in by_id):
                ready_at = max([finish[d] for d in t.depends_on if d in finish], default=0.0)
                w = heapq.heappop(workers)
                start = max(w, ready_at)
                finish[t.id] = start + est(t)
                heapq.heappush(workers, finish[t.id])
                todo.remove(t)
                progressed = True
        if not progressed:
            break
    base = max(finish.values(), default=0.0) + overall  # + QA / review / packaging allowance
    spread = 0.25 if len(done) >= 3 else 0.6 if len(done) == 2 else 1.0
    return base * (1 - min(spread, 0.5)), base * (1 + spread)
