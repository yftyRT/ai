import asyncio

import pytest

from app.orchestrator.scheduler import TaskScheduler, is_retryable, reset_interrupted
from app.providers.base import ProviderError
from app.schemas.models import Task, TaskState
from tests.helpers import run


def T(i, deps=(), owned=None):
    return Task(id=i, agent="backend", skill="s", description=i, depends_on=list(deps), owned_files=[owned or f"{i}/**"])


async def _noop(task):
    return None


def make(tasks, executor, **kw):
    saved = []

    async def persist(t):
        saved.append((t.id, t.state))
    kw.setdefault("poll_s", 0.01)
    kw.setdefault("backoff_base_s", 0.01)
    kw.setdefault("backoff_cap_s", 0.02)
    return TaskScheduler(tasks, executor, persist, **kw), saved


def test_dependency_order_and_independent_tasks_run_in_parallel():
    async def go():
        order, active, peak = [], 0, 0

        async def ex(t):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            order.append(("start", t.id))
            await asyncio.sleep(0.05)
            order.append(("end", t.id))
            active -= 1
        tasks = [T("a"), T("b", ["a"]), T("c", ["a"]), T("d", ["b", "c"])]
        s, saved = make(tasks, ex, max_active=4)
        counts = await s.run()
        assert counts["COMPLETED"] == 4
        pos = {e: i for i, e in enumerate(order)}
        assert pos[("end", "a")] < pos[("start", "b")] and pos[("end", "a")] < pos[("start", "c")]
        assert pos[("end", "b")] < pos[("start", "d")] and pos[("end", "c")] < pos[("start", "d")]
        assert peak == 2  # b and c ran concurrently
    run(go())


def test_concurrency_limit_is_enforced():
    async def go():
        active, peak = 0, 0

        async def ex(t):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.03)
            active -= 1
        tasks = [T(f"t{i}") for i in range(8)]
        s, _ = make(tasks, ex, max_active=2)
        await s.run()
        assert peak == 2 and s.peak_active == 2
        assert all(t.state == TaskState.COMPLETED for t in tasks)
    run(go())


def test_state_is_persisted_before_work_and_walks_all_states():
    async def go():
        seen_running_before_exec = []
        t = T("a")

        async def ex(task):
            seen_running_before_exec.append(any(sid == "a" and st == TaskState.RUNNING for sid, st in saved))
        s, saved = make([t], ex)
        await s.run()
        assert seen_running_before_exec == [True]
        states = [st for sid, st in saved if sid == "a"]
        assert states[:4] == [TaskState.READY, TaskState.RUNNING, TaskState.REVIEWING, TaskState.COMPLETED]
        assert t.started_at and t.ended_at and t.duration is not None
    run(go())


def test_retry_with_exponential_backoff_and_jitter():
    async def go():
        calls = {"n": 0}

        async def ex(task):
            calls["n"] += 1
            if calls["n"] < 3:
                raise ProviderError("temporary", retryable=True)
        t = T("a")
        s, _ = make([t], ex, max_retries=3, backoff_base_s=1.0, backoff_cap_s=100, rng=lambda: 0.5)
        s.backoff_base_s = 1.0
        # backoff maths (equal jitter): attempt n -> [d/2, d], d = base*2^(n-1)
        assert s.backoff_delay(1) == 0.75 and s.backoff_delay(2) == 1.5 and s.backoff_delay(3) == 3.0
        s.backoff_base_s = 0.01
        s.backoff_cap_s = 0.02
        await s.run()
        assert t.state == TaskState.COMPLETED and t.retries == 2 and calls["n"] == 3
        assert len(s.backoff_delays) == 2
        lo = TaskScheduler([T("z")], _noop, _noop, rng=lambda: 0.0, backoff_base_s=2, backoff_cap_s=60)
        hi = TaskScheduler([T("z")], _noop, _noop, rng=lambda: 1.0, backoff_base_s=2, backoff_cap_s=60)
        assert lo.backoff_delay(3) == 4.0 and hi.backoff_delay(3) == 8.0 and hi.backoff_delay(10) == 60.0
    run(go())


def test_retries_exhausted_fails_task_and_blocks_dependents():
    async def go():
        async def ex(task):
            if task.id == "a":
                raise ProviderError("model down", retryable=True)
        tasks = [T("a"), T("b", ["a"]), T("c", ["b"]), T("x")]
        s, _ = make(tasks, ex, max_retries=2)
        counts = await s.run()
        by = {t.id: t for t in tasks}
        assert by["a"].state == TaskState.FAILED and by["a"].retries == 2
        assert by["b"].state == TaskState.BLOCKED and by["c"].state == TaskState.BLOCKED
        assert "dependency" in by["b"].error
        assert by["x"].state == TaskState.COMPLETED  # unrelated work still finishes
        assert counts["FAILED"] == 1 and counts["BLOCKED"] == 2
    run(go())


def test_non_retryable_error_fails_immediately():
    async def go():
        n = {"c": 0}

        async def ex(task):
            n["c"] += 1
            raise ValueError("bug")
        t = T("a")
        s, _ = make([t], ex, max_retries=5)
        await s.run()
        assert t.state == TaskState.FAILED and n["c"] == 1 and t.retries == 0
        assert not is_retryable(ValueError()) and is_retryable(asyncio.TimeoutError())
        assert not is_retryable(ProviderError("bad key", retryable=False))
    run(go())


def test_task_timeout():
    async def go():
        async def ex(task):
            await asyncio.sleep(5)
        t = T("a")
        s, _ = make([t], ex, max_task_seconds=0.05, max_retries=0)
        await s.run()
        assert t.state == TaskState.FAILED and "exceeded" in t.error
    run(go())


def test_project_deadline_stops_everything():
    async def go():
        async def ex(task):
            await asyncio.sleep(5)
        tasks = [T("a"), T("b", ["a"])]
        s, _ = make(tasks, ex, max_project_seconds=0.1, max_task_seconds=30)
        await asyncio.wait_for(s.run(), 5)
        assert s.timed_out and tasks[0].state == TaskState.FAILED and tasks[1].state == TaskState.BLOCKED
    run(go())


def test_cancel_marks_everything_cancelled():
    async def go():
        async def ex(task):
            await asyncio.sleep(5)
        tasks = [T("a"), T("b", ["a"])]
        s, _ = make(tasks, ex)
        job = asyncio.create_task(s.run())
        await asyncio.sleep(0.1)
        s.cancel()
        await asyncio.wait_for(job, 5)
        assert all(t.state == TaskState.CANCELLED for t in tasks)
    run(go())


def test_budget_gate_stops_scheduling_new_tasks():
    async def go():
        allowed = {"v": True}

        async def ex(task):
            allowed["v"] = False  # budget used up by the first task
        tasks = [T("a"), T("b", ["a"])]
        s, _ = make(tasks, ex, can_start=lambda: allowed["v"])
        await s.run()
        assert tasks[0].state == TaskState.COMPLETED
        assert tasks[1].state == TaskState.BLOCKED and "budget" in tasks[1].error
    run(go())


def test_restart_recovery_resets_interrupted_tasks_and_resumes():
    async def go():
        tasks = [T("a"), T("b", ["a"]), T("c", ["b"])]
        tasks[0].state = TaskState.COMPLETED
        tasks[1].state = TaskState.RUNNING  # process died mid-task
        tasks[1].started_at = 1.0
        tasks[2].state = TaskState.READY
        changed = reset_interrupted(tasks)
        assert {t.id for t in changed} == {"b", "c"}
        assert tasks[1].state == TaskState.PENDING and "restart" in tasks[1].error
        ran = []

        async def ex(task):
            ran.append(task.id)
        s, _ = make(tasks, ex)
        await s.run()
        assert ran == ["b", "c"]  # completed work is not repeated
    run(go())
