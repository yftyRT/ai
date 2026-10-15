"""Clean ZIP creation, validation, and splitting for Telegram's upload limit."""
from __future__ import annotations

import fnmatch
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .qa import SECRET_PATTERNS

EXCLUDE_DIRS = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache",
                ".ruff_cache", ".sandbox_tmp", ".ssh", ".aws", ".idea", ".vscode", "dist", "build", ".tox", ".cache"}
EXCLUDE_GLOBS = ["*.pyc", "*.pyo", ".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_ed25519*", ".netrc", ".npmrc",
                 ".git-credentials", "credentials*.json", "*.safetensors", "*.gguf", "*.ckpt", "*.pt", "*.pth", "*.onnx",
                 "*.log", ".DS_Store", "*.sqlite", "*.db", ".qa_import_check.py"]
ALLOWED_EXCEPTIONS = {".env.example", ".env.sample", ".env.template"}
REQUIRED_FILES = ["README.md"]
MAX_ENTRY_BYTES = 20_000_000


@dataclass
class PackageResult:
    path: Path
    files: list[str] = field(default_factory=list)
    excluded: list[tuple[str, str]] = field(default_factory=list)
    size: int = 0
    parts: list[Path] = field(default_factory=list)


def _excluded_reason(rel: str, abs_path: Path) -> str | None:
    parts = rel.split("/")
    name = parts[-1]
    if name in ALLOWED_EXCEPTIONS:
        return None
    for seg in parts[:-1]:
        if seg in EXCLUDE_DIRS:
            return f"directory {seg}"
    for g in EXCLUDE_GLOBS:
        if fnmatch.fnmatch(name, g):
            return f"pattern {g}"
    try:
        if abs_path.stat().st_size > MAX_ENTRY_BYTES:
            return "file larger than 20 MB"
        if abs_path.stat().st_size < 500_000 and not name.endswith(".md"):
            text = abs_path.read_text(encoding="utf-8")
            for rx, label in SECRET_PATTERNS:
                if rx.search(text):
                    return f"possible secret ({label})"
    except (UnicodeDecodeError, OSError):
        pass
    return None


def build_zip(project_dir: Path, out_path: Path, root_name: str) -> PackageResult:
    project_dir = Path(project_dir)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    res = PackageResult(path=out_path)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(project_dir.rglob("*")):
            if not p.is_file() or p.is_symlink():
                continue
            rel = p.relative_to(project_dir).as_posix()
            why = _excluded_reason(rel, p)
            if why:
                res.excluded.append((rel, why))
                continue
            zf.write(p, f"{root_name}/{rel}")
            res.files.append(rel)
    res.size = out_path.stat().st_size
    return res


def validate_zip(path: Path, required: list[str] | None = None) -> list[str]:
    """Return a list of problems (empty = valid)."""
    problems: list[str] = []
    try:
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            if bad:
                problems.append(f"corrupt entry: {bad}")
            names = zf.namelist()
            if not names:
                problems.append("archive is empty")
            for n in names:
                low = n.lower()
                base = n.rsplit("/", 1)[-1]
                if n.startswith("/") or ".." in n.split("/"):
                    problems.append(f"unsafe path: {n}")
                if base not in ALLOWED_EXCEPTIONS and (base == ".env" or base.startswith(".env.")):
                    problems.append(f"secret file present: {n}")
                if any(seg in EXCLUDE_DIRS for seg in n.split("/")[:-1]) or low.endswith((".pyc", ".safetensors", ".gguf")):
                    problems.append(f"excluded content present: {n}")
            top = {n.split("/", 1)[1] for n in names if "/" in n}
            for req in (REQUIRED_FILES if required is None else required):
                if req not in top:
                    problems.append(f"required file missing: {req}")
    except zipfile.BadZipFile:
        problems.append("not a valid zip file")
    return problems


def split_file(path: Path, max_bytes: int) -> list[Path]:
    """Split into .zip.001, .zip.002 ... (join with `cat name.zip.* > name.zip`)."""
    path = Path(path)
    if path.stat().st_size <= max_bytes:
        return [path]
    parts: list[Path] = []
    with open(path, "rb") as f:
        i = 1
        while True:
            chunk = f.read(max_bytes)
            if not chunk:
                break
            part = path.with_name(f"{path.name}.{i:03d}")
            part.write_bytes(chunk)
            parts.append(part)
            i += 1
    return parts
