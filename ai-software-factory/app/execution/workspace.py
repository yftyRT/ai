"""Per-project workspaces, per-task isolated directories, ownership enforcement and integration.

Layout:  <workspace>/<project_id>/tasks/<task_id>/   files written by that agent only
         <workspace>/<project_id>/integrated/        merged result (rebuilt deterministically)
Agents never write to `integrated/` directly; the integration step merges task outputs,
detects conflicts and resolves them explicitly (the file's owner wins; otherwise unresolved).
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from app.schemas.models import Task

MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 8_000_000
MAX_FILES = 300
_FORBIDDEN_NAMES = {".env", ".git", ".ssh", ".aws", ".netrc", ".npmrc", ".git-credentials", "id_rsa", "id_ed25519"}


class WorkspaceError(Exception):
    pass


class OwnershipViolation(WorkspaceError):
    pass


class UnsafePath(WorkspaceError):
    pass


def glob_to_regex(pattern: str) -> re.Pattern:
    """`*` = within one path segment, `**` = across segments, `?` = one char."""
    out = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if pattern[i : i + 2] == "**":
                i += 2
                if pattern[i : i + 1] == "/":
                    i += 1
                    out.append("(?:.*/)?")
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def matches_ownership(rel_path: str, patterns: Iterable[str]) -> bool:
    return any(glob_to_regex(p).match(rel_path) for p in patterns)


def clean_relpath(rel: str) -> str:
    if not isinstance(rel, str) or not rel or "\x00" in rel:
        raise UnsafePath(f"invalid path {rel!r}")
    rel = rel.replace("\\", "/").strip()
    if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        raise UnsafePath(f"absolute path not allowed: {rel!r}")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise UnsafePath(f"path traversal not allowed: {rel!r}")
    if len(rel) > 240:
        raise UnsafePath("path too long")
    if any(p in _FORBIDDEN_NAMES for p in parts):
        raise UnsafePath(f"forbidden file name in path: {rel!r}")
    return "/".join(parts)


@dataclass
class Conflict:
    path: str
    tasks: list[str]
    resolution: str  # "owner:<task>" | "unresolved"
    resolved: bool


@dataclass
class IntegrationResult:
    files: list[str] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    file_owner: dict[str, str] = field(default_factory=dict)

    @property
    def unresolved(self) -> list[Conflict]:
        return [c for c in self.conflicts if not c.resolved]


class WorkspaceManager:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- paths
    def project_dir(self, project_id: str) -> Path:
        if not re.match(r"^[A-Za-z0-9_\-]{1,40}$", project_id):
            raise UnsafePath(f"invalid project id {project_id!r}")
        return self.root / project_id

    def task_dir(self, project_id: str, task_id: str) -> Path:
        if not re.match(r"^[a-z][a-z0-9_\-]{0,47}$", task_id):
            raise UnsafePath(f"invalid task id {task_id!r}")
        return self.project_dir(project_id) / "tasks" / task_id

    def integrated_dir(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "integrated"

    def ensure(self, project_id: str) -> Path:
        d = self.project_dir(project_id)
        for sub in ("tasks", "integrated", "logs"):
            (d / sub).mkdir(parents=True, exist_ok=True)
        return d

    def remove_project(self, project_id: str) -> None:
        shutil.rmtree(self.project_dir(project_id), ignore_errors=True)

    @staticmethod
    def _safe_target(base: Path, rel: str) -> Path:
        rel = clean_relpath(rel)
        target = (base / rel).resolve()
        if base.resolve() not in target.parents and target != base.resolve():
            raise UnsafePath(f"path escapes workspace: {rel!r}")
        # refuse to write through symlinks
        cur = base
        for part in rel.split("/")[:-1]:
            cur = cur / part
            if cur.is_symlink():
                raise UnsafePath(f"symlink in path: {rel!r}")
        if target.is_symlink():
            raise UnsafePath(f"symlink target: {rel!r}")
        return target

    # -------------------------------------------------- agent file output
    def write_task_files(self, project_id: str, task: Task, files: dict[str, str], *, clean: bool = True) -> list[str]:
        """Write an agent's output into ITS OWN task dir, enforcing file ownership and size limits."""
        if len(files) > MAX_FILES:
            raise WorkspaceError(f"too many files ({len(files)} > {MAX_FILES})")
        total = 0
        normalized: dict[str, str] = {}
        for raw_path, content in files.items():
            rel = clean_relpath(raw_path)
            if not matches_ownership(rel, task.owned_files):
                raise OwnershipViolation(
                    f"task {task.id} may not modify {rel!r}; owned patterns: {task.owned_files}"
                )
            if not isinstance(content, str):
                raise WorkspaceError(f"content of {rel!r} is not text")
            size = len(content.encode("utf-8"))
            if size > MAX_FILE_BYTES:
                raise WorkspaceError(f"{rel!r} is too large ({size} bytes)")
            total += size
            normalized[rel] = content
        if total > MAX_TOTAL_BYTES:
            raise WorkspaceError("total output too large")
        base = self.task_dir(project_id, task.id)
        if clean and base.exists():
            shutil.rmtree(base)
        base.mkdir(parents=True, exist_ok=True)
        written = []
        for rel, content in normalized.items():
            target = self._safe_target(base, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            written.append(rel)
        return sorted(written)

    def read_dir_files(self, directory: Path, *, max_bytes: int = 200_000) -> dict[str, str]:
        out: dict[str, str] = {}
        total = 0
        if not directory.is_dir():
            return out
        for p in sorted(directory.rglob("*")):
            if p.is_file() and not p.is_symlink():
                rel = p.relative_to(directory).as_posix()
                if any(seg.startswith(".") and seg not in (".env.example",) for seg in rel.split("/")) and rel != ".env.example":
                    continue
                if "__pycache__" in rel:
                    continue
                try:
                    text = p.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                total += len(text)
                if total > max_bytes:
                    break
                out[rel] = text
        return out

    def read_task_files(self, project_id: str, task_id: str, max_bytes: int = 60_000) -> dict[str, str]:
        return self.read_dir_files(self.task_dir(project_id, task_id), max_bytes=max_bytes)

    # ---------------------------------------------------------- integration
    def integrate(self, project_id: str, ordered_tasks: list[Task]) -> IntegrationResult:
        """Merge COMPLETED task outputs into integrated/ (rebuilt from scratch every time)."""
        result = IntegrationResult()
        dest = self.integrated_dir(project_id)
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        seen: dict[str, tuple[str, str]] = {}  # rel -> (task_id, content)
        for task in ordered_tasks:
            tdir = self.task_dir(project_id, task.id)
            if not tdir.is_dir():
                continue
            for p in sorted(tdir.rglob("*")):
                if not p.is_file() or p.is_symlink():
                    continue
                rel = p.relative_to(tdir).as_posix()
                content = p.read_text(encoding="utf-8")
                if rel in seen and seen[rel][1] != content:
                    prev_task = seen[rel][0]
                    owners = [t for t in (prev_task, task.id) if matches_ownership(rel, self._owned(ordered_tasks, t))]
                    if len(owners) == 1:
                        winner = owners[0]
                        result.conflicts.append(Conflict(rel, [prev_task, task.id], f"owner:{winner}", True))
                        if winner == task.id:
                            seen[rel] = (task.id, content)
                            result.file_owner[rel] = task.id
                    else:
                        # both or neither own it: keep the first, flag unresolved (explicit, never silent)
                        result.conflicts.append(Conflict(rel, [prev_task, task.id], "unresolved", False))
                    continue
                seen[rel] = (task.id, content)
                result.file_owner[rel] = task.id
        for rel, (tid, content) in sorted(seen.items()):
            target = self._safe_target(dest, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            result.files.append(rel)
        return result

    @staticmethod
    def _owned(tasks: list[Task], task_id: str) -> list[str]:
        for t in tasks:
            if t.id == task_id:
                return t.owned_files
        return []

    def apply_repair(self, project_id: str, task: Task, files: dict[str, str]) -> list[str]:
        """Repairs are written back into the responsible task's dir (merge with existing files)."""
        return self.write_task_files(project_id, task, {**self.read_task_files(project_id, task.id, 10**9), **files}, clean=False)
