"""Telegram command logic, independent of aiogram (so it is fully unit-testable).

Security: every entry point checks the authorized-user list first; skill mutations need an admin;
every outgoing string is redacted for secrets; logs are only shown to the project's owner.
"""
from __future__ import annotations

from typing import Any, Optional

from app.config import Settings
from app.orchestrator.service import Orchestrator
from app.schemas.models import FINAL_PROJECT_STATES, ProjectState
from app.skills.discovery import SkillDiscoveryService
from app.skills.importer import SkillImporter
from app.skills.registry import RegistryError, SkillRegistry
from app.util import redact

HELP = """Commands
/new - start a new project (then send the description)
/status - your projects
/details PROJECT_ID - tasks, skills and findings
/cancel PROJECT_ID - stop a project
/retry PROJECT_ID - retry failed tasks / resend the archive
/logs PROJECT_ID - recent log lines
/help - this message

Skills (authorized users: list/search; admins: the rest)
/skills - installed Skills
/skills_search QUERY - search approved local Skills
/skills_discover QUERY - find candidates on GitHub (never auto-activated)
/skills_review ID - inspect a candidate and its provenance
/skills_import ID [ack] - import a reviewed candidate (admin)
/skills_update NAME - check for a newer version (admin)
/skills_disable NAME - disable a Skill (admin)
/skills_rollback NAME - restore the previous approved version (admin)
/budget PROJECT_ID USD - raise a project's spending limit (admin)

You can also just send a message describing the software you want."""

NOT_AUTHORIZED = "You are not authorized to use this bot."
ADMIN_ONLY = "This command is for administrators only."


class BotService:
    def __init__(self, settings: Settings, orchestrator: Orchestrator, registry: SkillRegistry,
                 discovery: Optional[SkillDiscoveryService] = None, importer: Optional[SkillImporter] = None):
        self.s = settings
        self.orch = orchestrator
        self.registry = registry
        self.discovery = discovery
        self.importer = importer
        self._awaiting_prompt: set[int] = set()

    # ------------------------------------------------------------------ auth
    def is_authorized(self, user_id: Optional[int]) -> bool:
        return user_id is not None and user_id in self.s.authorized_user_ids

    def is_admin(self, user_id: Optional[int]) -> bool:
        return self.is_authorized(user_id) and user_id in self.s.admin_user_ids

    def _out(self, text: str) -> str:
        return redact(text, self.s.secret_values())[:3900]

    # ------------------------------------------------------------------ entry points
    async def handle_command(self, user_id: Optional[int], chat_id: int, command: str, args: str = "") -> str:
        if not self.is_authorized(user_id):
            return NOT_AUTHORIZED
        assert user_id is not None
        cmd = command.lower().lstrip("/").split("@")[0]
        args = args.strip()
        try:
            fn = getattr(self, f"cmd_{cmd}", None)
            if fn is None:
                return "Unknown command. Send /help."
            return self._out(await fn(user_id, chat_id, args))
        except Exception as exc:  # noqa: BLE001 - never leak internals to the chat
            return self._out(f"Something went wrong ({type(exc).__name__}). Check /logs or the server log.")

    async def handle_text(self, user_id: Optional[int], chat_id: int, text: str) -> str:
        if not self.is_authorized(user_id):
            return NOT_AUTHORIZED
        assert user_id is not None
        text = text.strip()
        if len(text) < 15:
            return "Please describe the software in a bit more detail (at least a sentence)."
        if len(text) > 8000:
            return "That description is too long (max 8000 characters)."
        self._awaiting_prompt.discard(user_id)
        project = await self.orch.submit(user_id, chat_id, text)
        return f"Project {project.id} created. Planning... I will show the plan and cost estimate for your approval."

    async def handle_callback(self, user_id: Optional[int], data: str) -> str:
        if not self.is_authorized(user_id):
            return NOT_AUTHORIZED
        assert user_id is not None
        action, _, pid = data.partition(":")
        if action == "ok":
            return await self.orch.confirm(pid, user_id)
        if action == "no":
            return await self.orch.reject(pid, user_id)
        return "Unknown action."

    # ------------------------------------------------------------------ project commands
    async def cmd_start(self, uid, cid, args):
        return "Welcome to the AI Software Factory. Describe a project and I will plan, build, test and deliver it.\n\n" + HELP

    async def cmd_help(self, uid, cid, args):
        return HELP

    async def cmd_new(self, uid, cid, args):
        if args:
            return await self.handle_text(uid, cid, args)
        self._awaiting_prompt.add(uid)
        return "Send the project description in your next message."

    async def cmd_status(self, uid, cid, args):
        projects = await self.orch.storage.list_projects(owner_id=uid, limit=8)
        ok, detail = await self.orch.provider.health()
        head = f"Model server: {'ready' if ok else 'NOT READY - ' + detail}"
        if not projects:
            return head + "\nNo projects yet. Send a description to start one."
        lines = [head, ""]
        for p in projects:
            name = p.plan.project_name if p.plan else "(planning)"
            lines.append(f"{p.id} - {name}: {p.state.value}" + (" (archive delivered)" if p.delivered else ""))
        lines.append("\nUse /details PROJECT_ID for more.")
        return "\n".join(lines)

    async def _owned(self, uid: int, pid: str):
        if not pid:
            return None
        p = await self.orch.storage.get_project(pid.strip())
        return p if p is not None and p.owner_id == uid else None

    async def cmd_details(self, uid, cid, args):
        p = await self._owned(uid, args)
        if p is None:
            return "Usage: /details PROJECT_ID (a project you own)."
        tasks = await self.orch.storage.get_tasks(p.id)
        lines = [await self.orch.status_text(p.id, uid), "", "Tasks:"]
        for t in tasks:
            lines.append(f"- {t.id} [{t.agent}/{t.skill} v{t.skill_version or '?'}] {t.state.value}, retries {t.retries}"
                         + (f" - {t.error[:80]}" if t.error and t.state != "COMPLETED" else ""))
        if p.skills_used:
            lines += ["", "Skills:"] + [f"- {u.role}: {u.skill} v{u.version} ({u.origin.replace('_', ' ')})"
                                       + (f" {u.source_url}@{u.commit[:8]} license {u.license}" if u.source_url else "")
                                       for u in p.skills_used]
        if p.findings:
            lines += ["", "Review findings: " + ", ".join(f"{sev}={sum(1 for f in p.findings if f.severity == sev)}"
                                                         for sev in ("high", "medium", "low", "info"))]
        if p.state in FINAL_PROJECT_STATES:
            lines.append(f"\nFinal: {p.final_status or p.state.value}; delivered: {p.delivered}"
                         + (f" ({p.delivery_error})" if p.delivery_error else ""))
        return "\n".join(lines)

    async def cmd_cancel(self, uid, cid, args):
        if not args:
            return "Usage: /cancel PROJECT_ID"
        return await self.orch.cancel(args.strip(), uid)

    async def cmd_retry(self, uid, cid, args):
        if not args:
            return "Usage: /retry PROJECT_ID"
        return await self.orch.retry(args.strip(), uid)

    async def cmd_logs(self, uid, cid, args):
        p = await self._owned(uid, args)
        if p is None:
            return "Usage: /logs PROJECT_ID (a project you own)."
        events = await self.orch.storage.get_events(p.id, limit=25)
        if not events:
            return "No log lines yet."
        import time as _t
        return "\n".join(f"{_t.strftime('%H:%M:%S', _t.gmtime(e['ts']))} {e['level']}: {e['message'][:200]}" for e in events)

    async def cmd_budget(self, uid, cid, args):
        if not self.is_admin(uid):
            return ADMIN_ONLY
        parts = args.split()
        if len(parts) != 2:
            return "Usage: /budget PROJECT_ID USD"
        try:
            usd = float(parts[1])
        except ValueError:
            return "USD must be a number."
        return await self.orch.set_budget(parts[0], usd)

    # ------------------------------------------------------------------ skills commands
    async def cmd_skills(self, uid, cid, args):
        entries = self.registry.list_skills(include_disabled=True)
        if not entries:
            return "No Skills installed."
        return "Installed Skills:\n" + "\n".join(
            f"- {e.label()} roles={','.join(e.roles)}" + (f" score={e.score:.0f}" if e.origin == "imported" else "") for e in entries)

    async def cmd_skills_search(self, uid, cid, args):
        if not args:
            return "Usage: /skills_search QUERY"
        hits = self.registry.search(args)
        return ("Approved local Skills:\n" + "\n".join(f"- {e.label()} - {e.description[:90]}" for e in hits)) if hits \
            else "No approved local Skill matches. Try /skills_discover."

    async def cmd_skills_discover(self, uid, cid, args):
        if not self.is_admin(uid):
            return ADMIN_ONLY
        if not args:
            return "Usage: /skills_discover QUERY (e.g. 'react frontend')"
        if self.discovery is None:
            return "Skill discovery is not configured."
        res = await self.discovery.discover(args)
        lines = [f"Searched {res.searched_repos} repositories."]
        for c in res.candidates[:8]:
            ev = c["evaluation"]
            lines.append(f"- {c['id']} {c['name']} ({c['repo']}) " + ("REJECTED: " + ev["reject_reasons"][0][:70] if ev["rejected"]
                                                                       else f"score {ev['total']:.0f}, license {c['license']}"))
        lines += [f"note: {n}" for n in res.notes]
        lines.append("Nothing was activated. Review with /skills_review ID.")
        return "\n".join(lines)

    async def cmd_skills_review(self, uid, cid, args):
        if not self.is_admin(uid):
            return ADMIN_ONLY
        if self.discovery is None or not args:
            return "Usage: /skills_review ID"
        return await self.discovery.review_report(args.strip())

    async def cmd_skills_import(self, uid, cid, args):
        if not self.is_admin(uid):
            return ADMIN_ONLY
        if self.importer is None:
            return "Skill import is not configured."
        parts = args.split()
        if not parts:
            return "Usage: /skills_import ID [ack]"
        res = await self.importer.import_candidate(parts[0], approved_by=uid, acknowledge_review=len(parts) > 1 and parts[1] == "ack")
        return res.text()

    async def cmd_skills_update(self, uid, cid, args):
        if not self.is_admin(uid):
            return ADMIN_ONLY
        if self.importer is None or not args:
            return "Usage: /skills_update NAME"
        r = await self.importer.check_update(args.strip())
        return r["message"]

    async def cmd_skills_disable(self, uid, cid, args):
        if not self.is_admin(uid):
            return ADMIN_ONLY
        try:
            self.registry.disable(args.strip())
        except RegistryError as exc:
            return str(exc)
        return f"Skill {args.strip()} disabled."

    async def cmd_skills_rollback(self, uid, cid, args):
        if not self.is_admin(uid):
            return ADMIN_ONLY
        try:
            e = self.registry.rollback(args.strip())
        except RegistryError as exc:
            return str(exc)
        return f"Rolled back to {e.label()}."
