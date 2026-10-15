"""Central configuration: environment parsing + validation.

All limits are configurable via environment variables (see .env.example).
Secrets are held in `Settings` but never included in `Settings.public_dict()`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

ROOT_DIR = Path(__file__).resolve().parent.parent


class ConfigError(ValueError):
    """Invalid configuration. Messages never contain secret values."""


def _parse_ids(raw: str | None) -> frozenset[int]:
    out: set[int] = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if not part.lstrip("-").isdigit():
            raise ValueError("user id lists must contain integers only")
        out.add(int(part))
    return frozenset(out)


def _is_private_host(host: str) -> bool:
    if host in {"localhost", "::1"} or host.startswith(("127.", "10.", "192.168.")):
        return True
    if host.startswith("172."):
        try:
            return 16 <= int(host.split(".")[1]) <= 31
        except (IndexError, ValueError):
            return False
    return host.endswith((".internal", ".local"))


class Settings(BaseModel):
    # --- Telegram ---------------------------------------------------------
    telegram_bot_token: str = Field(default="", repr=False)
    authorized_user_ids: frozenset[int] = frozenset()
    admin_user_ids: frozenset[int] = frozenset()
    telegram_max_file_mb: int = 49
    progress_min_interval_s: float = 5.0

    # --- model server (self-hosted, OpenAI compatible) --------------------
    provider: str = "openai"  # openai (vLLM/SGLang endpoint) | fake (tests only)
    model_base_url: str = "http://127.0.0.1:8000/v1"
    model_api_key: str = Field(default="", repr=False)
    model_id: str = "Qwen/Qwen3.6-27B"
    max_context_tokens: int = 131072
    max_output_tokens: int = 8192
    request_timeout_s: float = 900.0
    allow_remote_model: bool = False

    # --- scheduler limits -------------------------------------------------
    max_concurrent_model_requests: int = 4
    max_active_agents: int = 3
    max_task_seconds: int = 1800
    max_retries: int = 2
    max_project_seconds: int = 4 * 3600
    max_repair_cycles: int = 3
    max_tasks_per_plan: int = 12
    backoff_base_s: float = 2.0
    backoff_cap_s: float = 60.0

    # --- budget -----------------------------------------------------------
    gpu_budget_usd_per_hour: float = 2.0  # HARD cap for the always-on Pod
    gpu_hourly_usd: float = 1.79  # price of the Pod you actually selected (+storage if you add it)
    project_budget_usd: float = 10.0
    token_price_in_per_m: float = 0.0  # self-hosted: tokens are free, the Pod is the cost
    token_price_out_per_m: float = 0.0
    runpod_pod_id: str = ""

    # --- paths ------------------------------------------------------------
    db_path: Path = ROOT_DIR / "data" / "factory.db"
    workspace_dir: Path = ROOT_DIR / "data" / "workspace"
    artifacts_dir: Path = ROOT_DIR / "data" / "artifacts"
    skills_dir: Path = ROOT_DIR / "app" / "skills"

    # --- sandbox ----------------------------------------------------------
    sandbox_backend: str = "auto"  # auto | docker | subprocess
    sandbox_timeout_s: float = 300.0
    sandbox_memory_mb: int = 2048
    sandbox_allow_network: bool = False
    sandbox_docker_image: str = "python:3.12-slim"

    # --- skills / GitHub ---------------------------------------------------
    github_token: str = Field(default="", repr=False)
    github_api_url: str = "https://api.github.com"
    skill_trust_policy: str = "manual"  # manual | auto_strict
    skill_min_score: float = 55.0
    skill_auto_min_score: float = 80.0
    allow_copyleft_skills: bool = False
    skills_auto_discover: bool = False  # search GitHub when the registry has no suitable skill

    health_port: int = 8080
    log_level: str = "INFO"

    # ------------------------------------------------------------ validators
    @field_validator("provider")
    @classmethod
    def _provider(cls, v: str) -> str:
        if v not in {"openai", "fake"}:
            raise ValueError("PROVIDER must be 'openai' or 'fake'")
        return v

    @field_validator("sandbox_backend")
    @classmethod
    def _sandbox(cls, v: str) -> str:
        if v not in {"auto", "docker", "subprocess"}:
            raise ValueError("SANDBOX_BACKEND must be auto, docker or subprocess")
        return v

    @field_validator("skill_trust_policy")
    @classmethod
    def _policy(cls, v: str) -> str:
        if v not in {"manual", "auto_strict"}:
            raise ValueError("SKILL_TRUST_POLICY must be 'manual' or 'auto_strict'")
        return v

    @model_validator(mode="after")
    def _cross_checks(self) -> "Settings":
        problems: list[str] = []
        if self.gpu_hourly_usd <= 0:
            problems.append("GPU_HOURLY_USD must be > 0")
        if self.gpu_hourly_usd > self.gpu_budget_usd_per_hour + 1e-9:
            problems.append(
                f"GPU_HOURLY_USD ${self.gpu_hourly_usd:.2f} exceeds the hard budget "
                f"GPU_BUDGET_USD_PER_HOUR ${self.gpu_budget_usd_per_hour:.2f}: pick a cheaper Pod"
            )
        for name in ("max_active_agents", "max_concurrent_model_requests", "max_task_seconds",
                     "max_project_seconds", "max_output_tokens", "max_context_tokens", "max_tasks_per_plan"):
            if getattr(self, name) < 1:
                problems.append(f"{name.upper()} must be >= 1")
        if self.max_retries < 0 or self.max_repair_cycles < 0:
            problems.append("MAX_RETRIES and MAX_REPAIR_CYCLES must be >= 0")
        if self.max_active_agents > 16:
            problems.append("MAX_ACTIVE_AGENTS above 16 is not supported")
        if self.max_output_tokens >= self.max_context_tokens:
            problems.append("MAX_OUTPUT_TOKENS must be smaller than MAX_CONTEXT_TOKENS")
        if self.project_budget_usd <= 0:
            problems.append("PROJECT_BUDGET_USD must be > 0")
        if self.backoff_base_s <= 0 or self.backoff_cap_s < self.backoff_base_s:
            problems.append("BACKOFF_CAP_S must be >= BACKOFF_BASE_S > 0")
        if not 0 <= self.skill_min_score <= 100 or not 0 <= self.skill_auto_min_score <= 100:
            problems.append("skill scores must be within 0..100")
        if self.provider == "openai":
            u = urlparse(self.model_base_url)
            if u.scheme not in {"http", "https"} or not u.hostname:
                problems.append("MODEL_BASE_URL must be an http(s) URL")
            elif not self.allow_remote_model and not _is_private_host(u.hostname):
                problems.append("MODEL_BASE_URL is not a private host: the model must be self-hosted "
                                "(set ALLOW_REMOTE_MODEL=true only if you really intend that)")
        if not self.admin_user_ids:
            # single-owner convenience: every authorized user is an admin unless ADMIN_USER_IDS narrows it
            object.__setattr__(self, "admin_user_ids", self.authorized_user_ids)
        elif not self.admin_user_ids <= self.authorized_user_ids:
            problems.append("every ADMIN_USER_IDS entry must also be in AUTHORIZED_USER_IDS")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    # ------------------------------------------------------------- helpers
    def validate_for_production(self) -> list[str]:
        """Problems that must block a real deployment (tests may use fake providers)."""
        out: list[str] = []
        if not self.telegram_bot_token or self.telegram_bot_token.lower() in {"changeme", "change_me"}:
            out.append("TELEGRAM_BOT_TOKEN is not set")
        if not self.authorized_user_ids:
            out.append("AUTHORIZED_USER_IDS is empty: nobody could use the bot")
        if self.provider == "openai" and (not self.model_api_key or self.model_api_key.lower() in {"changeme", "change_me"}):
            out.append("MODEL_API_KEY is not set (the model endpoint must be authenticated)")
        if self.provider == "fake":
            out.append("PROVIDER=fake is for tests only")
        return out

    def public_dict(self) -> dict:
        """Safe-to-display configuration (no secrets)."""
        d = self.model_dump(exclude={"telegram_bot_token", "model_api_key", "github_token"})
        for k, v in list(d.items()):
            if isinstance(v, Path):
                d[k] = str(v)
            elif isinstance(v, frozenset):
                d[k] = sorted(v)
        d["github_authenticated"] = bool(self.github_token)
        return d

    def secret_values(self) -> list[str]:
        return [s for s in (self.telegram_bot_token, self.model_api_key, self.github_token) if s]


_ENV_MAP: dict[str, str] = {
    "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
    "TELEGRAM_MAX_FILE_MB": "telegram_max_file_mb",
    "PROGRESS_MIN_INTERVAL_S": "progress_min_interval_s",
    "PROVIDER": "provider",
    "MODEL_BASE_URL": "model_base_url",
    "MODEL_API_KEY": "model_api_key",
    "MODEL_ID": "model_id",
    "MAX_CONTEXT_TOKENS": "max_context_tokens",
    "MAX_OUTPUT_TOKENS": "max_output_tokens",
    "REQUEST_TIMEOUT_S": "request_timeout_s",
    "ALLOW_REMOTE_MODEL": "allow_remote_model",
    "MAX_CONCURRENT_MODEL_REQUESTS": "max_concurrent_model_requests",
    "MAX_ACTIVE_AGENTS": "max_active_agents",
    "MAX_TASK_SECONDS": "max_task_seconds",
    "MAX_RETRIES": "max_retries",
    "MAX_PROJECT_SECONDS": "max_project_seconds",
    "MAX_REPAIR_CYCLES": "max_repair_cycles",
    "MAX_TASKS_PER_PLAN": "max_tasks_per_plan",
    "BACKOFF_BASE_S": "backoff_base_s",
    "BACKOFF_CAP_S": "backoff_cap_s",
    "GPU_BUDGET_USD_PER_HOUR": "gpu_budget_usd_per_hour",
    "GPU_HOURLY_USD": "gpu_hourly_usd",
    "PROJECT_BUDGET_USD": "project_budget_usd",
    "TOKEN_PRICE_IN_PER_M": "token_price_in_per_m",
    "TOKEN_PRICE_OUT_PER_M": "token_price_out_per_m",
    "RUNPOD_POD_ID": "runpod_pod_id",
    "DB_PATH": "db_path",
    "WORKSPACE_DIR": "workspace_dir",
    "ARTIFACTS_DIR": "artifacts_dir",
    "SKILLS_DIR": "skills_dir",
    "SANDBOX_BACKEND": "sandbox_backend",
    "SANDBOX_TIMEOUT_S": "sandbox_timeout_s",
    "SANDBOX_MEMORY_MB": "sandbox_memory_mb",
    "SANDBOX_ALLOW_NETWORK": "sandbox_allow_network",
    "SANDBOX_DOCKER_IMAGE": "sandbox_docker_image",
    "GITHUB_TOKEN": "github_token",
    "GITHUB_API_URL": "github_api_url",
    "SKILL_TRUST_POLICY": "skill_trust_policy",
    "SKILL_MIN_SCORE": "skill_min_score",
    "SKILL_AUTO_MIN_SCORE": "skill_auto_min_score",
    "ALLOW_COPYLEFT_SKILLS": "allow_copyleft_skills",
    "SKILLS_AUTO_DISCOVER": "skills_auto_discover",
    "HEALTH_PORT": "health_port",
    "LOG_LEVEL": "log_level",
}


def load_settings(env: Optional[Mapping[str, str]] = None) -> Settings:
    import os

    e = os.environ if env is None else env
    data: dict = {}
    for key, field in _ENV_MAP.items():
        if key in e and e[key] != "":
            data[field] = e[key]
    try:
        if "AUTHORIZED_USER_IDS" in e:
            data["authorized_user_ids"] = _parse_ids(e["AUTHORIZED_USER_IDS"])
        if "ADMIN_USER_IDS" in e:
            data["admin_user_ids"] = _parse_ids(e["ADMIN_USER_IDS"])
        return Settings(**data)
    except ValidationError as exc:
        # field names + reasons only: never echo input values (they may be secrets)
        lines = [f"{'.'.join(str(p) for p in err['loc']).upper()}: {err['msg']}" for err in exc.errors()]
        raise ConfigError("invalid configuration:\n  " + "\n  ".join(lines)) from None
    except ValueError as exc:
        raise ConfigError(f"invalid configuration: {exc}") from None
