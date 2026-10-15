"""ProjectManager: prompt -> validated task graph, with dynamic role and Skill selection.

Skill selection order (spec section F):
  1. identify the specialist role of each task
  2. look in the local SkillRegistry (approved + enabled Skills only)
  3. if an installed imported Skill exists, use it; otherwise the built-in Skill is the baseline
  4. search GitHub ONLY when no imported Skill exists for the role and SKILLS_AUTO_DISCOVER is on
     (discovery never activates anything by itself; auto-import needs the strict `auto_strict` policy)
  5. attach the chosen Skill to the task and record its version
A Skill can never change task permissions: ownership is validated against code-defined role policy.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from pydantic import ValidationError

from app.agents.base import AgentOutputError, extract_json
from app.agents.roles import ROLE_POLICY, path_policy_errors, select_roles
from app.config import Settings
from app.providers.base import ChatMessage, ModelProvider
from app.schemas.models import Plan, PlanError, Project, SkillUse, Task
from app.skills.discovery import SkillDiscoveryService
from app.skills.importer import SkillImporter
from app.skills.registry import SkillEntry, SkillRegistry
from app.util import truncate

log = logging.getLogger(__name__)

USAGE_COLLECTION = "skill_usage"


class PlanningError(Exception):
    """The model could not produce a valid plan. Surfaced to the user; never silently replaced."""


PLAN_PROMPT = """### REQUEST: PLAN
You are the Project Manager of a multi-agent software factory. Turn the user's request into a task graph.
Respond with ONE JSON object and nothing else:
{{"project_name": "short-kebab-name", "summary": "...", "language": "python",
  "architecture": "markdown architecture spec: structure, API contracts, data schema, shared interfaces, env vars, testing expectations",
  "tasks": [{{"id": "arch", "title": "...", "agent": "<role>", "description": "...", "depends_on": [],
              "acceptance_criteria": ["..."], "owned_files": ["glob", "..."], "est_seconds": 60}}]}}

RULES
- Use ONLY these roles (a role not listed is forbidden): {roles}
- Exactly one task with agent "architect" first (it has no dependencies); every other task depends on it directly or transitively.
- At most {max_tasks} tasks. Do not create unnecessary agents for a simple project.
- Tasks that may run in parallel must NOT own overlapping files. Put shared files under ONE owner.
- Tests (role tester) depend on the code they test; docs (role docs) depend on the code they describe.
- Default language is Python (standard library preferred, pin any dependency with ==).
- owned_files must be relative globs and obey these per-role limits:
{limits}
- Task ids are lowercase letters, digits, '-' or '_'.
{feedback}
USER REQUEST:
{prompt}
"""


class ProjectManager:
    def __init__(
        self,
        provider: ModelProvider,
        registry: SkillRegistry,
        settings: Settings,
        *,
        discovery: Optional[SkillDiscoveryService] = None,
        importer: Optional[SkillImporter] = None,
        storage: Any = None,
    ):
        self.provider = provider
        self.registry = registry
        self.s = settings
        self.discovery = discovery
        self.importer = importer
        self.storage = storage

    # ------------------------------------------------------------------ planning
    def allowed_roles(self, prompt: str) -> list[str]:
        return select_roles(prompt)

    def _limits_text(self, roles: list[str]) -> str:
        lines = []
        for r in roles:
            pol = ROLE_POLICY[r]
            lines.append(f"  * {r}: allowed {list(pol.allow)}" + (f", never {list(pol.deny)[2:]}" if len(pol.deny) > 2 else ""))
        return "\n".join(lines)

    def validate_plan_dict(self, data: Any, roles: list[str]) -> Plan:
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
            raise PlanError('plan must be a JSON object with a "tasks" list')
        if len(data["tasks"]) > self.s.max_tasks_per_plan:
            raise PlanError(f"plan has {len(data['tasks'])} tasks, limit is {self.s.max_tasks_per_plan}")
        tasks: list[Task] = []
        for raw in data["tasks"]:
            if not isinstance(raw, dict):
                raise PlanError("each task must be an object")
            agent = raw.get("agent")
            if agent not in roles:
                raise PlanError(f"task {raw.get('id')!r} uses role {agent!r} which is not allowed for this project (allowed: {roles})")
            errs = path_policy_errors(agent, list(raw.get("owned_files") or []))
            if errs:
                raise PlanError("; ".join(errs))
            if not raw.get("owned_files"):
                raise PlanError(f"task {raw.get('id')!r} has no owned_files")
            try:
                tasks.append(Task(
                    id=str(raw.get("id", "")), title=str(raw.get("title", ""))[:120], agent=agent, skill="pending",
                    description=str(raw.get("description", "")), depends_on=list(raw.get("depends_on") or []),
                    acceptance_criteria=[str(c) for c in raw.get("acceptance_criteria") or []],
                    owned_files=[str(p) for p in raw["owned_files"]],
                    est_seconds=float(raw.get("est_seconds") or 120.0),
                ))
            except (ValidationError, ValueError, TypeError) as exc:
                raise PlanError(f"invalid task {raw.get('id')!r}: {exc}") from None
        architects = [t for t in tasks if t.agent == "architect"]
        if len(architects) != 1:
            raise PlanError("the plan needs exactly one architect task")
        arch = architects[0]
        if arch.depends_on:
            raise PlanError("the architect task must have no dependencies")
        for t in tasks:
            if t is not arch and arch.id not in _ancestors(t, {x.id: x for x in tasks}):
                raise PlanError(f"task {t.id} must depend (directly or indirectly) on the architect task {arch.id}")
        try:
            return Plan(
                project_name=str(data.get("project_name") or "project"),
                summary=str(data.get("summary", ""))[:1000],
                language=str(data.get("language") or "python").lower(),
                architecture=str(data.get("architecture", "")),
                tasks=tasks, source="model",
            )
        except (ValidationError, ValueError) as exc:
            # PlanError (cycles, ownership overlap) surfaces through pydantic as ValidationError
            raise PlanError(_clean_validation_error(exc)) from None

    async def plan(self, project: Project) -> Plan:
        roles = self.allowed_roles(project.prompt)
        feedback = ""
        last_err = ""
        for attempt in range(2):
            prompt = PLAN_PROMPT.format(
                roles=", ".join(roles), max_tasks=self.s.max_tasks_per_plan, limits=self._limits_text(roles),
                feedback=feedback, prompt=truncate(project.prompt, 6000))
            res = await self.provider.chat([ChatMessage("system", "You are a precise planning engine. Output JSON only."),
                                            ChatMessage("user", prompt)],
                                           max_tokens=min(4096, self.s.max_output_tokens), temperature=0.2, json_mode=True)
            try:
                plan = self.validate_plan_dict(extract_json(res.text), roles)
                break
            except (PlanError, AgentOutputError) as exc:
                last_err = str(exc)
                feedback = f"\nYOUR PREVIOUS PLAN WAS REJECTED: {last_err}\nFix exactly that problem.\n"
        else:
            raise PlanningError(f"the model could not produce a valid plan: {last_err}")
        await self.assign_skills(project, plan)
        return plan

    # ------------------------------------------------------------------ skills
    def _best_entry(self, role: str) -> Optional[SkillEntry]:
        entries = self.registry.for_role(role)
        imported = sorted((e for e in entries if e.origin == "imported"), key=lambda e: -e.score)
        if imported:
            return imported[0]
        builtin = [e for e in entries if e.origin == "builtin"]
        return builtin[0] if builtin else None

    async def _previously_used(self, skill: str) -> bool:
        if self.storage is None:
            return False
        return await self.storage.get_doc(USAGE_COLLECTION, skill) is not None

    async def _mark_used(self, skill: str, project_id: str) -> None:
        if self.storage is None:
            return
        doc = await self.storage.get_doc(USAGE_COLLECTION, skill) or {"projects": []}
        if project_id not in doc["projects"]:
            doc["projects"] = (doc["projects"] + [project_id])[-50:]
        await self.storage.put_doc(USAGE_COLLECTION, skill, doc)

    async def assign_skills(self, project: Project, plan: Plan) -> list[SkillUse]:
        uses: dict[str, SkillUse] = {}
        for role in sorted({t.agent for t in plan.tasks}):
            uses[role] = await self._skill_for_role(project, plan, role)
        for t in plan.tasks:
            use = uses[t.agent]
            t.skill = use.skill
            t.skill_version = use.version
        project.skills_used = list(uses.values())
        for u in project.skills_used:
            await self._mark_used(u.skill, project.id)
        return project.skills_used

    async def _skill_for_role(self, project: Project, plan: Plan, role: str) -> SkillUse:
        entry = self._best_entry(role)
        searched_without_result = False
        notes = ""
        newly_imported = False
        if (entry is None or entry.origin == "builtin") and self.s.skills_auto_discover and self.discovery is not None:
            try:
                res = await asyncio.wait_for(
                    self.discovery.discover(f"{ROLE_POLICY[role].title} {plan.language}", role=role,
                                            project_context=f"{plan.language} {project.prompt[:400]}"),
                    timeout=60)
                usable = res.usable
                if usable:
                    best = usable[0]
                    notes = f"GitHub candidate {best['id']} awaiting administrator review"
                    if self.importer is not None and self.s.skill_trust_policy == "auto_strict":
                        imp = await self.importer.import_candidate(best["id"], auto=True)
                        if imp.ok and imp.entry is not None:
                            entry, newly_imported, notes = imp.entry, True, "imported automatically under strict policy"
                else:
                    searched_without_result = True
            except Exception as exc:  # noqa: BLE001 - discovery must never break planning
                log.warning("skill discovery failed for role %s: %s", role, exc)
                notes = "GitHub discovery unavailable; using installed Skill"
        if entry is None:
            raise PlanningError(f"no enabled Skill is installed for role {role!r}")
        rec = entry.record or {}
        if entry.origin == "imported":
            origin = "newly_imported" if newly_imported else (
                "reused_from_previous_project" if await self._previously_used(entry.name) else "already_installed")
        else:
            origin = "generated_locally" if searched_without_result else "already_installed"
        return SkillUse(
            role=role, skill=entry.name, version=entry.version, origin=origin,
            source_url=str(rec.get("repo_url", "")), commit=str(rec.get("commit_sha", "")),
            license=str(rec.get("license", "")),
            summary=notes or (f"score {entry.score:.0f}" if entry.origin == "imported" else "built-in Skill"),
        )

    def skills_for_task(self, task: Task) -> list:
        """Primary Skill + at most one compatible supplementary imported Skill (never every Skill)."""
        primary = self.registry.get_skill(task.skill)
        if primary is None:
            primary = next(iter(self.registry.for_role(task.agent)), None)
            primary = self.registry.get_skill(primary.name) if primary else None
        if primary is None:
            raise PlanningError(f"Skill {task.skill!r} for task {task.id} is not available (disabled or removed)")
        extras = []
        for e in self.registry.for_role(task.agent):
            if e.name != primary.name and e.origin == "imported":
                sk = self.registry.get_skill(e.name)
                if sk is not None:
                    extras.append(sk)
        return [primary] + extras[:1]


def _ancestors(task: Task, by_id: dict[str, Task]) -> set[str]:
    seen: set[str] = set()
    stack = list(task.depends_on)
    while stack:
        x = stack.pop()
        if x in seen or x not in by_id:
            continue
        seen.add(x)
        stack.extend(by_id[x].depends_on)
    return seen


def _clean_validation_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        msgs = [str(e["msg"]).replace("Value error, ", "") for e in exc.errors()]
        return "; ".join(msgs)[:500]
    return str(exc)[:500]
