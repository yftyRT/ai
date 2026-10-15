"""SkillDiscoveryService: finds, fetches (pinned), validates and ranks candidate Skills on GitHub.

Discovery NEVER activates anything. It produces *candidates* (stored in the persistent `docs`
store) that an administrator reviews and imports. Nothing from a repository is executed.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from app.storage.base import Storage

from .evaluator import ROLE_KEYWORDS, TOPIC_QUERIES, Evaluation, SkillEvaluator
from .github_client import GitHubClient, GitHubError, RateLimited, RepoInfo
from .loader import SkillFormatError, SkillLoader
from .provenance import sha256_text
from .registry import SkillRegistry

CANDIDATES = "skill_candidates"


def candidate_id(repo: str, path: str, sha: str) -> str:
    return "c" + hashlib.sha1(f"{repo}:{path}:{sha}".encode()).hexdigest()[:8]


@dataclass
class DiscoveryResult:
    candidates: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    rate_limited: bool = False
    retry_after_s: float = 0.0
    searched_repos: int = 0

    @property
    def usable(self) -> list[dict[str, Any]]:
        return [c for c in self.candidates if not c["evaluation"]["rejected"]]


class SkillDiscoveryService:
    def __init__(
        self,
        github: GitHubClient,
        evaluator: SkillEvaluator,
        registry: SkillRegistry,
        storage: Storage,
        *,
        min_score: float = 55.0,
        max_bytes: int = 65536,
    ):
        self.github = github
        self.evaluator = evaluator
        self.registry = registry
        self.storage = storage
        self.min_score = min_score
        self.max_bytes = max_bytes

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def role_for_query(query: str) -> Optional[str]:
        q = query.lower()
        for topic, role in TOPIC_QUERIES.items():
            if topic in q:
                return role
        best, best_n = None, 0
        for role, kws in ROLE_KEYWORDS.items():
            n = sum(1 for k in kws if k in q)
            if n > best_n:
                best, best_n = role, n
        return best

    @staticmethod
    def build_queries(query: str) -> list[str]:
        q = re.sub(r"[^\w\s\.\+#\-]", " ", query).strip()
        return [
            f"{q} agent skills in:name,description,readme",
            f"topic:agent-skills {q}",
            f"topic:claude-skills {q}",
        ]

    def _path_relevance(self, path: str, tokens: set[str]) -> int:
        parts = set(re.findall(r"[a-z0-9\.\+#]{2,}", path.lower()))
        return len(parts & tokens)

    # ------------------------------------------------------------------ main
    async def discover(
        self,
        query: str,
        *,
        role: Optional[str] = None,
        project_context: str = "",
        max_repos: int = 4,
        max_files_per_repo: int = 3,
    ) -> DiscoveryResult:
        res = DiscoveryResult()
        role = role or self.role_for_query(query) or "backend"
        installed = self.registry.installed_contents()
        tokens = set(re.findall(r"[a-z0-9\.\+#]{2,}", query.lower())) | set(ROLE_KEYWORDS.get(role, []))
        repos: dict[str, RepoInfo] = {}
        extra_paths: dict[str, list[str]] = {}
        try:
            if self.github.authenticated:
                for hit in await self.github.search_code_skill_files(query):
                    extra_paths.setdefault(hit["repo"], []).append(hit["path"])
            else:
                res.notes.append("No GITHUB_TOKEN: code search skipped (repository search only).")
            for q in self.build_queries(query):
                for r in await self.github.search_repositories(q, per_page=8):
                    repos.setdefault(r.full_name, r)
            for name in extra_paths:
                if name not in repos:
                    repos[name] = await self.github.get_repo(name)
        except RateLimited as exc:
            res.rate_limited = True
            res.retry_after_s = exc.retry_after_s
            res.notes.append(f"GitHub rate limit reached; retry in ~{int(exc.retry_after_s)}s. Partial results returned.")
        except GitHubError as exc:
            res.notes.append(f"GitHub search failed: {exc}")
            return res

        # Do not simply trust stars: prefer non-archived, recently pushed repos, stars only break ties.
        ordered = sorted(
            (r for r in repos.values() if not r.archived),
            key=lambda r: (r.pushed_at, r.stars),
            reverse=True,
        )[:max_repos]
        for repo in ordered:
            if res.rate_limited:
                break
            res.searched_repos += 1
            try:
                sha = await self.github.get_commit_sha(repo.full_name, repo.default_branch)
                paths = extra_paths.get(repo.full_name) or await self.github.list_skill_paths(repo.full_name, sha)
                paths = sorted(paths, key=lambda p: -self._path_relevance(p, tokens))[:max_files_per_repo]
                for path in paths:
                    cand = await self._evaluate_file(repo, path, sha, role, query, project_context, installed)
                    if cand:
                        res.candidates.append(cand)
            except RateLimited as exc:
                res.rate_limited = True
                res.retry_after_s = exc.retry_after_s
                res.notes.append(f"GitHub rate limit reached while scanning {repo.full_name}; partial results returned.")
            except GitHubError as exc:
                res.notes.append(f"skipped {repo.full_name}: {exc}")
        res.candidates.sort(key=lambda c: (c["evaluation"]["rejected"], -c["evaluation"]["total"]))
        if not res.usable:
            res.notes.append("No suitable Skill could be verified; a locally generated fallback can be used instead.")
        return res

    async def _evaluate_file(
        self,
        repo: RepoInfo,
        path: str,
        sha: str,
        role: str,
        query: str,
        project_context: str,
        installed: dict[str, str],
        *,
        kind: str = "new",
        update_for: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        try:
            text = await self.github.get_file(repo.full_name, path, sha)
        except GitHubError as exc:
            if isinstance(exc, RateLimited):
                raise
            return None
        try:
            parsed = SkillLoader(strict=False).parse(text, origin="imported")
            name = parsed.name
        except SkillFormatError:
            name = re.sub(r"[^a-z0-9\-]+", "-", path.lower().replace("/skill.md", "").split("/")[-1]).strip("-") or "skill"
        installed_other = {k: v for k, v in installed.items() if k != update_for}
        ev: Evaluation = self.evaluator.evaluate(
            role=role,
            text=text,
            repo=repo,
            license_spdx=repo.license_spdx,
            query=query,
            project_context=project_context,
            installed=installed_other,
            name=name,
        )
        cid = candidate_id(repo.full_name, path, sha)
        cand = {
            "id": cid,
            "name": name,
            "role": role,
            "repo": repo.full_name,
            "repo_url": repo.html_url,
            "path": path,
            "commit_sha": sha,
            "license": repo.license_spdx or "unknown",
            "content_hash": sha256_text(text),
            "size_bytes": len(text.encode()),
            "evaluation": ev.to_dict(),
            "repo_meta": {
                "stars": repo.stars,
                "forks": repo.forks,
                "pushed_at": repo.pushed_at,
                "archived": repo.archived,
                "description": repo.description,
                "default_branch": repo.default_branch,
            },
            "query": query,
            "kind": kind,
            "update_for": update_for,
            "status": "rejected" if ev.rejected else "discovered",
        }
        await self.storage.put_doc(CANDIDATES, cid, cand)
        return cand

    # ----------------------------------------------------------- inspection
    async def get_candidate(self, cid: str) -> Optional[dict[str, Any]]:
        return await self.storage.get_doc(CANDIDATES, cid)

    async def review_report(self, cid: str) -> str:
        c = await self.get_candidate(cid)
        if c is None:
            return f"Unknown candidate id {cid!r}. Run /skills_discover first."
        ev = c["evaluation"]
        lines = [
            f"Candidate {c['id']} - {c['name']} (role: {c['role']}, kind: {c['kind']})",
            f"Source: {c['repo_url']}",
            f"File: {c['path']}",
            f"Pinned commit: {c['commit_sha']}",
            f"License: {c['license']} ({ev['license'].get('category')})",
            f"Content hash: {c['content_hash'][:16]}... size {c['size_bytes']} bytes",
            f"Repo: {c['repo_meta']['stars']} stars, {c['repo_meta']['forks']} forks, last push {c['repo_meta']['pushed_at'] or 'unknown'}",
            f"Evaluation: {'REJECTED' if ev['rejected'] else 'score ' + str(ev['total']) + '/100'}",
        ]
        if ev["rejected"]:
            lines += ["Rejected because:"] + [f"  - {r}" for r in ev["reject_reasons"][:8]]
        else:
            lines += ["Reasons:"] + [f"  - {r}" for r in ev["reasons"]]
            if ev["requires_admin_review"]:
                lines.append("NEEDS ADMIN REVIEW: executable/install instructions or restricted license. Import requires explicit acknowledgement (/skills_import ID ack). Nothing is ever installed or executed automatically.")
                for r in ev["validation"].get("review_reasons", [])[:6]:
                    lines.append(f"  - {r}")
        return "\n".join(lines)
