"""Shared test helpers. Tests are plain sync functions using asyncio.run, so they run under pytest
without needing pytest-asyncio."""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from app.bot.messenger import RecordingMessenger
from app.config import Settings, load_settings
from app.container import build_app
from app.providers.fake import FakeProvider
from app.storage.sqlite import SQLiteStorage

SKILLS_SRC = Path(__file__).resolve().parent.parent / "app" / "skills"
OWNER = 111
ADMIN = 111
OTHER = 222


def run(coro):
    return asyncio.run(coro)


def make_settings(tmp_path: Path, **over: str) -> Settings:
    env = {
        "TELEGRAM_BOT_TOKEN": "123456789:AAFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE",
        "AUTHORIZED_USER_IDS": f"{OWNER},{OTHER}",
        "ADMIN_USER_IDS": str(ADMIN),
        "PROVIDER": "fake",
        "MODEL_API_KEY": "test-model-key-0000",
        "DB_PATH": str(tmp_path / "db" / "factory.db"),
        "WORKSPACE_DIR": str(tmp_path / "ws"),
        "ARTIFACTS_DIR": str(tmp_path / "art"),
        "SKILLS_DIR": str(tmp_path / "skills"),
        "BACKOFF_BASE_S": "0.01",
        "BACKOFF_CAP_S": "0.05",
        "SANDBOX_TIMEOUT_S": "60",
        "PROGRESS_MIN_INTERVAL_S": "0",
    }
    env.update(over)
    return load_settings(env)


def copy_skills(tmp_path: Path) -> None:
    dst = tmp_path / "skills"
    if not dst.exists():
        shutil.copytree(SKILLS_SRC / "builtin", dst / "builtin")
        (dst / "imported").mkdir(parents=True, exist_ok=True)


async def make_app(tmp_path: Path, provider: FakeProvider | None = None, **over: str) -> tuple[Any, RecordingMessenger]:
    copy_skills(tmp_path)
    settings = make_settings(tmp_path, **over)
    msgr = RecordingMessenger()
    app = build_app(settings, msgr, provider=provider or FakeProvider())
    await app.storage.init()
    return app, msgr


async def wait_final(app, project_id: str, timeout: float = 120.0) -> Any:
    await app.orchestrator.wait(project_id, timeout)
    return await app.storage.get_project(project_id)


async def settle(app, project_id: str, timeout: float = 120.0) -> Any:
    """Wait until planning finished, then until any run finished."""
    for _ in range(int(timeout * 20)):
        run_task = app.orchestrator._runs.get(project_id)
        if run_task is None or run_task.done():
            break
        await asyncio.sleep(0.05)
    return await app.storage.get_project(project_id)
