"""Role policy lives in CODE, never in Skill files, so a Skill (builtin or imported) can never
widen an agent's file permissions or tools. Skills only add *instructions*."""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.execution.workspace import glob_to_regex
from app.schemas.models import ROLES


@dataclass(frozen=True)
class RolePolicy:
    role: str
    title: str
    allow: tuple[str, ...]
    deny: tuple[str, ...] = ()
    tools: tuple[str, ...] = ("write_files",)
    keywords: tuple[str, ...] = ()


_COMMON_DENY = (".git/**", ".env")
_NON_CODE = ("tests/**", "conftest.py", "docs/**", "README.md", "ARCHITECTURE.md", "SECURITY.md", "spec/**")

ROLE_POLICY: dict[str, RolePolicy] = {p.role: p for p in [
    RolePolicy("architect", "Project Architect",
               ("ARCHITECTURE.md", "docs/**", "spec/**", ".env.example", ".gitignore"), _COMMON_DENY),
    RolePolicy("backend", "Backend Developer", ("**",),
               _COMMON_DENY + _NON_CODE + ("frontend/**", "static/**", "templates/**", "public/**", "db/**",
                                           "migrations/**", "integrations/**", "clients/**"),
               keywords=("api", "server", "backend", "cli", "library", "service", "script", "bot", "tool", "app")),
    RolePolicy("frontend", "Frontend Developer", ("frontend/**", "static/**", "templates/**", "public/**"), _COMMON_DENY,
               keywords=("frontend", "web page", "webpage", "website", "ui ", "react", "html", "dashboard", "next.js",
                         "nextjs", "vue", "landing page", "web app", "interface")),
    RolePolicy("database", "Database Engineer",
               ("db/**", "migrations/**", "schema/**", "**/db/**", "**/models.py", "**/repository.py"), _COMMON_DENY,
               keywords=("database", " sql", "sqlite", "postgres", "mysql", "mongodb", "persist", "orm", "schema")),
    RolePolicy("api_integrator", "API Integrator",
               ("integrations/**", "clients/**", "**/integrations/**", "**/clients/**"), _COMMON_DENY,
               keywords=("integrate", "integration", "third-party", "third party", "webhook", "stripe", "oauth",
                         "external api", "github api", "telegram api")),
    RolePolicy("security", "Security Reviewer", ("SECURITY.md", "docs/security/**"), _COMMON_DENY,
               keywords=("auth", "login", "password", "payment", "security", "encrypt", "jwt", "user account")),
    RolePolicy("tester", "Test Engineer", ("tests/**", "conftest.py", "pytest.ini", "tox.ini"), _COMMON_DENY),
    RolePolicy("reviewer", "Code Reviewer", (), _COMMON_DENY, tools=("read_files",)),
    RolePolicy("docs", "Documentation Writer", ("README.md", "docs/**", "CHANGELOG.md", ".env.example"), _COMMON_DENY),
]}
assert set(ROLE_POLICY) == set(ROLES)

CORE_ROLES = ("architect", "backend", "tester", "docs")


def select_roles(prompt: str) -> list[str]:
    """Heuristic role selection: a simple project gets only the small core team."""
    p = f" {prompt.lower()} "
    chosen = list(CORE_ROLES)
    for role, pol in ROLE_POLICY.items():
        if role in chosen or not pol.keywords or role == "backend":
            continue
        if any(k in p for k in pol.keywords):
            chosen.append(role)
    order = list(ROLE_POLICY)
    return sorted(chosen, key=order.index)


def _covered(pattern: str, allowed: tuple[str, ...]) -> bool:
    # treat the owned pattern as literal text and test it against the allowed globs
    return any(glob_to_regex(a).match(pattern) for a in allowed)


def _literal_prefix(p: str) -> str:
    m = re.search(r"[*?]", p)
    return p[: m.start()] if m else p


def _overlaps(a: str, b: str) -> bool:
    if a == b or glob_to_regex(a).match(b) or glob_to_regex(b).match(a):
        return True
    wild = lambda s: "*" in s or "?" in s  # noqa: E731
    if wild(a) or wild(b):
        pa, pb = _literal_prefix(a), _literal_prefix(b)
        return pa.startswith(pb) or pb.startswith(pa)
    return False


def path_policy_errors(role: str, owned_files: list[str]) -> list[str]:
    """Reasons why `owned_files` is not permitted for `role` (empty list = allowed)."""
    pol = ROLE_POLICY.get(role)
    if pol is None:
        return [f"unknown role {role!r}"]
    errs: list[str] = []
    for p in owned_files:
        if not _covered(p, pol.allow):
            errs.append(f"role {role} may not own {p!r}")
            continue
        for d in pol.deny:
            if _overlaps(p, d):
                errs.append(f"role {role} may not own {p!r} (reserved: {d})")
                break
    return errs
