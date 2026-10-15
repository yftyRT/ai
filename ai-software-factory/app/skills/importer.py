"""SkillImporter: the safe 11-step import workflow.

 1 search (discovery)     2 retrieve content     3 verify source + pin     4 license + attribution
 5 format/size            6 malicious scan       7 evaluate                8 compare with installed
 9 stage                  10 validation tests    11 approve/activate per trust policy

Nothing from a Skill repository is ever executed and no dependency is ever installed here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from app.storage.base import Storage

from .discovery import CANDIDATES, SkillDiscoveryService, candidate_id
from .evaluator import SkillEvaluator
from .github_client import GitHubClient, GitHubError, RateLimited
from .loader import SkillFormatError, SkillLoader
from .provenance import attribution_notice, build_version_record, sha256_text
from .registry import RegistryError, SkillEntry, SkillRegistry
from .validator import check_license, validate_skill_content

_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")


@dataclass
class ImportResult:
    ok: bool
    status: str  # activated | needs_admin_ack | needs_approval | rejected | failed | no_change
    steps: list[tuple[str, bool, str]] = field(default_factory=list)
    entry: Optional[SkillEntry] = None
    reasons: list[str] = field(default_factory=list)

    def step(self, name: str, ok: bool, detail: str = "") -> bool:
        self.steps.append((name, ok, detail))
        return ok

    def text(self) -> str:
        head = {
            "activated": "Imported and activated.",
            "needs_admin_ack": "NOT imported: this Skill needs explicit administrator acknowledgement (see review).",
            "needs_approval": "NOT imported: approval required.",
            "rejected": "REJECTED.",
            "failed": "FAILED.",
            "no_change": "No change.",
        }[self.status]
        lines = [head]
        for n, ok, d in self.steps:
            lines.append(f"  {'ok ' if ok else 'FAIL'} {n}{': ' + d if d else ''}")
        lines += [f"  - {r}" for r in self.reasons]
        return "\n".join(lines)


class SkillImporter:
    def __init__(
        self,
        registry: SkillRegistry,
        github: GitHubClient,
        storage: Storage,
        evaluator: SkillEvaluator,
        discovery: SkillDiscoveryService,
        *,
        trust_policy: str = "manual",
        auto_min_score: float = 80.0,
        min_score: float = 55.0,
        max_bytes: int = 65536,
        allow_copyleft: bool = False,
    ):
        self.registry = registry
        self.github = github
        self.storage = storage
        self.evaluator = evaluator
        self.discovery = discovery
        self.trust_policy = trust_policy
        self.auto_min_score = auto_min_score
        self.min_score = min_score
        self.max_bytes = max_bytes
        self.allow_copyleft = allow_copyleft

    # --------------------------------------------------------------- policy
    def auto_policy_allows(self, cand: dict[str, Any]) -> tuple[bool, list[str]]:
        """Strict, configurable policy for the OPTIONAL automatic mode. Never grants extra privileges."""
        ev = cand["evaluation"]
        why: list[str] = []
        if self.trust_policy != "auto_strict":
            why.append("trust policy is 'manual'")
        if ev["rejected"]:
            why.append("candidate is rejected")
        if ev["total"] < self.auto_min_score:
            why.append(f"score {ev['total']} < auto threshold {self.auto_min_score}")
        if not ev["license"].get("auto_ok"):
            why.append("license is not on the automatic-approval list")
        if ev["requires_admin_review"]:
            why.append("requires administrator review")
        if ev["validation"].get("warnings") or ev["validation"].get("review_reasons"):
            why.append("validation produced warnings")
        if cand["repo_meta"].get("archived"):
            why.append("repository is archived")
        if not _SHA_RE.match(cand.get("commit_sha", "")):
            why.append("version is not pinned to a commit")
        if cand.get("kind") == "update":
            why.append("updates always require administrator approval")
        return (not why), why

    # ---------------------------------------------------------------- import
    async def import_candidate(
        self,
        cid: str,
        *,
        approved_by: Optional[int] = None,
        acknowledge_review: bool = False,
        auto: bool = False,
    ) -> ImportResult:
        res = ImportResult(ok=False, status="failed")
        cand = await self.storage.get_doc(CANDIDATES, cid)
        if cand is None:
            res.reasons.append(f"unknown candidate {cid!r}; run /skills_discover first")
            return res
        res.step("1 candidate loaded", True, f"{cand['repo']}:{cand['path']}")

        # Approval gate (step 11 decided early so nothing is staged without authority)
        if auto:
            allowed, why = self.auto_policy_allows(cand)
            if not allowed:
                res.status = "needs_approval"
                res.reasons += ["automatic activation not permitted: " + "; ".join(why)]
                return res
        elif approved_by is None:
            res.status = "needs_approval"
            res.reasons.append("administrator approval required")
            return res

        # 2 + 3: retrieve at the pinned commit and verify
        sha = cand.get("commit_sha", "")
        if not _SHA_RE.match(sha):
            res.step("3 verify source and pin", False, "candidate is not pinned to an immutable commit")
            res.status = "rejected"
            return res
        try:
            text = await self.github.get_file(cand["repo"], cand["path"], sha)
        except RateLimited as exc:
            res.step("2 retrieve content", False, f"rate limited, retry in {int(exc.retry_after_s)}s")
            return res
        except GitHubError as exc:
            res.step("2 retrieve content", False, str(exc))
            return res
        res.step("2 retrieve content", True, f"{len(text.encode())} bytes at {sha[:12]}")
        if sha256_text(text) != cand["content_hash"]:
            res.step("3 verify source and pin", False, "content differs from the reviewed version")
            res.status = "rejected"
            res.reasons.append("content hash changed since discovery; re-run discovery and review again")
            return res
        res.step("3 verify source and pin", True, f"pinned {sha[:12]}")

        # 4 license
        lic = check_license(cand.get("license"), self.allow_copyleft)
        if not res.step("4 license", lic.allowed, f"{lic.spdx} -> {lic.category}; {lic.reason}"):
            res.status = "rejected"
            res.reasons.append(f"license: {lic.reason}")
            return res
        attribution = attribution_notice(cand["repo_url"], cand["path"], sha, lic.spdx, holder_hint=cand["repo"].split("/")[0])

        # 5 + 6 format, size and content scan
        rep = validate_skill_content(text, max_bytes=self.max_bytes)
        if not res.step("5-6 format, size and security scan", rep.ok, "; ".join(rep.rejected_reasons[:3]) or "clean"):
            res.status = "rejected"
            res.reasons += rep.rejected_reasons[:6]
            return res
        flagged = rep.requires_admin_review or lic.category in {"weak_copyleft", "strong_copyleft"}
        if flagged and not (acknowledge_review and approved_by is not None and not auto):
            res.step("6 review required", False, "; ".join(rep.review_reasons[:3]) or lic.reason)
            res.status = "needs_admin_ack"
            res.reasons += rep.review_reasons[:6]
            res.reasons.append("re-run with explicit acknowledgement: /skills_import ID ack. Dependencies/scripts are still never installed or executed automatically.")
            return res

        # same upstream file already installed? -> either no change, or this is an (approved) update of it
        same = next(
            (
                e
                for e in self.registry.list_skills()
                if e.origin == "imported"
                and e.record.get("repo_url") == cand["repo_url"]
                and e.record.get("file_path") == cand["path"]
            ),
            None,
        )
        if same is not None and same.record.get("commit_sha") == sha:
            res.status = "no_change"
            res.ok = True
            res.entry = same
            res.reasons.append("this exact version is already installed")
            return res
        exclude = cand.get("update_for") or (same.name if same else None)

        # 7 + 8 evaluate and compare with installed
        installed = {k: v for k, v in self.registry.installed_contents().items() if k != exclude}
        ev = self.evaluator.evaluate(
            role=cand["role"], text=text, repo=None, license_spdx=cand.get("license"), query=cand.get("query", ""),
            installed=installed, name=cand["name"],
        ).to_dict()
        # keep repository-level scores computed at discovery time (they need live repo metadata)
        ev["total"] = cand["evaluation"]["total"] if not ev["rejected"] else 0.0
        ev["components"] = cand["evaluation"]["components"]
        ev["reasons"] = cand["evaluation"]["reasons"]
        if not res.step("7 evaluate", not ev["rejected"] and ev["total"] >= self.min_score, f"score {ev['total']}"):
            res.status = "rejected"
            res.reasons += ev["reject_reasons"][:5] or [f"score {ev['total']} below minimum {self.min_score}"]
            return res
        res.step("8 compare with installed", True, f"max similarity {ev['similarity']:.0%}" + (f" with {ev['duplicate_of']}" if ev["duplicate_of"] else ""))

        # name handling: never silently overwrite a different skill
        try:
            name = SkillLoader(strict=False).parse(text, origin="imported").name
        except SkillFormatError as exc:
            res.step("5 format", False, str(exc))
            res.status = "rejected"
            return res
        if cand.get("kind") == "update":
            name = cand["update_for"]
        elif same is not None:
            name = same.name  # newer commit of an installed skill: explicit approval = this call; old version kept for rollback
        else:
            existing = self.registry.get_entry(name)
            if existing is not None:
                # a different skill already uses this name: namespace ours, never overwrite
                name = f"{cand['repo'].split('/')[0].lower()}-{name}"[:64]
                clash = self.registry.get_entry(name)
                if clash is not None and clash.record.get("repo_url") != cand["repo_url"]:
                    res.step("8 name collision", False, f"{name} already installed from another source")
                    res.status = "rejected"
                    return res

        record = build_version_record(
            repo_url=cand["repo_url"], file_path=cand["path"], commit_sha=sha, license_spdx=lic.spdx,
            content=text, evaluation=ev, attribution=attribution,
        )
        # 9 stage
        staged = self.registry.stage(name, text, attribution, record)
        res.step("9 stage", True, str(staged.relative_to(self.registry.root)))
        # 10 validation tests
        problems = self.registry.validate_staged(staged, record, self.max_bytes)
        if not res.step("10 validation tests", not problems, "; ".join(problems[:3]) or "passed"):
            import shutil

            shutil.rmtree(staged, ignore_errors=True)
            res.status = "failed"
            res.reasons += problems
            return res
        # 11 activate
        try:
            entry = self.registry.activate_staged(name, staged, record, [cand["role"]])
        except (RegistryError, OSError) as exc:
            res.step("11 activate", False, f"{exc} (previous state restored)")
            res.reasons.append("activation failed and was rolled back")
            return res
        res.step("11 activate", True, f"{'auto' if auto else 'approved by ' + str(approved_by)}")
        cand["status"] = "imported"
        await self.storage.put_doc(CANDIDATES, cid, cand)
        res.ok = True
        res.status = "activated"
        res.entry = entry
        return res

    # --------------------------------------------------------------- updates
    async def check_update(self, name: str) -> dict[str, Any]:
        """Look for a newer upstream version. Creates an *update candidate*; never overwrites."""
        entry = self.registry.get_entry(name)
        if entry is None or entry.origin != "imported":
            return {"status": "not_applicable", "message": f"{name!r} is not an imported skill"}
        rec = entry.record
        repo_full = rec["repo_url"].replace("https://github.com/", "")
        try:
            repo = await self.github.get_repo(repo_full)
            sha = await self.github.get_commit_sha(repo_full, repo.default_branch)
        except RateLimited as exc:
            return {"status": "rate_limited", "message": f"rate limited, retry in {int(exc.retry_after_s)}s"}
        except GitHubError as exc:
            return {"status": "error", "message": str(exc)}
        if sha == rec.get("commit_sha"):
            return {"status": "up_to_date", "message": f"{name} is at the latest commit {sha[:12]}"}
        cand = await self.discovery._evaluate_file(
            repo, rec["file_path"], sha, entry.roles[0] if entry.roles else "backend", name, "",
            self.registry.installed_contents(), kind="update", update_for=name,
        )
        if cand is None:
            return {"status": "error", "message": "could not fetch the updated file"}
        if cand["content_hash"] == rec.get("content_hash"):
            return {"status": "up_to_date", "message": "upstream commit changed but the Skill file is identical"}
        return {
            "status": "update_available",
            "candidate_id": cand["id"],
            "message": f"update available ({rec.get('commit_sha','')[:8]} -> {sha[:8]}); review with /skills_review {cand['id']} then /skills_import {cand['id']}",
            "candidate": cand,
        }
