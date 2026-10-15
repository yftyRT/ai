"""Sandboxed execution of UNTRUSTED generated code.

Generated code never runs inside the bot/orchestrator process. Every command goes through
`CommandPolicy` (validation) and `Sandbox.run` (isolation):

* scrubbed environment: no Telegram token, model key, GitHub token or any deployment secret
* working directory limited to the project's `integrated/` copy (copied to a throw-away dir first)
* wall-clock timeout, kill of the whole process group, output size cap
* rlimits: address space, CPU seconds, file size, open files, processes
* isolation backends, strongest available is used automatically and reported by `capabilities()`:
    docker  -> --network none, read-only root, memory/cpu/pids limits, no host mounts except project
    bwrap   -> user/pid/net/ipc namespaces, read-only system dirs, only the project dir writable
    unshare -> new network namespace (no network) + rlimits
    plain   -> rlimits + scrubbed env + cwd only (NO network/filesystem isolation; reported honestly)
"""
from __future__ import annotations

import asyncio
import os
import re
import resource
import shlex
import shutil
import signal
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

ALLOWED_EXECUTABLES = {"python", "python3", "pytest", "ruff", "mypy", "node", "npm", "pip", "pip3", "bandit"}
_DENY_PATTERNS = [
    (re.compile(r"(^|[\s;&|])sudo(\s|$)"), "sudo is not allowed"),
    (re.compile(r"\brm\s+-[a-z]*r[a-z]*f?\s+(/|~|\$HOME|\*)"), "destructive rm"),
    (re.compile(r"(curl|wget)\s+[^|]*\|\s*(ba|z)?sh"), "pipe-to-shell is not allowed"),
    (re.compile(r"/var/run/docker\.sock|--privileged|\bnsenter\b|\bchroot\b"), "container escape primitive"),
    (re.compile(r"(\.ssh|\.aws|\.git-credentials|\.netrc|/etc/shadow|/proc/\d+/environ|/proc/self/environ)"), "access to credential locations"),
    (re.compile(r"\bmkfs\b|\bdd\s+if=|>\s*/dev/sd"), "disk-level command"),
]
_SHELL_META = re.compile(r"[;&|`$<>]|\$\(")


@dataclass
class PolicyDecision:
    allowed: bool
    reason: str = ""
    needs_approval: bool = False
    argv: list[str] = field(default_factory=list)


class CommandPolicy:
    """Validates shell commands / tool calls BEFORE execution. Commands are parsed to argv and run
    without a shell, so metacharacters can never chain extra commands."""

    def __init__(self, allow_install: bool = False):
        self.allow_install = allow_install

    def validate(self, command: str | list[str]) -> PolicyDecision:
        if isinstance(command, str):
            if _SHELL_META.search(command):
                return PolicyDecision(False, "shell metacharacters (; & | ` $ < >) are not allowed")
            try:
                argv = shlex.split(command)
            except ValueError as exc:
                return PolicyDecision(False, f"unparseable command: {exc}")
            text = command
        else:
            argv = list(command)
            text = " ".join(argv)
        if not argv:
            return PolicyDecision(False, "empty command")
        for rx, why in _DENY_PATTERNS:
            if rx.search(text):
                return PolicyDecision(False, why)
        exe = os.path.basename(argv[0])
        if exe not in ALLOWED_EXECUTABLES and argv[0] != sys.executable:
            return PolicyDecision(False, f"executable {exe!r} is not on the allow-list")
        if exe in {"pip", "pip3"} or (exe.startswith("python") and "pip" in argv[1:3]) or (exe == "npm" and len(argv) > 1 and argv[1] in {"install", "i", "ci", "add"}):
            if not self.allow_install:
                return PolicyDecision(False, "installing packages requires explicit administrator approval", needs_approval=True)
        for a in argv[1:]:
            if a.startswith("/") and not a.startswith(("/tmp", "/usr", sys.prefix)):
                return PolicyDecision(False, f"absolute path argument outside the sandbox: {a}")
            if ".." in Path(a).parts:
                return PolicyDecision(False, f"path traversal in argument: {a}")
        return PolicyDecision(True, argv=argv)


@dataclass
class CommandResult:
    argv: list[str]
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    timed_out: bool = False
    backend: str = "plain"

    @property
    def output(self) -> str:
        return (self.stdout + ("\n" + self.stderr if self.stderr else "")).strip()


def _limit_setter(memory_mb: int, cpu_s: int):
    def _set() -> None:
        os.setsid()
        for res, val in (
            (resource.RLIMIT_AS, memory_mb * 1024 * 1024),
            (resource.RLIMIT_CPU, cpu_s),
            (resource.RLIMIT_FSIZE, 200 * 1024 * 1024),
            (resource.RLIMIT_NOFILE, 512),
            (resource.RLIMIT_NPROC, 512),
            (resource.RLIMIT_CORE, 0),
        ):
            try:
                resource.setrlimit(res, (val, val))
            except (ValueError, OSError):
                pass

    return _set


class Sandbox:
    def __init__(
        self,
        *,
        backend: str = "subprocess",
        memory_mb: int = 2048,
        timeout_s: float = 300.0,
        allow_network: bool = False,
        docker_image: str = "python:3.12-slim",
        output_limit: int = 20000,
        auto_detect: bool = True,
    ):
        self.requested = backend
        self.memory_mb = memory_mb
        self.timeout_s = timeout_s
        self.allow_network = allow_network
        self.docker_image = docker_image
        self.output_limit = output_limit
        self.policy = CommandPolicy()
        self._chosen: Optional[str] = None
        self._auto_detect = auto_detect

    # ---------------------------------------------------------- capabilities
    def _probe(self, argv: list[str]) -> bool:
        import subprocess

        try:
            r = subprocess.run(argv, capture_output=True, timeout=15)
            return r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def backend_name(self) -> str:
        if self._chosen is not None:
            return self._chosen
        choice = "plain"
        if not self._auto_detect:
            self._chosen = choice
            return choice
        if self.requested == "docker" and shutil.which("docker") and self._probe(["docker", "info"]):
            choice = "docker"
        elif shutil.which("bwrap") and self._probe(["bwrap", "--unshare-all", "--ro-bind", "/usr", "/usr", "--dev", "/dev", "--proc", "/proc", "true"]):
            choice = "bwrap"
        elif shutil.which("unshare") and self._probe(["unshare", "--net", "--map-root-user", "true"]):
            choice = "unshare"
        self._chosen = choice
        return choice

    def capabilities(self) -> dict:
        b = self.backend_name()
        return {
            "backend": b,
            "network_isolated": (b in {"docker", "bwrap", "unshare"}) and not self.allow_network,
            "filesystem_restricted": b in {"docker", "bwrap"},
            "resource_limits": True,
            "env_scrubbed": True,
        }

    # ------------------------------------------------------------------- run
    @staticmethod
    def clean_env(cwd: Path) -> dict[str, str]:
        tmp = cwd / ".sandbox_tmp"
        tmp.mkdir(exist_ok=True)
        return {
            "PATH": "/usr/local/bin:/usr/bin:/bin:" + os.path.expanduser("~/.local/bin"),
            "HOME": str(tmp),
            "TMPDIR": str(tmp),
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(cwd),
            "PYTHONUNBUFFERED": "1",
            "NO_COLOR": "1",
        }

    def _wrap(self, argv: list[str], cwd: Path, env: dict[str, str]) -> list[str]:
        b = self.backend_name()
        if b == "unshare" and not self.allow_network:
            return ["unshare", "--net", "--map-root-user", *argv]
        if b == "bwrap":
            args = ["bwrap", "--unshare-all", "--die-with-parent", "--new-session"]
            if self.allow_network:
                args.append("--share-net")
            for d in ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc/alternatives", "/etc/ssl", sys.prefix):
                if os.path.exists(d):
                    args += ["--ro-bind", d, d]
            args += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--bind", str(cwd), str(cwd), "--chdir", str(cwd)]
            for k, v in env.items():
                args += ["--setenv", k, v]
            return args + argv
        if b == "docker":
            args = [
                "docker", "run", "--rm", "--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--pids-limit", "256",
                "--memory", f"{self.memory_mb}m", "--cpus", "2",
                "-v", f"{cwd}:/work", "-w", "/work",
            ]
            if not self.allow_network:
                args += ["--network", "none"]
            for k in ("PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED", "LANG"):
                args += ["-e", f"{k}={env[k]}"]
            args += ["-e", "HOME=/tmp", "-e", "PYTHONPATH=/work"]
            return args + [self.docker_image] + [("python" if a == sys.executable else a) for a in argv]
        return argv

    async def run(
        self,
        command: str | list[str],
        cwd: Path,
        *,
        timeout_s: Optional[float] = None,
        validate: bool = True,
    ) -> CommandResult:
        if validate:
            decision = self.policy.validate(command)
            if not decision.allowed:
                return CommandResult(
                    argv=[command] if isinstance(command, str) else list(command),
                    exit_code=126,
                    stderr=f"command rejected by sandbox policy: {decision.reason}",
                    backend="policy",
                )
            argv = decision.argv
        else:
            argv = shlex.split(command) if isinstance(command, str) else list(command)
        timeout = timeout_s or self.timeout_s
        cwd = Path(cwd).resolve()
        env = self.clean_env(cwd)
        full = self._wrap(argv, cwd, env)
        use_preexec = self.backend_name() in {"plain", "unshare"}
        start = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *full,
            cwd=str(cwd),
            env=env if self.backend_name() != "docker" else {"PATH": env["PATH"]},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=_limit_setter(self.memory_mb, int(timeout) + 30) if use_preexec else None,
            start_new_session=not use_preexec,
        )
        timed_out = False
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            self._kill(proc)
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=5)
            except asyncio.TimeoutError:
                out, err = b"", b""
        except asyncio.CancelledError:
            self._kill(proc)
            raise
        dur = time.monotonic() - start

        def _dec(b: bytes) -> str:
            t = b.decode("utf-8", "replace")
            return t if len(t) <= self.output_limit else t[: self.output_limit // 2] + "\n...[output truncated]...\n" + t[-self.output_limit // 2 :]

        return CommandResult(
            argv=argv,
            exit_code=-9 if timed_out else (proc.returncode if proc.returncode is not None else -1),
            stdout=_dec(out),
            stderr=_dec(err),
            duration_s=dur,
            timed_out=timed_out,
            backend=self.backend_name(),
        )

    @staticmethod
    def _kill(proc: asyncio.subprocess.Process) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    # ------------------------------------------------------- scratch copies
    @staticmethod
    def make_scratch(src: Path) -> Path:
        """Copy the project into a throw-away dir so test runs can't modify the integrated tree."""
        scratch = Path(tempfile.mkdtemp(prefix="sbx_"))
        shutil.copytree(src, scratch / "project", ignore=shutil.ignore_patterns("__pycache__", ".sandbox_tmp", ".git"))
        return scratch / "project"

    @staticmethod
    def cleanup_scratch(project_copy: Path) -> None:
        shutil.rmtree(project_copy.parent, ignore_errors=True)
