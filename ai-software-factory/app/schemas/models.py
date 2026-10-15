"""Validated Pydantic schemas shared by planner, scheduler, storage and bot."""
from __future__ import annotations

import fnmatch
import re
import time
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class TaskState(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    REVIEWING = "REVIEWING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


TERMINAL_TASK_STATES = {TaskState.COMPLETED, TaskState.FAILED, TaskState.BLOCKED, TaskState.CANCELLED}


class ProjectState(str, Enum):
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    RUNNING = "RUNNING"
    INTEGRATING = "INTEGRATING"
    TESTING = "TESTING"
    REPAIRING = "REPAIRING"
    REVIEWING = "REVIEWING"
    PACKAGING = "PACKAGING"
    DELIVERING = "DELIVERING"
    # terminal
    SUCCESS = "SUCCESS"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    UNRESOLVED_ISSUES = "UNRESOLVED_ISSUES"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


FINAL_PROJECT_STATES = {
    ProjectState.SUCCESS,
    ProjectState.PARTIAL_SUCCESS,
    ProjectState.UNRESOLVED_ISSUES,
    ProjectState.FAILED,
    ProjectState.CANCELLED,
}
ACTIVE_PROJECT_STATES = {
    ProjectState.RUNNING,
    ProjectState.INTEGRATING,
    ProjectState.TESTING,
    ProjectState.REPAIRING,
    ProjectState.REVIEWING,
    ProjectState.PACKAGING,
    ProjectState.DELIVERING,
}

ROLES = (
    "architect",
    "frontend",
    "backend",
    "database",
    "api_integrator",
    "security",
    "tester",
    "reviewer",
    "docs",
)
_ID_RE = re.compile(r"^[a-z][a-z0-9_\-]{0,47}$")


def now() -> float:
    return time.time()


class Task(BaseModel):
    id: str
    title: str = ""
    agent: str  # role, one of ROLES
    skill: str  # skill name resolved by the registry
    description: str
    depends_on: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    owned_files: list[str] = Field(default_factory=list)  # relative paths or globs
    required_tools: list[str] = Field(default_factory=lambda: ["write_files"])
    state: TaskState = TaskState.PENDING
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    retries: int = 0
    repair_rounds: int = 0
    test_status: Literal["not_run", "passed", "failed"] = "not_run"
    error: Optional[str] = None
    skill_version: Optional[str] = None
    est_seconds: float = 120.0
    output_files: list[str] = Field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not _ID_RE.match(v):
            raise ValueError(f"invalid task id {v!r}")
        return v

    @field_validator("agent")
    @classmethod
    def _agent(cls, v: str) -> str:
        if v not in ROLES:
            raise ValueError(f"unknown agent role {v!r}")
        return v

    @field_validator("owned_files")
    @classmethod
    def _owned(cls, v: list[str]) -> list[str]:
        out = []
        for p in v:
            p = p.strip().replace("\\", "/")
            if not p or p.startswith("/") or ".." in p.split("/") or re.match(r"^[A-Za-z]:", p):
                raise ValueError(f"unsafe owned file pattern {p!r}")
            out.append(p)
        return out

    @property
    def duration(self) -> Optional[float]:
        if self.started_at is not None and self.ended_at is not None:
            return max(0.0, self.ended_at - self.started_at)
        return None


def patterns_overlap(a: str, b: str) -> bool:
    """Conservative overlap test for two glob patterns."""
    if a == b:
        return True
    return fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a)


class PlanError(ValueError):
    pass


class Plan(BaseModel):
    project_name: str
    summary: str = ""
    language: str = "python"
    architecture: str = ""  # architecture specification (markdown)
    tasks: list[Task]
    source: Literal["model", "heuristic"] = "model"

    @field_validator("project_name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = re.sub(r"[^A-Za-z0-9_\-]+", "-", v.strip()).strip("-").lower()
        if not v:
            raise ValueError("project_name is empty")
        return v[:48]

    @model_validator(mode="after")
    def _validate_graph(self) -> "Plan":
        if not self.tasks:
            raise PlanError("plan has no tasks")
        ids = [t.id for t in self.tasks]
        if len(set(ids)) != len(ids):
            raise PlanError("duplicate task ids")
        idset = set(ids)
        for t in self.tasks:
            for d in t.depends_on:
                if d not in idset:
                    raise PlanError(f"task {t.id} depends on unknown task {d}")
                if d == t.id:
                    raise PlanError(f"task {t.id} depends on itself")
        cyc = find_cycle({t.id: t.depends_on for t in self.tasks})
        if cyc:
            raise PlanError("circular dependency: " + " -> ".join(cyc))
        # file ownership must not overlap between tasks (no concurrent overwrite)
        owners: list[tuple[str, str]] = []
        for t in self.tasks:
            for p in t.owned_files:
                for other_pat, other_id in owners:
                    if other_id != t.id and patterns_overlap(p, other_pat):
                        raise PlanError(f"file ownership overlap: {p!r} ({t.id}) vs {other_pat!r} ({other_id})")
                owners.append((p, t.id))
        return self

    def task(self, task_id: str) -> Task:
        for t in self.tasks:
            if t.id == task_id:
                return t
        raise KeyError(task_id)


def find_cycle(graph: dict[str, list[str]]) -> Optional[list[str]]:
    """Return one cycle as a list of node ids, or None. Iterative DFS."""
    WHITE, GREY, BLACK = 0, 1, 2
    color = {n: WHITE for n in graph}
    parent: dict[str, Optional[str]] = {}
    for start in graph:
        if color[start] != WHITE:
            continue
        stack = [(start, iter(graph[start]))]
        color[start] = GREY
        parent[start] = None
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                color[node] = BLACK
                stack.pop()
                continue
            if nxt not in color:
                continue
            if color[nxt] == GREY:
                cycle = [nxt, node]
                cur = node
                while cur != nxt and parent.get(cur) is not None:
                    cur = parent[cur]  # type: ignore[assignment]
                    cycle.append(cur)
                return list(reversed(cycle))
            if color[nxt] == WHITE:
                color[nxt] = GREY
                parent[nxt] = node
                stack.append((nxt, iter(graph[nxt])))
    return None


class Finding(BaseModel):
    severity: Literal["high", "medium", "low", "info"]
    file: str = ""
    evidence: str = ""
    explanation: str
    suggested_fix: str = ""
    category: str = "general"
    source: Literal["static", "model"] = "static"


class CheckResult(BaseModel):
    name: str
    command: str
    exit_code: int
    passed: bool
    output: str = ""
    duration_s: float = 0.0
    skipped: bool = False
    required: bool = True


class TestReport(BaseModel):
    __test__ = False  # not a pytest class
    language: str = "python"
    checks: list[CheckResult] = Field(default_factory=list)
    generated_at: float = Field(default_factory=now)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks if c.required and not c.skipped) and any(
            not c.skipped for c in self.checks
        )

    @property
    def failed_checks(self) -> list[CheckResult]:
        return [c for c in self.checks if c.required and not c.skipped and not c.passed]


class SkillUse(BaseModel):
    role: str
    skill: str
    version: str = ""
    origin: Literal[
        "already_installed", "newly_imported", "reused_from_previous_project", "generated_locally"
    ] = "already_installed"
    source_url: str = ""
    commit: str = ""
    license: str = ""
    summary: str = ""


class Project(BaseModel):
    id: str
    owner_id: int
    chat_id: int
    prompt: str
    state: ProjectState = ProjectState.CREATED
    plan: Optional[Plan] = None
    created_at: float = Field(default_factory=now)
    updated_at: float = Field(default_factory=now)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    estimated_cost_usd: float = 0.0  # pre-run estimate
    budget_usd: float = 5.0
    spent_tokens_usd: float = 0.0  # estimated from token usage
    repair_cycles: int = 0
    final_status: Optional[str] = None
    summary: str = ""
    error: Optional[str] = None
    artifact_path: Optional[str] = None
    delivered: bool = False
    delivery_error: Optional[str] = None
    skills_used: list[SkillUse] = Field(default_factory=list)
    latest_status_text: str = ""
    status_message_id: Optional[int] = None
    notes: list[str] = Field(default_factory=list)
    test_report: Optional[TestReport] = None
    findings: list[Finding] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)
