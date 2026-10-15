"""SkillRegistry: installed Skills (built-in + imported) with versions, provenance, enable/disable,
update tracking and rollback. State lives on disk (SKILL.md + manifest.json per imported Skill)
so it survives restarts and can be audited with plain tools.

Layout:
  app/skills/builtin/<name>/SKILL.md                        shipped Skills (role-mapped via frontmatter)
  app/skills/imported/<name>/SKILL.md                       ACTIVE imported version
  app/skills/imported/<name>/ATTRIBUTION.md                 required attribution / license notice
  app/skills/imported/<name>/manifest.json                  provenance for every version
  app/skills/imported/<name>/.versions/<version_id>/SKILL.md   previous versions (for rollback)
  app/skills/imported/.staging/<name>/<version_id>/...      candidates being validated
  app/skills/registry_state.json                            enable/disable flags for built-in Skills
"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .loader import Skill, SkillFormatError, SkillLoader
from .provenance import (
    build_manifest,
    read_manifest,
    sha256_text,
    verify_content_hash,
    write_manifest,
)
from .validator import validate_skill_content


class RegistryError(Exception):
    pass


@dataclass
class SkillEntry:
    name: str
    roles: list[str]
    version: str
    origin: str  # builtin | imported
    enabled: bool
    description: str
    path: Path
    score: float = 0.0
    record: dict[str, Any] = field(default_factory=dict)

    def label(self) -> str:
        src = "imported" if self.origin == "imported" else "built-in"
        return f"{self.name} v{self.version} [{src}{'' if self.enabled else ', DISABLED'}]"


class SkillRegistry:
    def __init__(self, skills_dir: Path, *, loader: Optional[SkillLoader] = None):
        self.root = Path(skills_dir)
        self.builtin_dir = self.root / "builtin"
        self.imported_dir = self.root / "imported"
        self.staging_dir = self.imported_dir / ".staging"
        self.state_path = self.root / "registry_state.json"
        self.loader = loader or SkillLoader(strict=True)
        self.imported_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- state
    def _state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"disabled_builtin": []}

    def _save_state(self, st: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.state_path)

    # ----------------------------------------------------------- listing
    def _builtin_entries(self) -> list[SkillEntry]:
        out = []
        disabled = set(self._state().get("disabled_builtin", []))
        for name, sk in self.loader.load_dir(self.builtin_dir, origin="builtin").items():
            out.append(
                SkillEntry(name, [sk.role], sk.version, "builtin", name not in disabled, sk.description, sk.path or self.builtin_dir)
            )
        return out

    def _imported_entries(self) -> list[SkillEntry]:
        out = []
        if not self.imported_dir.is_dir():
            return out
        for d in sorted(p for p in self.imported_dir.iterdir() if p.is_dir() and not p.name.startswith(".")):
            man = read_manifest(d / "manifest.json")
            f = d / "SKILL.md"
            if not man or not f.is_file():
                continue
            active = next((v for v in man.get("versions", []) if v.get("version_id") == man.get("active_version")), {})
            try:
                sk = SkillLoader(strict=False).parse(f.read_text(encoding="utf-8"), path=f, origin="imported")
                desc = sk.description
            except SkillFormatError:
                desc = ""
            score = float((active.get("evaluation") or {}).get("total", 0))
            out.append(
                SkillEntry(
                    d.name, list(man.get("roles", [])), man.get("active_version", ""), "imported",
                    bool(man.get("enabled", True)) and active.get("status") == "approved", desc, f, score, active,
                )
            )
        return out

    def list_skills(self, include_disabled: bool = True) -> list[SkillEntry]:
        entries = self._builtin_entries() + self._imported_entries()
        if not include_disabled:
            entries = [e for e in entries if e.enabled]
        return entries

    def get_entry(self, name: str) -> Optional[SkillEntry]:
        for e in self.list_skills():
            if e.name == name:
                return e
        return None

    def get_skill(self, name: str, *, enabled_only: bool = True) -> Optional[Skill]:
        e = self.get_entry(name)
        if e is None or (enabled_only and not e.enabled):
            return None
        text = e.path.read_text(encoding="utf-8")
        if e.origin == "imported":
            # integrity check: file on disk must still match provenance hash
            if e.record and not verify_content_hash(e.record, text):
                raise RegistryError(f"integrity check failed for imported skill {name!r} (content hash mismatch)")
            return SkillLoader(strict=False).parse(text, path=e.path, origin="imported")
        return self.loader.parse(text, path=e.path, origin="builtin")

    def for_role(self, role: str) -> list[SkillEntry]:
        return [e for e in self.list_skills(include_disabled=False) if role in e.roles]

    def search(self, query: str, role: Optional[str] = None, limit: int = 10) -> list[SkillEntry]:
        toks = set(re.findall(r"[a-z0-9\.\+#]{2,}", query.lower()))
        scored = []
        for e in self.list_skills(include_disabled=False):
            if role and role not in e.roles:
                continue
            hay = set(re.findall(r"[a-z0-9\.\+#]{2,}", f"{e.name} {e.description} {' '.join(e.roles)}".lower()))
            s = len(toks & hay)
            if not toks:
                s = 1
            if s > 0:
                scored.append((s, e.score, e))
        scored.sort(key=lambda t: (-t[0], -t[1], t[2].name))
        return [e for _, _, e in scored[:limit]]

    def installed_contents(self) -> dict[str, str]:
        out = {}
        for e in self.list_skills(include_disabled=True):
            try:
                out[e.name] = e.path.read_text(encoding="utf-8")
            except OSError:
                pass
        return out

    # ----------------------------------------------------- enable/disable
    def disable(self, name: str) -> None:
        e = self.get_entry(name)
        if e is None:
            raise RegistryError(f"unknown skill {name!r}")
        self._set_enabled(e, False)

    def enable(self, name: str) -> None:
        e = self.get_entry(name)
        if e is None:
            raise RegistryError(f"unknown skill {name!r}")
        self._set_enabled(e, True)

    def _set_enabled(self, e: SkillEntry, enabled: bool) -> None:
        if e.origin == "builtin":
            st = self._state()
            dis = set(st.get("disabled_builtin", []))
            (dis.discard if enabled else dis.add)(e.name)
            st["disabled_builtin"] = sorted(dis)
            self._save_state(st)
        else:
            mpath = self.imported_dir / e.name / "manifest.json"
            man = read_manifest(mpath) or {}
            man["enabled"] = enabled
            write_manifest(mpath, man)

    # --------------------------------------------------- staging + install
    def stage(self, name: str, content: str, attribution: str, record: dict[str, Any]) -> Path:
        d = self.staging_dir / name / record["version_id"]
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(content, encoding="utf-8")
        (d / "ATTRIBUTION.md").write_text(attribution, encoding="utf-8")
        (d / "provenance.json").write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
        return d

    def validate_staged(self, staged: Path, record: dict[str, Any], max_bytes: int = 65536) -> list[str]:
        """Run validation tests on a staged copy. Returns a list of problems (empty = ok)."""
        problems: list[str] = []
        f = staged / "SKILL.md"
        if not f.is_file():
            return ["staged SKILL.md missing"]
        text = f.read_text(encoding="utf-8")
        if not verify_content_hash(record, text):
            problems.append("staged content hash does not match provenance record")
        rep = validate_skill_content(text, max_bytes=max_bytes)
        problems += rep.rejected_reasons
        if not (staged / "ATTRIBUTION.md").is_file():
            problems.append("attribution notice missing")
        try:
            SkillLoader(strict=False).parse(text, path=f, origin="imported")
        except SkillFormatError as exc:
            problems.append(str(exc))
        return problems

    def activate_staged(self, name: str, staged: Path, record: dict[str, Any], roles: list[str]) -> SkillEntry:
        """Atomically activate a staged version; on ANY failure the previous state is restored."""
        target = self.imported_dir / name
        snapshot = self._snapshot(target)
        try:
            man = read_manifest(target / "manifest.json") or {}
            versions: list[dict[str, Any]] = [v for v in man.get("versions", []) if v.get("version_id") != record["version_id"]]
            current = target / "SKILL.md"
            if current.is_file() and man.get("active_version"):
                old_id = man["active_version"]
                vdir = target / ".versions" / old_id
                vdir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(current, vdir / "SKILL.md")
                if (target / "ATTRIBUTION.md").is_file():
                    shutil.copy2(target / "ATTRIBUTION.md", vdir / "ATTRIBUTION.md")
                for v in versions:
                    if v.get("version_id") == old_id and v.get("status") == "approved":
                        v["status"] = "superseded"
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(staged / "SKILL.md", target / "SKILL.md")
            shutil.copy2(staged / "ATTRIBUTION.md", target / "ATTRIBUTION.md")
            rec = dict(record)
            rec["status"] = "approved"
            versions.append(rec)
            manifest = build_manifest(name, sorted(set(roles) | set(man.get("roles", []))), versions, rec["version_id"], enabled=True)
            write_manifest(target / "manifest.json", manifest)
            self._post_activate_check(target, rec)
        except Exception:
            self._restore(target, snapshot)
            raise
        shutil.rmtree(staged, ignore_errors=True)
        entry = self.get_entry(name)
        assert entry is not None
        return entry

    def _post_activate_check(self, target: Path, rec: dict[str, Any]) -> None:
        text = (target / "SKILL.md").read_text(encoding="utf-8")
        if not verify_content_hash(rec, text):
            raise RegistryError("post-activation integrity check failed")

    @staticmethod
    def _snapshot(target: Path) -> Optional[dict[str, bytes]]:
        if not target.exists():
            return None
        snap = {}
        for p in target.rglob("*"):
            if p.is_file():
                snap[str(p.relative_to(target))] = p.read_bytes()
        return snap

    @staticmethod
    def _restore(target: Path, snap: Optional[dict[str, bytes]]) -> None:
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        if snap is None:
            return
        for rel, data in snap.items():
            p = target / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)

    # ----------------------------------------------------------- rollback
    def versions(self, name: str) -> list[dict[str, Any]]:
        man = read_manifest(self.imported_dir / name / "manifest.json")
        return list(man.get("versions", [])) if man else []

    def rollback(self, name: str) -> SkillEntry:
        """Restore the most recent previously approved version. The current one is kept in history."""
        target = self.imported_dir / name
        man = read_manifest(target / "manifest.json")
        if not man:
            raise RegistryError(f"{name!r} is not an imported skill (nothing to roll back)")
        active = man["active_version"]
        versions = man["versions"]
        idx = next((i for i, v in enumerate(versions) if v["version_id"] == active), None)
        if idx is None:
            raise RegistryError("active version missing from manifest")
        prev = next(
            (v for v in reversed(versions[:idx]) if v.get("status") in {"superseded", "approved"} and (target / ".versions" / v["version_id"] / "SKILL.md").is_file()),
            None,
        )
        if prev is None:
            raise RegistryError(f"no previous approved version of {name!r} to roll back to")
        text = (target / ".versions" / prev["version_id"] / "SKILL.md").read_text(encoding="utf-8")
        if not verify_content_hash(prev, text):
            raise RegistryError("previous version failed integrity verification; refusing rollback")
        snapshot = self._snapshot(target)
        try:
            cur_dir = target / ".versions" / active
            cur_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target / "SKILL.md", cur_dir / "SKILL.md")
            (target / "SKILL.md").write_text(text, encoding="utf-8")
            att = target / ".versions" / prev["version_id"] / "ATTRIBUTION.md"
            if att.is_file():
                shutil.copy2(att, target / "ATTRIBUTION.md")
            for v in versions:
                if v["version_id"] == active:
                    v["status"] = "rolled_back"
                if v["version_id"] == prev["version_id"]:
                    v["status"] = "approved"
            man = build_manifest(name, man.get("roles", []), versions, prev["version_id"], enabled=man.get("enabled", True))
            write_manifest(target / "manifest.json", man)
        except Exception:
            self._restore(target, snapshot)
            raise
        entry = self.get_entry(name)
        assert entry is not None
        return entry

    def file_hash(self, name: str) -> str:
        e = self.get_entry(name)
        if e is None:
            raise RegistryError(name)
        return sha256_text(e.path.read_text(encoding="utf-8"))
