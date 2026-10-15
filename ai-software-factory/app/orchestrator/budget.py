"""Cost-control layer.

Honest accounting: the Pod is billed by RunPod whether or not it is busy. Numbers here are
ESTIMATES derived from the configured hourly price and token counts; the real bill is the
RunPod console. Nothing here provisions GPUs or Pods.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from app.config import Settings
from app.schemas.models import Project


class BudgetExceeded(Exception):
    pass


@dataclass
class CostEstimate:
    infra_usd: float
    tokens_usd: float

    @property
    def total(self) -> float:
        return self.infra_usd + self.tokens_usd


class BudgetTracker:
    def __init__(self, settings: Settings, clock=time.time):
        self.s = settings
        self.clock = clock
        self.assert_gpu_budget()

    def assert_gpu_budget(self) -> None:
        """Hard cap: refuse to run if the configured Pod price exceeds the hourly budget."""
        if self.s.gpu_hourly_usd > self.s.gpu_budget_usd_per_hour:
            raise BudgetExceeded(
                f"Pod price ${self.s.gpu_hourly_usd:.2f}/h exceeds hard budget ${self.s.gpu_budget_usd_per_hour:.2f}/h"
            )

    # ---- numbers --------------------------------------------------------
    def token_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (
            prompt_tokens / 1e6 * self.s.token_price_in_per_m
            + completion_tokens / 1e6 * self.s.token_price_out_per_m
        )

    def infra_cost_for_seconds(self, seconds: float) -> float:
        return max(0.0, seconds) / 3600.0 * self.s.gpu_hourly_usd

    def estimate_before_run(self, project: Project, avg_task_seconds: float = 120.0) -> CostEstimate:
        """Pre-run estimate: assumes the project occupies the Pod for its critical path."""
        if project.plan is None:
            return CostEstimate(0.0, 0.0)
        tasks = project.plan.tasks
        total = sum(t.est_seconds or avg_task_seconds for t in tasks)
        parallel = max(1, self.s.max_active_agents)
        wall = max(total / parallel, max((t.est_seconds for t in tasks), default=0))
        wall *= 1.5  # QA + review + repair overhead
        tokens = len(tasks) * 12000
        return CostEstimate(self.infra_cost_for_seconds(wall), self.token_cost(tokens, tokens // 2))

    def spent(self, project: Project) -> CostEstimate:
        elapsed = 0.0
        if project.started_at:
            end = project.finished_at or self.clock()
            elapsed = end - project.started_at
        return CostEstimate(self.infra_cost_for_seconds(elapsed), project.spent_tokens_usd)

    def can_schedule(self, project: Project) -> bool:
        return self.spent(project).total < project.budget_usd

    def check(self, project: Project) -> None:
        sp = self.spent(project)
        if sp.total >= project.budget_usd:
            raise BudgetExceeded(
                f"project budget reached: estimated ${sp.total:.2f} >= limit ${project.budget_usd:.2f}"
            )
