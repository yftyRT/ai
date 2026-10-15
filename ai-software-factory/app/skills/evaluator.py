"""SkillEvaluator: transparent, rule-based scoring (0-100) with human-readable reasons.

The score is the sum of the weighted components below minus explicit penalties. Security
and licensing are decided by deterministic rules (validator.py) and are HARD gates: a rejected
candidate scores 0 regardless of how good it reads. An optional LLM judge may only nudge the
`relevance`/`completeness` components by at most +-5 points; it never overrides gates.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .github_client import RepoInfo
from .loader import SkillFormatError, SkillLoader
from .validator import LicenseDecision, ValidationReport, check_license, validate_skill_content

WEIGHTS = {
    "relevance": 30,
    "completeness": 15,
    "correctness": 10,
    "compatibility": 10,
    "maintenance": 10,
    "credibility": 10,
    "license": 10,
    "dependencies_security": 5,
}  # sums to 100

ROLE_KEYWORDS: dict[str, list[str]] = {
    "architect": ["architecture", "design", "system design", "adr", "api contract", "structure", "planning", "spec"],
    "frontend": ["frontend", "front-end", "react", "next.js", "nextjs", "css", "html", "ui", "component", "typescript", "javascript", "accessibility"],
    "backend": ["backend", "back-end", "python", "fastapi", "flask", "django", "server", "service", "api", "node", "express"],
    "database": ["database", "sql", "schema", "migration", "postgres", "sqlite", "orm", "query", "index"],
    "api_integrator": ["api", "integration", "http", "rest", "webhook", "client", "sdk", "oauth", "graphql"],
    "security": ["security", "vulnerab", "owasp", "threat", "secret", "auth", "injection", "hardening"],
    "tester": ["test", "testing", "pytest", "unittest", "jest", "tdd", "coverage", "debug", "qa"],
    "reviewer": ["review", "code review", "lint", "refactor", "quality", "debug", "performance"],
    "docs": ["documentation", "readme", "docs", "writing", "docstring", "guide", "changelog"],
}

TOPIC_QUERIES: dict[str, str] = {
    "frontend development": "frontend",
    "backend development": "backend",
    "database design": "database",
    "api development and integration": "api_integrator",
    "python engineering": "backend",
    "typescript and javascript": "frontend",
    "react and next.js": "frontend",
    "software architecture": "architect",
    "debugging": "tester",
    "automated testing": "tester",
    "code review": "reviewer",
    "security review": "security",
    "performance optimization": "reviewer",
    "devops and deployment": "backend",
    "documentation": "docs",
    "coding-agent workflows": "architect",
}

FRAMEWORK_WORDS = [
    "python", "typescript", "javascript", "react", "next.js", "nextjs", "vue", "svelte", "fastapi", "flask", "django",
    "node", "express", "go", "rust", "java", "sqlite", "postgres", "docker",
]


@dataclass
class Evaluation:
    total: float
    components: dict[str, float]
    reasons: list[str]
    rejected: bool = False
    reject_reasons: list[str] = field(default_factory=list)
    requires_admin_review: bool = False
    duplicate_of: Optional[str] = None
    similarity: float = 0.0
    license: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": round(self.total, 1),
            "components": {k: round(v, 1) for k, v in self.components.items()},
            "reasons": self.reasons,
            "rejected": self.rejected,
            "reject_reasons": self.reject_reasons,
            "requires_admin_review": self.requires_admin_review,
            "duplicate_of": self.duplicate_of,
            "similarity": round(self.similarity, 2),
            "license": self.license,
            "validation": self.validation,
        }

    def summary(self) -> str:
        if self.rejected:
            return "REJECTED: " + "; ".join(self.reject_reasons[:3])
        parts = ", ".join(f"{k}={v:.0f}" for k, v in self.components.items())
        return f"score {self.total:.0f}/100 ({parts})"


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9\.\+#]{2,}", text.lower()))


def shingles(text: str, n: int = 4) -> set[tuple[str, ...]]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    if len(words) < n:
        return {tuple(words)} if words else set()
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def similarity(a: str, b: str) -> float:
    sa, sb = shingles(a), shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _days_since(iso: str, now: Optional[datetime] = None) -> Optional[float]:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - dt).total_seconds() / 86400)


class SkillEvaluator:
    def __init__(self, *, allow_copyleft: bool = False, max_bytes: int = 65536, now: Optional[datetime] = None):
        self.allow_copyleft = allow_copyleft
        self.max_bytes = max_bytes
        self.now = now

    # ---------------------------------------------------------------- parts
    def relevance(self, role: str, text: str, query: str = "", name: str = "") -> tuple[float, list[str]]:
        kws = ROLE_KEYWORDS.get(role, [])
        lower = text.lower()
        hit = sum(1 for k in kws if k in lower)
        name_hit = any(k.replace(" ", "-") in name.lower() or k in name.lower() for k in kws)
        role_part = min(1.0, hit / max(3, len(kws) * 0.5))
        q_tokens = _tokens(query) - {"the", "and", "for", "agent", "skill", "skills"}
        q_part = 0.0
        if q_tokens:
            q_part = len(q_tokens & _tokens(text)) / len(q_tokens)
        score = WEIGHTS["relevance"] * (0.65 * role_part + 0.25 * q_part + (0.10 if name_hit else 0.0))
        reasons = [f"relevance: {hit}/{len(kws)} role keywords, query overlap {q_part:.0%}" + (", name matches role" if name_hit else "")]
        return score, reasons

    def completeness(self, text: str) -> tuple[float, list[str]]:
        n = len(text)
        headings = len(re.findall(r"(?m)^#{1,4}\s+\S", text))
        has_steps = bool(re.search(r"(?mi)^\s*(\d+\.|[-*])\s+\S", text))
        has_example = "```" in text or bool(re.search(r"(?i)\bexample", text))
        score = 0.0
        if 400 <= n <= 30000:
            score += 6
        elif 150 <= n < 400 or 30000 < n <= 60000:
            score += 3
        score += min(4, headings)  # up to 4
        score += 2.5 if has_steps else 0
        score += 2.5 if has_example else 0
        score = min(score, WEIGHTS["completeness"])
        return score, [f"completeness: {n} chars, {headings} headings, steps={has_steps}, examples={has_example}"]

    def correctness(self, text: str) -> tuple[float, list[str]]:
        """Heuristic proxy only (no execution): balanced fences, no placeholders, no broken markdown."""
        score = WEIGHTS["correctness"]
        reasons = []
        if text.count("```") % 2 == 1:
            score -= 4
            reasons.append("unbalanced code fences")
        placeholders = len(re.findall(r"(?i)\bTODO\b|\bFIXME\b|lorem ipsum|<insert[^>]*>|\bTBD\b", text))
        if placeholders:
            score -= min(4, placeholders)
            reasons.append(f"{placeholders} placeholder markers")
        if re.search(r"(?i)deprecated|no longer maintained|unmaintained", text):
            score -= 2
            reasons.append("mentions deprecation")
        return max(0.0, score), ["technical correctness (heuristic): " + (", ".join(reasons) or "no obvious issues")]

    def compatibility(self, text: str, project_context: str) -> tuple[float, list[str]]:
        ctx = _tokens(project_context)
        wanted = {w for w in FRAMEWORK_WORDS if w in ctx}
        if not wanted:
            return WEIGHTS["compatibility"] * 0.6, ["compatibility: project stack unspecified, neutral score"]
        present = {w for w in FRAMEWORK_WORDS if w in text.lower()}
        overlap = wanted & present
        conflict = (present - wanted) if not overlap else set()
        score = WEIGHTS["compatibility"] * (len(overlap) / len(wanted))
        if conflict and len(conflict) >= 2:
            score *= 0.5
        reason = f"compatibility: wants {sorted(wanted)}, skill covers {sorted(overlap) or 'none'}"
        return score, [reason]

    def maintenance(self, repo: Optional[RepoInfo]) -> tuple[float, list[str]]:
        if repo is None:
            return 0.0, ["maintenance: no repository metadata"]
        if repo.archived:
            return 0.0, ["maintenance: repository is archived"]
        days = _days_since(repo.pushed_at, self.now)
        if days is None:
            return 2.0, ["maintenance: unknown last update"]
        if days <= 90:
            s = 10.0
        elif days <= 365:
            s = 6.0
        elif days <= 730:
            s = 3.0
        else:
            s = 0.0
        return s, [f"maintenance: last push {days:.0f} days ago"]

    def credibility(self, repo: Optional[RepoInfo]) -> tuple[float, list[str]]:
        """Stars/forks are SECONDARY signals: capped at 4 of 10 points."""
        if repo is None:
            return 0.0, ["credibility: no repository metadata"]
        pop = min(4.0, math.log10(1 + repo.stars + 2 * repo.forks) * 1.3)
        s = pop
        notes = [f"{repo.stars} stars/{repo.forks} forks (capped secondary signal)"]
        if repo.description:
            s += 2
        if repo.license_spdx:
            s += 2
        if not repo.fork:
            s += 1
        if repo.topics:
            s += 1
        return min(float(WEIGHTS["credibility"]), s), ["credibility: " + "; ".join(notes)]

    # -------------------------------------------------------------- overall
    def evaluate(
        self,
        *,
        role: str,
        text: str,
        repo: Optional[RepoInfo],
        license_spdx: Optional[str],
        query: str = "",
        project_context: str = "",
        installed: Optional[dict[str, str]] = None,  # name -> content of installed skills
        name: str = "",
        llm_adjustment: float = 0.0,
    ) -> Evaluation:
        lic: LicenseDecision = check_license(license_spdx, self.allow_copyleft)
        val: ValidationReport = validate_skill_content(text, max_bytes=self.max_bytes)
        reject: list[str] = []
        if not lic.allowed:
            reject.append(f"license: {lic.reason}")
        reject.extend(val.rejected_reasons)

        dup_of, sim = None, 0.0
        for iname, icontent in (installed or {}).items():
            s = similarity(text, icontent)
            if s > sim:
                sim, dup_of = s, iname
        if sim >= 0.85:
            reject.append(f"duplicate of installed skill {dup_of!r} (similarity {sim:.0%})")

        comps: dict[str, float] = {}
        reasons: list[str] = []
        for fn, args in (
            (self.relevance, (role, text, query, name)),
            (self.completeness, (text,)),
            (self.correctness, (text,)),
            (self.compatibility, (text, project_context)),
        ):
            s, r = fn(*args)  # type: ignore[arg-type]
            key = fn.__name__
            comps[key] = s
            reasons.extend(r)
        comps["maintenance"], r = self.maintenance(repo)
        reasons.extend(r)
        comps["credibility"], r = self.credibility(repo)
        reasons.extend(r)
        comps["license"] = float(WEIGHTS["license"]) if lic.category == "permissive" else (5.0 if lic.allowed else 0.0)
        reasons.append(f"license: {lic.spdx or 'unknown'} -> {lic.category} ({lic.reason})")
        sec = float(WEIGHTS["dependencies_security"])
        if val.review_reasons:
            sec = 0.0
            reasons.append(f"dependencies/security: needs administrator review ({len(val.review_reasons)} item(s))")
        else:
            reasons.append("dependencies/security: no executable or install requirements detected")
        comps["dependencies_security"] = sec

        adj = max(-5.0, min(5.0, llm_adjustment))
        total = sum(comps.values()) + adj
        if adj:
            reasons.append(f"optional LLM quality adjustment: {adj:+.1f} (cannot override gates)")
        if 0.6 <= sim < 0.85:
            total -= 10
            reasons.append(f"duplication penalty: {sim:.0%} similar to installed skill {dup_of!r}")
        total = max(0.0, min(100.0, total))
        if reject:
            total = 0.0
        return Evaluation(
            total=total,
            components=comps,
            reasons=reasons,
            rejected=bool(reject),
            reject_reasons=reject,
            requires_admin_review=bool(val.review_reasons) or lic.category in {"weak_copyleft", "strong_copyleft"},
            duplicate_of=dup_of if sim >= 0.6 else None,
            similarity=sim,
            license={"spdx": lic.spdx, "category": lic.category, "auto_ok": lic.auto_ok, "needs_attribution": lic.needs_attribution},
            validation=val.to_dict(),
        )

    def explain_selection(self, ev: Evaluation, name: str) -> str:
        lines = [f"Selected {name!r}: {ev.summary()}"]
        lines += [f"  - {r}" for r in ev.reasons]
        return "\n".join(lines)
