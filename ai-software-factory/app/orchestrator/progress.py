"""Status rendering + rate-limited Telegram progress updates."""
from __future__ import annotations

import time
from typing import Callable, Optional

from app.bot.messenger import Messenger
from app.config import Settings
from app.orchestrator.budget import BudgetTracker
from app.orchestrator.estimator import estimate_remaining
from app.schemas.models import FINAL_PROJECT_STATES, Project, Task, TaskState
from app.util import fmt_duration


def _fmt_range(r: Optional[tuple[float, float]]) -> str:
    if r is None:
        return "Calculating estimate"
    lo, hi = r
    if hi <= 0:
        return "almost done"
    return f"~{fmt_duration(lo)} - {fmt_duration(hi)} (estimate)"


def render_status(project: Project, tasks: list[Task], settings: Settings, budget: BudgetTracker,
                  now: Optional[float] = None) -> str:
    now = time.time() if now is None else now
    c = {s.value: 0 for s in TaskState}
    for t in tasks:
        c[t.state.value] += 1
    running = [t for t in tasks if t.state in (TaskState.RUNNING, TaskState.REVIEWING)]
    started = project.started_at
    elapsed = ((project.finished_at or now) - started) if started else 0
    spent = budget.spent(project)
    retries = sum(t.retries for t in tasks)
    blockers = [f"{t.id}: {(t.error or t.state.value)[:90]}" for t in tasks
                if t.state in (TaskState.BLOCKED, TaskState.FAILED)]
    lines = [
        f"Project {project.id} - {project.plan.project_name if project.plan else '(planning)'}",
        f"State: {project.state.value}",
        f"Active agents: {', '.join(sorted({t.agent for t in running})) or 'none'}",
        f"Tasks: {c['COMPLETED']} done, {c['RUNNING'] + c['REVIEWING']} running, {c['PENDING'] + c['READY']} pending, "
        f"{c['FAILED']} failed, {c['BLOCKED']} blocked, {c['CANCELLED']} cancelled (of {len(tasks)})",
    ]
    if running:
        lines.append("Now: " + "; ".join(f"{t.id} ({t.title or t.description[:40]})" for t in running[:4]))
    lines.append(f"Elapsed: {fmt_duration(elapsed)}")
    if project.state not in FINAL_PROJECT_STATES:
        lines.append("Remaining: " + _fmt_range(estimate_remaining(tasks, settings.max_active_agents, now)))
    lines.append(f"Retries used: {retries}; repair cycles: {project.repair_cycles}/{settings.max_repair_cycles}")
    lines.append("Blockers: " + ("; ".join(blockers[:4]) if blockers else "none"))
    lines.append(f"Cost (ESTIMATE, not billed): Pod ${spent.infra_usd:.3f} + tokens ${spent.tokens_usd:.4f} "
                 f"of ${project.budget_usd:.2f} limit. Real bill: RunPod console.")
    return "\n".join(lines)


class ProgressReporter:
    """Edits ONE status message per project; at most one update per `min_interval_s`."""

    def __init__(self, messenger: Messenger, storage, settings: Settings, budget: BudgetTracker,
                 clock: Callable[[], float] = time.monotonic):
        self.messenger = messenger
        self.storage = storage
        self.s = settings
        self.budget = budget
        self.clock = clock
        self._last_sent: dict[str, float] = {}
        self._last_text: dict[str, str] = {}

    async def update(self, project: Project, tasks: list[Task], *, force: bool = False) -> bool:
        text = render_status(project, tasks, self.s, self.budget)
        project.latest_status_text = text  # persisted with the project
        now = self.clock()
        if not force:
            if text == self._last_text.get(project.id):
                return False
            if now - self._last_sent.get(project.id, -1e9) < self.s.progress_min_interval_s:
                return False
        self._last_sent[project.id] = now
        self._last_text[project.id] = text
        try:
            if project.status_message_id:
                ok = await self.messenger.edit_text(project.chat_id, project.status_message_id, text)
                if ok:
                    return True
            mid = await self.messenger.send_text(project.chat_id, text)
            if mid:
                project.status_message_id = mid
            return mid is not None
        except Exception:  # noqa: BLE001 - progress must never break the build
            return False
