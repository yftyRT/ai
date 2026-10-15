"""Minimal GitHub REST API client.

* Official REST API only; no HTML scraping.
* Optional token from the environment (unauthenticated public search still works for
  repositories/trees/contents; *code* search requires a token and is skipped without one).
* Conditional requests (ETag) + TTL cache stored via the Storage `docs` collection, so unchanged
  content is not downloaded again and rate limit budget is preserved.
* Rate limits are handled gracefully: `RateLimited` carries the reset time; callers back off.
* The HTTP transport is injectable so unit tests use mocked responses.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from app.storage.base import Storage

CACHE_COLLECTION = "gh_cache"


class GitHubError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class RateLimited(GitHubError):
    def __init__(self, message: str, retry_after_s: float, reset_at: float | None = None):
        super().__init__(message, 403)
        self.retry_after_s = retry_after_s
        self.reset_at = reset_at


class NotFound(GitHubError):
    def __init__(self, message: str = "not found"):
        super().__init__(message, 404)


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


Transport = Callable[[str, dict[str, str]], Awaitable[HttpResponse]]


async def urllib_transport(url: str, headers: dict[str, str]) -> HttpResponse:
    def _do() -> HttpResponse:
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - https GitHub API
                return HttpResponse(resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read())
        except urllib.error.HTTPError as exc:
            return HttpResponse(exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read() or b"")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GitHubError(f"network error talking to GitHub: {getattr(exc, 'reason', exc)}") from None

    return await asyncio.to_thread(_do)


@dataclass
class RepoInfo:
    full_name: str
    html_url: str
    description: str
    default_branch: str
    license_spdx: Optional[str]
    stars: int
    forks: int
    pushed_at: str
    archived: bool
    fork: bool
    topics: list[str] = field(default_factory=list)
    open_issues: int = 0
    size_kb: int = 0

    @classmethod
    def from_api(cls, d: dict) -> "RepoInfo":
        lic = (d.get("license") or {}).get("spdx_id")
        return cls(
            full_name=d["full_name"],
            html_url=d.get("html_url", f"https://github.com/{d['full_name']}"),
            description=d.get("description") or "",
            default_branch=d.get("default_branch") or "main",
            license_spdx=lic,
            stars=int(d.get("stargazers_count", 0)),
            forks=int(d.get("forks_count", 0)),
            pushed_at=d.get("pushed_at") or d.get("updated_at") or "",
            archived=bool(d.get("archived", False)),
            fork=bool(d.get("fork", False)),
            topics=list(d.get("topics") or []),
            open_issues=int(d.get("open_issues_count", 0)),
            size_kb=int(d.get("size", 0)),
        )


class GitHubClient:
    def __init__(
        self,
        storage: Optional[Storage] = None,
        *,
        token: str = "",
        api_url: str = "https://api.github.com",
        transport: Optional[Transport] = None,
        cache_ttl_s: float = 6 * 3600.0,
        clock: Callable[[], float] = time.time,
        user_agent: str = "ai-software-factory-skill-discovery",
    ):
        self.storage = storage
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.transport = transport or urllib_transport
        self.cache_ttl_s = cache_ttl_s
        self.clock = clock
        self.user_agent = user_agent
        self.requests_made = 0
        self.cache_hits = 0
        self.not_modified = 0
        self.rate_remaining: Optional[int] = None
        self.rate_reset: Optional[float] = None

    # ------------------------------------------------------------ plumbing
    @property
    def authenticated(self) -> bool:
        return bool(self.token)

    def _headers(self, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
        h = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": self.user_agent,
        }
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if extra:
            h.update(extra)
        return h

    @staticmethod
    def _ckey(url: str) -> str:
        return hashlib.sha256(url.encode()).hexdigest()[:40]

    def _note_rate(self, resp: HttpResponse) -> None:
        try:
            if "x-ratelimit-remaining" in resp.headers:
                self.rate_remaining = int(resp.headers["x-ratelimit-remaining"])
            if "x-ratelimit-reset" in resp.headers:
                self.rate_reset = float(resp.headers["x-ratelimit-reset"])
        except ValueError:
            pass

    def _raise_for_limit(self, resp: HttpResponse) -> None:
        if resp.status not in (403, 429):
            return
        remaining = resp.headers.get("x-ratelimit-remaining")
        retry_after = resp.headers.get("retry-after")
        body = resp.body.decode("utf-8", "replace").lower()
        limited = resp.status == 429 or remaining == "0" or "rate limit" in body or "abuse" in body or retry_after
        if not limited:
            raise GitHubError(f"GitHub denied the request (HTTP 403): {body[:120]}", 403)
        reset = None
        wait = 60.0
        if retry_after:
            try:
                wait = float(retry_after)
            except ValueError:
                pass
        elif resp.headers.get("x-ratelimit-reset"):
            try:
                reset = float(resp.headers["x-ratelimit-reset"])
                wait = max(1.0, reset - self.clock())
            except ValueError:
                pass
        raise RateLimited("GitHub API rate limit reached", wait, reset)

    async def get(self, path_or_url: str, params: Optional[dict[str, Any]] = None, *, use_cache: bool = True) -> Any:
        """GET JSON with ETag + TTL cache. Raises NotFound / RateLimited / GitHubError."""
        url = path_or_url if path_or_url.startswith("http") else f"{self.api_url}{path_or_url}"
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        cached = None
        key = self._ckey(url)
        if self.storage is not None and use_cache:
            cached = await self.storage.get_doc(CACHE_COLLECTION, key)
            if cached and self.clock() - cached.get("fetched_at", 0) < self.cache_ttl_s:
                self.cache_hits += 1
                return cached["body"]
        extra = {"If-None-Match": cached["etag"]} if cached and cached.get("etag") else None
        resp = await self.transport(url, self._headers(extra))
        self.requests_made += 1
        self._note_rate(resp)
        if resp.status == 304 and cached:
            self.not_modified += 1
            cached["fetched_at"] = self.clock()
            if self.storage is not None:
                await self.storage.put_doc(CACHE_COLLECTION, key, cached)
            return cached["body"]
        self._raise_for_limit(resp)
        if resp.status == 404:
            raise NotFound(f"GitHub resource not found: {path_or_url}")
        if resp.status == 401:
            raise GitHubError("GitHub token rejected (HTTP 401)", 401)
        if resp.status >= 400:
            raise GitHubError(f"GitHub HTTP {resp.status}", resp.status)
        try:
            body = resp.json()
        except json.JSONDecodeError:
            raise GitHubError("GitHub returned invalid JSON") from None
        if self.storage is not None and use_cache:
            await self.storage.put_doc(
                CACHE_COLLECTION,
                key,
                {"url": url, "etag": resp.headers.get("etag"), "fetched_at": self.clock(), "body": body},
            )
        return body

    async def invalidate_cache(self, path_or_url: Optional[str] = None) -> int:
        """Drop one cached URL (or the whole cache). Returns number of entries removed."""
        if self.storage is None:
            return 0
        if path_or_url is not None:
            url = path_or_url if path_or_url.startswith("http") else f"{self.api_url}{path_or_url}"
            await self.storage.delete_doc(CACHE_COLLECTION, self._ckey(url))
            return 1
        docs = await self.storage.list_docs(CACHE_COLLECTION)
        for d in docs:
            await self.storage.delete_doc(CACHE_COLLECTION, d["_key"])
        return len(docs)

    # ---------------------------------------------------------- API methods
    async def search_repositories(self, query: str, per_page: int = 10, sort: str = "updated") -> list[RepoInfo]:
        data = await self.get("/search/repositories", {"q": query, "per_page": per_page, "sort": sort, "order": "desc"})
        return [RepoInfo.from_api(i) for i in data.get("items", [])]

    async def search_code_skill_files(self, query: str, per_page: int = 10) -> list[dict[str, Any]]:
        """Code search for SKILL.md files. GitHub requires authentication for code search."""
        if not self.authenticated:
            return []
        data = await self.get("/search/code", {"q": f"{query} filename:SKILL.md", "per_page": per_page})
        out = []
        for i in data.get("items", []):
            repo = (i.get("repository") or {}).get("full_name")
            if repo and i.get("path"):
                out.append({"repo": repo, "path": i["path"]})
        return out

    async def get_repo(self, full_name: str) -> RepoInfo:
        return RepoInfo.from_api(await self.get(f"/repos/{full_name}"))

    async def get_commit_sha(self, full_name: str, ref: str) -> str:
        data = await self.get(f"/repos/{full_name}/commits/{urllib.parse.quote(ref, safe='')}")
        sha = data.get("sha", "")
        if len(sha) < 7:
            raise GitHubError("could not resolve commit SHA")
        return sha

    async def list_skill_paths(self, full_name: str, sha: str, limit: int = 20) -> list[str]:
        """Find SKILL.md files using the git tree of a pinned commit (one API call)."""
        data = await self.get(f"/repos/{full_name}/git/trees/{sha}", {"recursive": "1"})
        paths = [
            e["path"]
            for e in data.get("tree", [])
            if e.get("type") == "blob" and e.get("path", "").split("/")[-1] == "SKILL.md"
        ]
        return sorted(paths, key=lambda p: (p.count("/"), p))[:limit]

    async def get_file(self, full_name: str, path: str, ref: str) -> str:
        """Fetch a file's text at an immutable ref (commit SHA)."""
        quoted = "/".join(urllib.parse.quote(p) for p in path.split("/"))
        data = await self.get(f"/repos/{full_name}/contents/{quoted}", {"ref": ref})
        if isinstance(data, list) or data.get("type") != "file":
            raise GitHubError(f"{path} is not a file")
        if data.get("encoding") != "base64":
            raise GitHubError("unexpected content encoding")
        if int(data.get("size", 0)) > 1_000_000:
            raise GitHubError("file too large to import")
        try:
            return base64.b64decode(data["content"]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            raise GitHubError("file is not valid UTF-8 text") from None
