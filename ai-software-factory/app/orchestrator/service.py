"""Orchestrator: owns the whole project lifecycle and runs it in background asyncio tasks, so the
Telegram bot stays responsive. Generated code only ever runs inside the Sandbox, never here."""
from __future__ import annotations

import ast
import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional

from app.agents.base import Agent, AgentOutputError, TaskContext
from app.agents.reviewer import ReviewerAgent, owner_of_file, static_review
from app.bot.messenger import Messenger
from app.config import Settings
from app.execution.packaging import PackageResult, build_zip, split_file, validate_zip
from app.execution.qa import QARunner, responsible_tasks
from app.execution.sandbox import Sandbox
from app.execution.workspace import WorkspaceManager
from app.orchestrator.budget import BudgetTracker
from app.orchestrator.progress import ProgressReporter, render_status
from app.orchestrator.project_manager import PlanningError, ProjectManager
from app.orchestrator.reporting import (
    DISCLAIMER, ensure_baseline_files, final_status, render_build_summary, render_skills_used, render_test_report,
)
from app.orchestrator.scheduler import TaskScheduler, reset_interrupted
from app.providers.base import ModelProvider, ProviderError
from app.schemas.models import (
    ACTIVE_PROJECT_STATES, FINAL_PROJECT_STATES, Finding, Project, ProjectState, Task, TaskState,
)
from app.util import new_project_id, redact, truncate

log = logging.getLogger("factory.orchestrator")


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        storage: Any,
        provider: ModelProvider,
        pm: ProjectManager,
        workspace: WorkspaceManager,
        sandbox: Sandbox,
        messenger: Messenger,
        budget: BudgetTracker,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ):
        self.s = settings
        self.storage = storage
        self.provider = provider
        self.pm = pm
        self.workspace = workspace
        self.sandbox = sandbox
        self.messenger = messenger
        self.budget = budget
        self.clock = clock
        self.sleep = sleep
        self.qa = QARunner(sandbox)
        self.progress = ProgressReporter(messenger, storage, settings, budget)
        self._runs: dict[str, asyncio.Task] = {}
        self._schedulers: dict[str, TaskScheduler] = {}
        self._cancelled: set[str] = set()
        self.s.artifacts_dir.mkdir(parents=True, exist_ok=True)

    # ================================================================ lifecycle API
    async def submit(self, owner_id: int, chat_id: int, prompt: str) -> Project:
        project = Project(id=new_project_id(), owner_id=owner_id, chat_id=chat_id, prompt=prompt.strip(),
                          state=ProjectState.PLANNING, budget_usd=self.s.project_budget_usd)
        await self.storage.save_project(project)
        await self._log(project.id, "info", "project created; planning started")
        self._spawn(project.id, self._plan_and_ask(project.id))
        return project

    async def confirm(self, project_id: str, user_id: int) -> str:
        project = await self.storage.get_project(project_id)
        if project is None or project.owner_id != user_id:
            return "Project not found."
        if project.state != ProjectState.AWAITING_CONFIRMATION:
            return f"Project {project_id} is {project.state.value}; nothing to confirm."
        project.state = ProjectState.RUNNING
        await self.storage.save_project(project)
        self._spawn(project_id, self._run(project_id))
        return f"Started project {project_id}. Use /status to follow it."

    async def reject(self, project_id: str, user_id: int) -> str:
        project = await self.storage.get_project(project_id)
        if project is None or project.owner_id != user_id:
            return "Project not found."
        if project.state != ProjectState.AWAITING_CONFIRMATION:
            return f"Project {project_id} is {project.state.value}."
        project.state = ProjectState.CANCELLED
        project.finished_at = self.clock()
        await self.storage.save_project(project)
        return f"Project {project_id} cancelled before any work started."

    async def cancel(self, project_id: str, user_id: int) -> str:
        project = await self.storage.get_project(project_id)
        if project is None or project.owner_id != user_id:
            return "Project not found."
        if project.state in FINAL_PROJECT_STATES:
            return f"Project {project_id} is already {project.state.value}."
        self._cancelled.add(project_id)
        sched = self._schedulers.get(project_id)
        if sched is not None:
            sched.cancel()
        run = self._runs.get(project_id)
        if run is None or sched is None:
            if run is not None:
                run.cancel()
            project.state = ProjectState.CANCELLED
            project.finished_at = self.clock()
            await self.storage.save_project(project)
        return f"Cancelling project {project_id}."

    async def retry(self, project_id: str, user_id: int) -> str:
        project = await self.storage.get_project(project_id)
        if project is None or project.owner_id != user_id:
            return "Project not found."
        if project_id in self._runs and not self._runs[project_id].done():
            return "That project is still running."
        if project.state not in FINAL_PROJECT_STATES:
            return f"Project {project_id} is {project.state.value}; only finished projects can be retried."
        if project.plan is None:
            return "This project never produced a plan. Send the request again with /new."
        # delivery-only retry
        if project.artifact_path and not project.delivered and project.state != ProjectState.CANCELLED \
                and project.state != ProjectState.FAILED:
            self._spawn(project_id, self._deliver_only(project_id))
            return f"Re-sending the archive of project {project_id}."
        spent = self.budget.spent(project)
        if spent.total >= project.budget_usd:
            return (f"Project budget reached (${spent.total:.2f} of ${project.budget_usd:.2f}). "
                    f"An administrator can raise it with /budget {project_id} USD.")
        tasks = await self.storage.get_tasks(project_id)
        for t in tasks:
            if t.state in (TaskState.FAILED, TaskState.BLOCKED, TaskState.CANCELLED):
                t.state, t.retries, t.error, t.started_at, t.ended_at = TaskState.PENDING, 0, None, None, None
                await self.storage.save_task(project_id, t)
        project.state, project.error, project.finished_at, project.repair_cycles = ProjectState.RUNNING, None, None, 0
        project.final_status = None
        self._cancelled.discard(project_id)
        await self.storage.save_project(project)
        self._spawn(project_id, self._run(project_id))
        return f"Retrying project {project_id}: failed tasks were re-queued."

    async def set_budget(self, project_id: str, usd: float) -> str:
        project = await self.storage.get_project(project_id)
        if project is None:
            return "Project not found."
        if usd <= 0 or usd > 1000:
            return "Budget must be between 0 and 1000 USD."
        project.budget_usd = usd
        await self.storage.save_project(project)
        return f"Budget of {project_id} set to ${usd:.2f}."

    async def recover(self) -> int:
        """After a restart: resume interrupted projects from their persisted state."""
        n = 0
        for project in await self.storage.list_projects(limit=500):
            if project.state == ProjectState.PLANNING:
                self._spawn(project.id, self._plan_and_ask(project.id))
                n += 1
            elif project.state in ACTIVE_PROJECT_STATES:
                tasks = await self.storage.get_tasks(project.id)
                for t in reset_interrupted(tasks):
                    await self.storage.save_task(project.id, t)
                await self._log(project.id, "warning", "recovered after restart; resuming")
                if project.state == ProjectState.DELIVERING and project.artifact_path:
                    self._spawn(project.id, self._deliver_only(project.id))
                else:
                    project.state = ProjectState.RUNNING
                    await self.storage.save_project(project)
                    self._spawn(project.id, self._run(project.id))
                n += 1
        return n

    async def shutdown(self) -> None:
        """Graceful stop: running work is cancelled but its state stays persisted for recovery."""
        for pid, sched in list(self._schedulers.items()):
            sched.cancel()
        runs = [r for r in self._runs.values() if not r.done()]
        for r in runs:
            r.cancel()
        await asyncio.gather(*runs, return_exceptions=True)

    async def status_text(self, project_id: str, user_id: int) -> str:
        project = await self.storage.get_project(project_id)
        if project is None or project.owner_id != user_id:
            return "Project not found."
        tasks = await self.storage.get_tasks(project_id)
        return render_status(project, tasks, self.s, self.budget)

    # ================================================================ internals
    def _spawn(self, project_id: str, coro) -> None:
        task = asyncio.create_task(coro, name=f"project-{project_id}")
        self._runs[project_id] = task

        def _done(t: asyncio.Task) -> None:
            if self._runs.get(project_id) is t:
                self._runs.pop(project_id, None)
            if not t.cancelled() and t.exception() is not None:
                log.error("project %s crashed: %r", project_id, t.exception())

        task.add_done_callback(_done)

    async def _log(self, project_id: str, level: str, msg: str) -> None:
        try:
            await self.storage.add_event(project_id, level, redact(msg, self.s.secret_values()))
        except Exception:  # noqa: BLE001
            pass

    async def wait(self, project_id: str, timeout: Optional[float] = None) -> None:
        """Test helper: wait for a project's background run to finish."""
        run = self._runs.get(project_id)
        if run is not None:
            await asyncio.wait_for(asyncio.shield(run), timeout)

    # ---------------------------------------------------------------- planning
    async def _plan_and_ask(self, project_id: str) -> None:
        project = await self.storage.get_project(project_id)
        if project is None:
            return
        try:
            plan = await self.pm.plan(project)
        except (PlanningError, ProviderError) as exc:
            project.state, project.error = ProjectState.FAILED, f"planning failed: {exc}"
            project.finished_at = self.clock()
            await self.storage.save_project(project)
            await self._log(project_id, "error", project.error)
            await self.messenger.send_text(project.chat_id, f"Project {project_id}: {project.error}")
            return
        project.plan = plan
        est = self.budget.estimate_before_run(project)
        project.estimated_cost_usd = est.total
        project.state = ProjectState.AWAITING_CONFIRMATION
        await self.storage.save_tasks(project_id, plan.tasks)
        await self.storage.save_project(project)
        await self.messenger.send_text(project.chat_id, self.plan_text(project, est.infra_usd, est.tokens_usd),
                                       buttons=[("Start", f"ok:{project_id}"), ("Cancel", f"no:{project_id}")])

    def plan_text(self, project: Project, infra: float, tok: float) -> str:
        plan = project.plan
        assert plan is not None
        lines = [f"Plan for {project.id}: {plan.project_name}", plan.summary, "", "Tasks (agent / skill):"]
        for t in plan.tasks:
            dep = f" after {','.join(t.depends_on)}" if t.depends_on else ""
            lines.append(f"- {t.id}: {t.title or t.description[:50]} [{t.agent} / {t.skill} v{t.skill_version}]{dep}")
        lines += ["", "Skills: " + "; ".join(f"{u.role}: {u.skill} ({u.origin.replace('_', ' ')})" for u in project.skills_used)]
        ra = len(plan.tasks)
        lines += [f"Workload: {ra} tasks, up to {self.s.max_active_agents} in parallel, up to {self.s.max_repair_cycles} repair cycles.",
                  f"Estimated cost: ~${infra + tok:.2f} (Pod time ${infra:.2f} at ${self.s.gpu_hourly_usd:.2f}/h + tokens ${tok:.2f}); "
                  f"limit ${project.budget_usd:.2f}. This is an ESTIMATE, not a bill.",
                  "Start the build?"]
        return "\n".join(lines)

    # ---------------------------------------------------------------- main run
    async def _run(self, project_id: str) -> None:
        project = await self.storage.get_project(project_id)
        if project is None or project.plan is None:
            return
        plan = project.plan
        tasks = await self.storage.get_tasks(project_id)
        self.workspace.ensure(project_id)
        project.started_at = self.clock() - float(project.extra.get("active_s", 0.0))
        project.finished_at = None
        project.state = ProjectState.RUNNING
        await self.storage.save_project(project)
        notes: list[str] = []

        async def persist(task: Task) -> None:
            await self.storage.save_task(project_id, task)
            project.extra["active_s"] = self.clock() - (project.started_at or self.clock())
            await self.storage.save_project(project)
            await self.progress.update(project, tasks)

        async def on_event(ev: str, task: Task) -> None:
            await self._log(project_id, "error" if ev == "failed" else "info", f"task {task.id} {ev}"
                            + (f": {task.error}" if task.error and ev != "completed" else ""))

        async def executor(task: Task) -> None:
            await self._execute_task(project, plan.architecture, tasks, task)

        sched = TaskScheduler(
            tasks, executor, persist, max_active=self.s.max_active_agents, max_retries=self.s.max_retries,
            max_task_seconds=self.s.max_task_seconds,
            max_project_seconds=max(1.0, self.s.max_project_seconds - float(project.extra.get("active_s", 0.0))),
            backoff_base_s=self.s.backoff_base_s, backoff_cap_s=self.s.backoff_cap_s,
            can_start=lambda: self.budget.can_schedule(project), on_event=on_event,
            verifier=self._verify_task_factory(project_id), clock=self.clock)
        self._schedulers[project_id] = sched
        try:
            await sched.run()
        except asyncio.CancelledError:
            await sched._shutdown_running(TaskState.CANCELLED)
            project.state = ProjectState.CANCELLED if project_id in self._cancelled else project.state
            await self.storage.save_project(project)
            raise
        finally:
            self._schedulers.pop(project_id, None)
        if project_id in self._cancelled:
            self._cancelled.discard(project_id)
            project.state, project.finished_at = ProjectState.CANCELLED, self.clock()
            await self.storage.save_project(project)
            await self.progress.update(project, tasks, force=True)
            await self.messenger.send_text(project.chat_id, f"Project {project_id} was cancelled.")
            return
        if sched.timed_out:
            notes.append("project time limit reached")
        if any(t.state == TaskState.BLOCKED and "budget" in (t.error or "") for t in tasks):
            notes.append("project budget reached: remaining tasks were not scheduled")
        await self._post_process(project, tasks, notes)

    # ---------------------------------------------------------------- task execution
    def _verify_task_factory(self, project_id: str):
        async def verify(task: Task) -> None:
            tdir = self.workspace.task_dir(project_id, task.id)
            for rel in task.output_files:
                if rel.endswith(".py"):
                    try:
                        ast.parse((tdir / rel).read_text(encoding="utf-8"))
                    except SyntaxError as exc:
                        raise AgentOutputError(f"{rel} has a Python syntax error at line {exc.lineno}: {exc.msg}") from None
            if not task.output_files:
                raise AgentOutputError("the agent produced no files")
        return verify

    def _deps_files(self, project_id: str, task: Task, tasks: list[Task]) -> dict[str, str]:
        by_id = {t.id: t for t in tasks}
        seen: list[str] = []
        stack = list(task.depends_on)
        while stack:
            d = stack.pop(0)
            if d in seen or d not in by_id:
                continue
            seen.append(d)
            stack.extend(by_id[d].depends_on)
        out: dict[str, str] = {}
        for d in seen:
            if by_id[d].state == TaskState.COMPLETED:
                out.update(self.workspace.read_task_files(project_id, d, max_bytes=60_000))
        return out

    async def _record_usage(self, project: Project, pt: int, ct: int) -> None:
        cost = self.budget.token_cost(pt, ct)
        project.spent_tokens_usd += cost
        await self.storage.add_usage(project.id, pt, ct, cost)

    async def _execute_task(self, project: Project, architecture: str, tasks: list[Task], task: Task) -> None:
        skills = self.pm.skills_for_task(task)
        agent = Agent(task.agent, self.provider, skills, max_output_tokens=self.s.max_output_tokens)
        feedback = task.error if task.retries else None
        ctx = TaskContext(project_prompt=project.prompt, architecture=architecture, task=task,
                          dependency_files=self._deps_files(project.id, task, tasks), feedback=feedback)
        res = await agent.run(ctx)
        await self._record_usage(project, res.prompt_tokens, res.completion_tokens)
        task.tokens_in += res.prompt_tokens
        task.tokens_out += res.completion_tokens
        task.output_files = self.workspace.write_task_files(project.id, task, res.files)

    async def _repair(self, project: Project, task: Task, feedback: str) -> bool:
        try:
            skills = self.pm.skills_for_task(task)
            agent = Agent(task.agent, self.provider, skills, max_output_tokens=self.s.max_output_tokens)
            existing = self.workspace.read_task_files(project.id, task.id, max_bytes=120_000)
            ctx = TaskContext(project_prompt=project.prompt, architecture=project.plan.architecture if project.plan else "",
                              task=task, feedback=feedback, repair=True, existing_files=existing)
            res = await agent.run(ctx)
            await self._record_usage(project, res.prompt_tokens, res.completion_tokens)
            task.output_files = self.workspace.apply_repair(project.id, task, res.files)
            task.repair_rounds += 1
            await self.storage.save_task(project.id, task)
            return True
        except (ProviderError, AgentOutputError, Exception) as exc:  # noqa: BLE001
            await self._log(project.id, "error", f"repair of {task.id} failed: {exc}")
            return False

    # ---------------------------------------------------------------- QA / review / packaging
    def _integrate(self, project: Project, tasks: list[Task]):
        done = [t for t in tasks if t.state == TaskState.COMPLETED]
        return self.workspace.integrate(project.id, done)

    async def _set_state(self, project: Project, tasks: list[Task], state: ProjectState) -> None:
        project.state = state
        project.extra["active_s"] = self.clock() - (project.started_at or self.clock())
        await self.storage.save_project(project)
        await self.progress.update(project, tasks, force=True)

    def _fallback_task(self, tasks: list[Task]) -> Optional[str]:
        for role in ("backend", "frontend", "database", "api_integrator"):
            for t in tasks:
                if t.agent == role and t.state == TaskState.COMPLETED:
                    return t.id
        return None

    async def _post_process(self, project: Project, tasks: list[Task], notes: list[str]) -> None:
        pid = project.id
        try:
            if not any(t.state == TaskState.COMPLETED for t in tasks):
                await self._finish(project, tasks, ProjectState.FAILED, None, [], notes + ["no task completed"])
                return
            await self._set_state(project, tasks, ProjectState.INTEGRATING)
            integ = self._integrate(project, tasks)
            for c in integ.unresolved:
                notes.append(f"unresolved integration conflict on {c.path} between {', '.join(c.tasks)}")
            integrated = self.workspace.integrated_dir(pid)
            report = None
            for cycle in range(self.s.max_repair_cycles + 1):
                if project.id in self._cancelled:
                    break
                await self._set_state(project, tasks, ProjectState.TESTING)
                report = await self.qa.run(integrated)
                project.test_report = report
                await self.storage.save_project(project)
                if report.passed or cycle == self.s.max_repair_cycles or not self.budget.can_schedule(project):
                    break
                resp = responsible_tasks(report, integ.file_owner, self._fallback_task(tasks))
                if not resp:
                    break
                await self._set_state(project, tasks, ProjectState.REPAIRING)
                by_id = {t.id: t for t in tasks}
                # A failing assertion is reported in the TEST file, but the bug is usually in the code under test:
                # route test-file failures to the implementers the tests depend on as well.
                for tid in list(resp):
                    if tid in by_id and by_id[tid].agent == "tester":
                        for dep in by_id[tid].depends_on:
                            if dep in by_id and by_id[dep].agent in ("backend", "frontend", "database", "api_integrator"):
                                resp.setdefault(dep, []).append("code under test (failure reported in a test file)")
                fb_text = "\n\n".join(f"[{c.name}] exit {c.exit_code}\n{c.output}" for c in report.failed_checks)
                repaired = False
                for tid, ev in resp.items():
                    if tid in by_id and by_id[tid].state == TaskState.COMPLETED:
                        repaired |= await self._repair(project, by_id[tid], fb_text + "\nEVIDENCE: " + "; ".join(ev))
                project.repair_cycles += 1
                integ = self._integrate(project, tasks)
                if not repaired:
                    break
            # ---- independent review
            await self._set_state(project, tasks, ProjectState.REVIEWING)
            files = self.workspace.read_dir_files(integrated, max_bytes=400_000)
            findings = static_review(files, project.plan, report)
            summary = ""
            try:
                rev_skill = self.pm.registry.get_skill(self.pm._best_entry("reviewer").name)  # type: ignore[union-attr]
                reviewer = ReviewerAgent("reviewer", self.provider, [rev_skill], max_output_tokens=self.s.max_output_tokens)
                qa_text = "\n".join(f"{c.name}: {'skipped' if c.skipped else 'passed' if c.passed else 'FAILED'}" for c in (report.checks if report else []))
                summary, model_findings, pt, ct = await reviewer.review(project.prompt, files, qa_text)
                await self._record_usage(project, pt, ct)
                findings += model_findings
            except Exception as exc:  # noqa: BLE001
                notes.append(f"independent review could not run: {exc}")
            high = [f for f in findings if f.severity == "high"]
            if high and project.repair_cycles < self.s.max_repair_cycles and self.budget.can_schedule(project):
                await self._set_state(project, tasks, ProjectState.REPAIRING)
                by_id = {t.id: t for t in tasks}
                targets: dict[str, list[Finding]] = {}
                for f in high:
                    tid = owner_of_file(f.file, integ.file_owner) or self._fallback_task(tasks)
                    if tid and tid in by_id and by_id[tid].state == TaskState.COMPLETED:
                        targets.setdefault(tid, []).append(f)
                for tid, fs in targets.items():
                    await self._repair(project, by_id[tid], "REVIEW FINDINGS:\n" + "\n".join(
                        f"- [{f.severity}] {f.file}: {f.explanation} Fix: {f.suggested_fix}" for f in fs))
                project.repair_cycles += 1
                integ = self._integrate(project, tasks)
                await self._set_state(project, tasks, ProjectState.TESTING)
                report = await self.qa.run(integrated)
                project.test_report = report
                files = self.workspace.read_dir_files(integrated, max_bytes=400_000)
                still = {(f.file, f.explanation) for f in static_review(files, project.plan, report) if f.severity == "high"}
                # model findings are re-evaluated only by static checks + QA; unresolved model findings stay listed
                findings = [f for f in findings if f.source == "model" and f.severity == "high"] + \
                           static_review(files, project.plan, report) + [f for f in findings if f.source == "model" and f.severity != "high"]
            project.findings = findings
            project.summary = summary
            if project.id in self._cancelled:
                self._cancelled.discard(project.id)
                project.state, project.finished_at = ProjectState.CANCELLED, self.clock()
                await self.storage.save_project(project)
                await self.messenger.send_text(project.chat_id, f"Project {pid} was cancelled.")
                return
            # ---- docs + packaging
            files = self.workspace.read_dir_files(integrated, max_bytes=2_000_000)
            notes += ensure_baseline_files(integrated, project, files)
            status = final_status(tasks, report, findings, has_files=bool(files))
            caps = self.sandbox.capabilities()
            if not caps.get("network_isolated"):
                notes.append("sandbox had NO network isolation on this host: tests ran with scrubbed env + rlimits only")
            (integrated / "test-report.md").write_text(render_test_report(project, report, caps, findings), encoding="utf-8")
            su = render_skills_used(project)
            if su:
                (integrated / "skills-used.md").write_text(su, encoding="utf-8")
            (integrated / "BUILD_SUMMARY.md").write_text(render_build_summary(project, tasks, status.value, notes), encoding="utf-8")
            await self._finish(project, tasks, status, report, findings, notes)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("post-processing failed for %s", pid)
            project.error = f"internal error: {type(exc).__name__}: {exc}"
            project.state, project.finished_at = ProjectState.FAILED, self.clock()
            await self.storage.save_project(project)
            await self.messenger.send_text(project.chat_id, f"Project {pid} failed: {project.error}")

    async def _finish(self, project: Project, tasks: list[Task], status: ProjectState, report, findings, notes: list[str]) -> None:
        pid = project.id
        package: Optional[PackageResult] = None
        integrated = self.workspace.integrated_dir(pid)
        if status != ProjectState.FAILED or any(integrated.rglob("*")):
            await self._set_state(project, tasks, ProjectState.PACKAGING)
            name = project.plan.project_name if project.plan else pid
            out = self.s.artifacts_dir / pid / f"{name}.zip"
            package = build_zip(integrated, out, name)
            problems = validate_zip(out)
            if problems:
                notes.append("archive validation problems: " + "; ".join(problems))
                if status == ProjectState.SUCCESS:
                    status = ProjectState.PARTIAL_SUCCESS
            if package.excluded:
                notes.append("excluded from archive: " + ", ".join(f"{r} ({w})" for r, w in package.excluded[:10]))
            project.artifact_path = str(out)
            project.extra["archive_problems"] = problems
        project.final_status = status.value
        project.notes = notes
        project.extra["notes"] = notes
        project.extra["parts"] = []
        project.state = ProjectState.DELIVERING if package else status
        if package is None:
            project.finished_at = self.clock()
        await self.storage.save_project(project)
        if package is not None:
            await self._deliver(project, status, notes)
        else:
            await self.messenger.send_text(project.chat_id, self.final_text(project, tasks, status, notes, delivered=False))
        await self.progress.update(project, tasks, force=True)

    def final_text(self, project: Project, tasks: list[Task], status: ProjectState, notes: list[str], delivered: bool) -> str:
        done = sum(1 for t in tasks if t.state == TaskState.COMPLETED)
        high = sum(1 for f in project.findings if f.severity == "high")
        lines = [f"Project {project.id} finished: {status.value}",
                 f"Tasks completed: {done}/{len(tasks)}; repair cycles: {project.repair_cycles}; open high-severity findings: {high}"]
        rep = project.test_report
        if rep is not None:
            lines.append("QA: " + ", ".join(f"{c.name}={'skip' if c.skipped else 'ok' if c.passed else 'FAIL'}" for c in rep.checks))
        lines += [f"- {n}" for n in notes[:6]]
        lines.append("Archive delivered." if delivered else ("Archive NOT delivered: " + (project.delivery_error or "no archive")))
        lines.append(DISCLAIMER)
        return "\n".join(lines)

    # ---------------------------------------------------------------- delivery
    async def _send_with_retry(self, chat_id: int, path: Path, caption: str, attempts: int = 3) -> Optional[int]:
        last: Optional[Exception] = None
        for i in range(attempts):
            try:
                mid = await self.messenger.send_document(chat_id, path, caption)
                if mid:
                    return mid
                last = RuntimeError("Telegram did not confirm the upload")
            except Exception as exc:  # noqa: BLE001
                last = exc
            await self.sleep(min(2 ** i, 8) * 0.5)
        raise RuntimeError(f"{type(last).__name__}: {last}" if last else "delivery failed")

    async def _deliver(self, project: Project, status: ProjectState, notes: list[str]) -> None:
        tasks = await self.storage.get_tasks(project.id)
        path = Path(project.artifact_path or "")
        limit = self.s.telegram_max_file_mb * 1024 * 1024
        try:
            parts = split_file(path, limit)
            project.extra["parts"] = [p.name for p in parts]
            for i, part in enumerate(parts, 1):
                cap = f"{project.id} {status.value}" + (f" - part {i}/{len(parts)}" if len(parts) > 1 else "")
                await self._send_with_retry(project.chat_id, part, cap)
            if len(parts) > 1:
                await self.messenger.send_text(project.chat_id, f"The archive was split into {len(parts)} parts. Join them with: "
                                               f"cat {path.name}.0* > {path.name}")
            project.delivered, project.delivery_error = True, None
        except Exception as exc:  # noqa: BLE001
            project.delivered, project.delivery_error = False, redact(str(exc), self.s.secret_values())[:300]
            await self._log(project.id, "error", f"delivery failed: {project.delivery_error}")
        project.state = status
        project.finished_at = self.clock()
        await self.storage.save_project(project)
        try:
            await self.messenger.send_text(project.chat_id, self.final_text(project, tasks, status, notes, project.delivered))
        except Exception:  # noqa: BLE001
            pass

    async def _deliver_only(self, project_id: str) -> None:
        project = await self.storage.get_project(project_id)
        if project is None or not project.artifact_path:
            return
        status = ProjectState(project.final_status) if project.final_status else ProjectState.PARTIAL_SUCCESS
        await self._deliver(project, status, list(project.extra.get("notes", [])))
