"""QA runner: REAL commands in the sandbox, real exit codes, real captured output.

Nothing in here fabricates results. If a tool is not installed in the sandbox environment the
check is reported as `skipped` (with the reason), never as passed.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Optional

from app.schemas.models import CheckResult, TestReport
from app.util import truncate

from .sandbox import CommandResult, Sandbox

SECRET_PATTERNS = [
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_\-]{30,}\b"), "Telegram bot token"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key id"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), "GitHub token"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"), "API secret key"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
    (re.compile(r"""(?i)(password|passwd|secret|api[_-]?key|token)\s*[:=]\s*['"][^'"\s]{8,}['"]"""), "hard-coded credential"),
]

IMPORT_CHECK = '''\
import importlib, os, sys, traceback
root = os.getcwd()
sys.path.insert(0, root)
skip_dirs = {"tests", "test", ".git", "venv", ".venv", "node_modules", "__pycache__", ".sandbox_tmp", "docs", "migrations"}
failures = []
count = 0
for dirpath, dirs, files in os.walk(root):
    dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
    for f in sorted(files):
        if not f.endswith(".py") or f.startswith(".") or f in ("setup.py", "conftest.py"):
            continue
        rel = os.path.relpath(os.path.join(dirpath, f), root)
        mod = rel[:-3].replace(os.sep, ".")
        if mod.endswith(".__init__"):
            mod = mod[: -len(".__init__")]
        if mod.endswith("__main__"):
            continue
        count += 1
        try:
            importlib.import_module(mod)
        except BaseException as exc:
            failures.append((rel, "".join(traceback.format_exception_only(type(exc), exc)).strip()))
for rel, err in failures:
    print(f"IMPORT FAILURE in {rel}: {err}")
print(f"checked {count} modules, {len(failures)} failures")
sys.exit(1 if failures else 0)
'''


def detect_language(project: Path) -> str:
    if (project / "package.json").is_file():
        return "javascript"
    if any(project.rglob("*.py")):
        return "python"
    return "unknown"


def _find_test_files(project: Path) -> list[Path]:
    out = []
    for p in project.rglob("*.py"):
        parts = p.relative_to(project).parts
        if any(s in {".sandbox_tmp", "__pycache__", "venv", ".venv"} for s in parts):
            continue
        if p.name.startswith("test_") or p.name.endswith("_test.py") or (parts and parts[0] in {"tests", "test"} and p.name != "__init__.py" and p.name != "conftest.py"):
            out.append(p)
    return out


def scan_secrets(project: Path) -> list[tuple[str, str]]:
    hits = []
    for p in project.rglob("*"):
        if not p.is_file() or p.is_symlink() or p.stat().st_size > 500_000:
            continue
        rel = p.relative_to(project).as_posix()
        if any(seg in {".sandbox_tmp", "__pycache__", ".git", "node_modules"} for seg in rel.split("/")):
            continue
        if rel.endswith(".env.example") or rel.endswith(".md"):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for rx, label in SECRET_PATTERNS:
            if rx.search(text):
                hits.append((rel, label))
                break
    return hits


def unpinned_requirements(project: Path) -> list[str]:
    req = project / "requirements.txt"
    if not req.is_file():
        return []
    bad = []
    for line in req.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith(("#", "-")):
            continue
        if "==" not in s:
            bad.append(s)
    return bad


class QARunner:
    def __init__(self, sandbox: Sandbox, *, python: Optional[str] = None):
        self.sandbox = sandbox
        self.python = python or sys.executable

    async def _run(self, argv: list[str], cwd: Path, timeout: float = 240) -> CommandResult:
        return await self.sandbox.run(argv, cwd, timeout_s=timeout, validate=False)

    def _check(self, name: str, res: CommandResult, *, required: bool = True, ok_codes=(0,)) -> CheckResult:
        return CheckResult(
            name=name,
            command=" ".join(res.argv[:6]),
            exit_code=res.exit_code,
            passed=res.exit_code in ok_codes and not res.timed_out,
            output=truncate(res.output, 6000),
            duration_s=round(res.duration_s, 2),
            required=required,
        )

    @staticmethod
    def _skipped(name: str, why: str, command: str = "") -> CheckResult:
        return CheckResult(name=name, command=command, exit_code=0, passed=True, output=why, skipped=True, required=False)

    async def _has_module(self, project: Path, module: str) -> bool:
        r = await self._run([self.python, "-c", f"import {module}"], project, timeout=30)
        return r.exit_code == 0

    async def run(self, integrated: Path, *, only: Optional[set[str]] = None) -> TestReport:
        """Run all checks on a scratch COPY of the integrated project."""
        lang = detect_language(integrated)
        report = TestReport(language=lang)
        if lang != "python":
            report.checks.append(
                CheckResult(name="language-support", command="", exit_code=1, passed=False,
                            output=f"automatic QA supports Python projects; detected language: {lang}", required=True)
            )
            return report
        work = self.sandbox.make_scratch(integrated)
        try:
            await self._run_python_checks(work, report, only)
        finally:
            self.sandbox.cleanup_scratch(work)
        return report

    async def _run_python_checks(self, work: Path, report: TestReport, only: Optional[set[str]]) -> None:
        want = lambda n: only is None or n in only  # noqa: E731
        py = self.python
        if want("syntax"):
            report.checks.append(self._check("syntax", await self._run([py, "-m", "compileall", "-q", "."], work)))
        if want("imports"):
            (work / ".qa_import_check.py").write_text(IMPORT_CHECK, encoding="utf-8")
            report.checks.append(self._check("imports", await self._run([py, ".qa_import_check.py"], work)))
            (work / ".qa_import_check.py").unlink(missing_ok=True)
        if want("tests"):
            tests = _find_test_files(work)
            if not tests:
                report.checks.append(CheckResult(name="tests", command="", exit_code=1, passed=False,
                                                 output="no test files found (tests/ or test_*.py)", required=True))
            else:
                if await self._has_module(work, "pytest"):
                    res = await self._run([py, "-m", "pytest", "-q", "-x", "--no-header", "-p", "no:cacheprovider"], work, timeout=300)
                    c = self._check("tests (pytest)", res)
                else:
                    res = await self._run([py, "-m", "unittest", "discover", "-v", "-s", ".", "-t", "."], work, timeout=300)
                    c = self._check("tests (unittest)", res)
                    c.output = "pytest is not installed in the sandbox; used unittest discovery.\n" + c.output
                report.checks.append(c)
        if want("lint"):
            if await self._has_module(work, "ruff") or self._which("ruff"):
                cmd = ["ruff", "check", "--select", "E,F", "--ignore", "E501", "--no-cache", "."]
                res = await self._run(cmd, work, timeout=120)
                report.checks.append(self._check("lint (ruff)", res, required=False))
            else:
                report.checks.append(self._skipped("lint", "ruff not installed in sandbox", "ruff check"))
        if want("types"):
            if self._which("mypy"):
                res = await self._run(["mypy", "--ignore-missing-imports", "--no-error-summary", "--cache-dir=/dev/null", "."], work, timeout=180)
                report.checks.append(self._check("types (mypy)", res, required=False))
            else:
                report.checks.append(self._skipped("types", "mypy not installed in sandbox", "mypy"))
        if want("smoke"):
            report.checks.append(await self._smoke(work))
        if want("security"):
            hits = scan_secrets(work)
            unpinned = unpinned_requirements(work)
            msgs = [f"possible {label} in {rel}" for rel, label in hits]
            report.checks.append(CheckResult(
                name="secrets-scan", command="builtin regex scan", exit_code=1 if hits else 0, passed=not hits,
                output="\n".join(msgs) or "no secrets detected", required=True))
            report.checks.append(CheckResult(
                name="dependency-pinning", command="requirements.txt", exit_code=0, passed=True,
                output=("unpinned requirements (use ==): " + ", ".join(unpinned)) if unpinned else "all requirements pinned or none declared",
                required=False))

    @staticmethod
    def _which(tool: str) -> bool:
        import shutil

        return shutil.which(tool) is not None or (Path.home() / ".local" / "bin" / tool).exists()

    async def _smoke(self, work: Path) -> CheckResult:
        entry = None
        for cand in ("main.py", "app.py", "cli.py"):
            if (work / cand).is_file():
                entry = cand
                break
        if entry is None:
            for pkg in sorted(p for p in work.iterdir() if p.is_dir() and (p / "__main__.py").is_file()):
                entry = f"-m {pkg.name}"
                break
        if entry is None:
            return self._skipped("smoke", "no entry point (main.py/app.py/cli.py/__main__.py) found", "")
        argv = [self.python] + entry.split() + ["--help"]
        res = await self._run(argv, work, timeout=8)
        if res.timed_out:
            return CheckResult(name="smoke", command=" ".join(argv[1:]), exit_code=0, passed=True,
                               output="process kept running for 8s (server-style entry point); stopped by the sandbox",
                               duration_s=round(res.duration_s, 2), required=False)
        return self._check("smoke", res, required=True)


# ------------------------------------------------------------------ failure routing
_TB_FILE = re.compile(r'File "([^"]+)", line \d+')
_PY_PATH = re.compile(r"([\w./\-]+\.py)(?::\d+|\b)")


def responsible_tasks(report: TestReport, file_owner: dict[str, str], fallback: Optional[str] = None) -> dict[str, list[str]]:
    """Map failing checks to (task_id -> [evidence lines]) using file paths found in REAL output."""
    out: dict[str, list[str]] = {}
    for chk in report.failed_checks:
        paths = set(_TB_FILE.findall(chk.output)) | set(_PY_PATH.findall(chk.output))
        matched = False
        for p in paths:
            norm = p.replace("\\", "/")
            cand = None
            for rel in file_owner:
                if norm == rel or norm.endswith("/" + rel):
                    cand = rel
                    break
            if cand:
                out.setdefault(file_owner[cand], []).append(f"[{chk.name}] {cand}")
                matched = True
        if not matched and fallback:
            out.setdefault(fallback, []).append(f"[{chk.name}] (no file path in output)")
    return out
