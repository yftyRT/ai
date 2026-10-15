"""Independent final reviewer.

It runs with a FRESH context (no author conversations): it only sees the integrated files and the real
QA output, uses the Code Reviewer Skill, and returns structured findings. Deterministic static checks
are merged in so the review never depends on the model alone.
"""
from __future__ import annotations

import ast
from typing import Any

from app.agents.base import Agent, AgentOutputError, extract_json
from app.providers.base import ChatMessage
from app.schemas.models import Finding, Plan, TestReport
from app.util import truncate

REVIEW_PROMPT = """### REQUEST: REVIEW
You are the independent final reviewer. You did NOT write this project. Review it against the user's request.
Check: requirements coverage, architecture consistency, broken imports, missing files, API/DB mismatches, security
vulnerabilities, exposed secrets, error handling, dependency problems, duplicated or unnecessary code, performance,
missing documentation, test coverage. Only report problems you can support with evidence from the files below.
Respond with ONE JSON object:
{{"summary": "...", "findings": [{{"severity": "high|medium|low|info", "file": "path", "evidence": "quote/line",
  "explanation": "...", "suggested_fix": "...", "category": "..."}}]}}
Severity "high" = broken behaviour, security flaw, secret exposure or missing core requirement.

USER REQUEST:
{prompt}

REAL QA RESULTS:
{qa}

PROJECT FILES:
{files}
"""

VALID_SEV = {"high", "medium", "low", "info"}


class ReviewerAgent(Agent):
    async def review(self, prompt: str, files: dict[str, str], qa_text: str) -> tuple[str, list[Finding], int, int]:
        budget = self.max_context_chars // 2
        blob, used = [], 0
        for path, content in files.items():
            chunk = f"--- FILE {path} ---\n{truncate(content, 12000)}\n"
            if used + len(chunk) > budget:
                blob.append("--- (remaining files omitted for context size) ---")
                break
            blob.append(chunk)
            used += len(chunk)
        user = REVIEW_PROMPT.format(prompt=truncate(prompt, 4000), qa=truncate(qa_text, 4000), files="\n".join(blob))
        res = await self.provider.chat([ChatMessage("system", self.system_prompt()), ChatMessage("user", user)],
                                       max_tokens=min(4096, self.max_output_tokens), temperature=0.1, json_mode=True)
        data = extract_json(res.text)
        if not isinstance(data, dict) or not isinstance(data.get("findings", []), list):
            raise AgentOutputError("review JSON must contain a findings list")
        out: list[Finding] = []
        for raw in data.get("findings", []):
            if not isinstance(raw, dict):
                continue
            sev = str(raw.get("severity", "info")).lower()
            out.append(Finding(
                severity=sev if sev in VALID_SEV else "info", file=str(raw.get("file", "")),
                evidence=str(raw.get("evidence", ""))[:500], explanation=str(raw.get("explanation", "no explanation"))[:1000],
                suggested_fix=str(raw.get("suggested_fix", ""))[:1000], category=str(raw.get("category", "general")),
                source="model"))
        return str(data.get("summary", ""))[:1500], out, res.prompt_tokens, res.completion_tokens


def static_review(files: dict[str, str], plan: Plan | None, report: TestReport | None) -> list[Finding]:
    """Deterministic checks (do not rely on the LLM)."""
    out: list[Finding] = []
    names = set(files)
    if "README.md" not in names:
        out.append(Finding(severity="medium", file="README.md", explanation="README.md is missing",
                           suggested_fix="Document purpose, install, usage and tests.", category="documentation"))
    if "ARCHITECTURE.md" not in names:
        out.append(Finding(severity="low", file="ARCHITECTURE.md", explanation="ARCHITECTURE.md is missing",
                           category="documentation"))
    if not any(n.startswith("tests/") or "/test_" in n or n.startswith("test_") for n in names):
        out.append(Finding(severity="high", file="tests/", explanation="the project contains no tests",
                           suggested_fix="Add automated tests for each acceptance criterion.", category="tests"))
    for path, text in files.items():
        if path.endswith(".py"):
            try:
                ast.parse(text)
            except SyntaxError as exc:
                out.append(Finding(severity="high", file=path, evidence=f"line {exc.lineno}: {exc.msg}",
                                   explanation="Python syntax error", category="correctness"))
            if "eval(" in text or "os.system(" in text or "shell=True" in text:
                out.append(Finding(severity="medium", file=path, evidence="eval/os.system/shell=True",
                                   explanation="dangerous call that may allow code/command injection",
                                   suggested_fix="Use safe APIs and validate input.", category="security"))
    if report is not None:
        for c in report.checks:
            if c.name == "secrets-scan" and not c.passed:
                out.append(Finding(severity="high", file="(multiple)", evidence=c.output[:300],
                                   explanation="possible hard-coded secret", category="security"))
    return out


def owner_of_file(path: str, file_owner: dict[str, str]) -> str | None:
    if path in file_owner:
        return file_owner[path]
    for rel, owner in file_owner.items():
        if path.endswith("/" + rel) or rel.endswith("/" + path):
            return owner
    return None
