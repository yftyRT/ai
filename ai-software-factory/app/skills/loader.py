"""Dynamic SKILL.md loader (frontmatter + required sections)."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

REQUIRED_SECTIONS = [
    "responsibilities",
    "required knowledge",
    "input and output formats",
    "coding standards",
    "allowed file paths",
    "quality requirements",
    "testing rules",
    "error handling",
    "completion criteria",
    "escalation conditions",
]

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{1,63}$")


class SkillFormatError(ValueError):
    pass


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Minimal `key: value` frontmatter parser (no YAML dependency, no code execution)."""
    text = text.lstrip("﻿")
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    meta: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip().lower()] = v.strip().strip("'\"")
    return meta, "\n".join(lines[end + 1 :])


def parse_sections(body: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: Optional[str] = None
    for line in body.splitlines():
        m = re.match(r"^#{2,3}\s+(.+?)\s*$", line)
        if m:
            current = m.group(1).strip().lower().rstrip(":")
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


@dataclass
class Skill:
    name: str
    description: str
    role: str
    version: str
    body: str
    sections: dict[str, str] = field(default_factory=dict)
    path: Optional[Path] = None
    origin: str = "builtin"  # builtin | imported
    content_hash: str = ""
    meta: dict[str, str] = field(default_factory=dict)

    def missing_sections(self) -> list[str]:
        return [s for s in REQUIRED_SECTIONS if not self.sections.get(s)]

    def allowed_paths(self) -> list[str]:
        raw = self.sections.get("allowed file paths", "")
        out = []
        for line in raw.splitlines():
            line = line.strip().lstrip("-*").strip().strip("`")
            if line and not line.startswith("#"):
                out.append(line)
        return out


class SkillLoader:
    def __init__(self, strict: bool = True):
        self.strict = strict

    def parse(self, text: str, path: Optional[Path] = None, origin: str = "builtin", strict: Optional[bool] = None) -> Skill:
        strict = self.strict if strict is None else strict
        meta, body = parse_frontmatter(text)
        name = meta.get("name") or (path.parent.name if path else "")
        if not NAME_RE.match(name or ""):
            raise SkillFormatError(f"invalid or missing skill name: {name!r}")
        description = meta.get("description", "").strip()
        if not description:
            first = next((l.strip("# ").strip() for l in body.splitlines() if l.strip()), "")
            description = first[:200]
        if not body.strip():
            raise SkillFormatError("skill body is empty")
        skill = Skill(
            name=name,
            description=description,
            role=meta.get("role", ""),
            version=meta.get("version", "0.0.0"),
            body=body.strip(),
            sections=parse_sections(body),
            path=path,
            origin=origin,
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            meta=meta,
        )
        if strict:
            missing = skill.missing_sections()
            if missing:
                raise SkillFormatError(f"skill {name!r} is missing required sections: {', '.join(missing)}")
            if skill.role == "":
                raise SkillFormatError(f"skill {name!r} has no role in frontmatter")
        return skill

    def load_file(self, path: Path, origin: str = "builtin", strict: Optional[bool] = None) -> Skill:
        text = path.read_text(encoding="utf-8")
        return self.parse(text, path=path, origin=origin, strict=strict)

    def load_dir(self, directory: Path, origin: str = "builtin") -> dict[str, Skill]:
        skills: dict[str, Skill] = {}
        if not directory.is_dir():
            return skills
        for sub in sorted(p for p in directory.iterdir() if p.is_dir() and not p.name.startswith(".")):
            f = sub / "SKILL.md"
            if f.is_file():
                s = self.load_file(f, origin=origin)
                skills[s.name] = s
        return skills
