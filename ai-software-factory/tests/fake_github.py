"""In-memory GitHub REST API used by the skill-discovery tests (no network)."""
from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

from app.skills.github_client import HttpResponse


def iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def skill_text(name: str, topic: str, extra: str = "") -> str:
    return f"""---
name: {name}
description: Guidance for {topic} with checklists, examples and review steps.
---

# {name}

Use this Skill when working on {topic}. It gives a repeatable workflow for {topic} tasks.

## Responsibilities
- Plan the {topic} work before writing code and keep functions small.
- Validate inputs, handle errors explicitly and write tests for every public function.

## Coding Standards
- Use typed Python 3 code and descriptive names; document public APIs for {topic}.
- Prefer the standard library; keep modules cohesive and avoid duplicated logic.

## Testing Rules
- Write unit tests first for each behaviour; use fakes for network access.

## Completion Criteria
- All tests pass and the {topic} code is documented.
{extra}
"""


class FakeGitHub:
    def __init__(self) -> None:
        self.repos: dict[str, dict[str, Any]] = {}
        self.requests: list[str] = []
        self.rate_limit = False
        self.use_etag = True
        self.authenticated_seen = False

    def add_repo(self, full_name: str, *, files: dict[str, str], license: str | None = "MIT", stars: int = 10,
                 days_old: int = 20, sha: str | None = None, archived: bool = False) -> str:
        sha = sha or format(abs(hash(full_name)) % (16 ** 40), "040x")
        self.repos[full_name] = {"license": license, "stars": stars, "pushed": iso_days_ago(days_old), "sha": sha,
                                 "files": dict(files), "archived": archived}
        return sha

    def _repo_json(self, name: str) -> dict:
        r = self.repos[name]
        return {"full_name": name, "html_url": f"https://github.com/{name}", "description": f"{name} skills",
                "default_branch": "main", "license": {"spdx_id": r["license"]} if r["license"] else None,
                "stargazers_count": r["stars"], "forks_count": r["stars"] // 5, "pushed_at": r["pushed"],
                "archived": r["archived"], "fork": False, "topics": ["agent-skills"], "open_issues_count": 1, "size": 40}

    async def transport(self, url: str, headers: dict[str, str]) -> HttpResponse:
        self.requests.append(url)
        self.authenticated_seen = self.authenticated_seen or "Authorization" in headers
        if self.rate_limit:
            return HttpResponse(403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9999999999"},
                                b'{"message": "API rate limit exceeded"}')
        u = urlparse(url)
        path, qs = u.path, parse_qs(u.query)
        etag = '"v1"'
        if self.use_etag and headers.get("If-None-Match") == etag:
            return HttpResponse(304, {"etag": etag}, b"")
        hdr = {"etag": etag, "x-ratelimit-remaining": "50"}

        def ok(obj: Any) -> HttpResponse:
            return HttpResponse(200, hdr, json.dumps(obj).encode())

        if path == "/search/repositories":
            return ok({"items": [self._repo_json(n) for n in self.repos]})
        if path == "/search/code":
            return ok({"items": [{"path": p, "repository": {"full_name": n}} for n, r in self.repos.items() for p in r["files"]]})
        m = re.match(r"^/repos/([^/]+/[^/]+)(/.*)?$", path)
        if not m or m.group(1) not in self.repos:
            return HttpResponse(404, {}, b'{"message": "Not Found"}')
        name, rest = m.group(1), m.group(2) or ""
        repo = self.repos[name]
        if rest == "":
            return ok(self._repo_json(name))
        if rest.startswith("/commits/"):
            return ok({"sha": repo["sha"]})
        if rest.startswith("/git/trees/"):
            return ok({"tree": [{"path": p, "type": "blob"} for p in repo["files"]]})
        if rest.startswith("/contents/"):
            fpath = rest[len("/contents/"):]
            if fpath not in repo["files"]:
                return HttpResponse(404, {}, b'{"message": "Not Found"}')
            text = repo["files"][fpath]
            return ok({"type": "file", "encoding": "base64", "size": len(text),
                       "content": base64.b64encode(text.encode()).decode()})
        return HttpResponse(404, {}, b"{}")
