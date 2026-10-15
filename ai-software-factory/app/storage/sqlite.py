"""SQLite storage backend (WAL mode). Blocking sqlite3 calls run in a worker thread
guarded by a lock so the event loop (and therefore the Telegram bot) never blocks."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from app.schemas.models import Project, Task

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(
  id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, state TEXT NOT NULL,
  updated_at REAL NOT NULL, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_projects_owner ON projects(owner_id, updated_at);
CREATE TABLE IF NOT EXISTS tasks(
  project_id TEXT NOT NULL, task_id TEXT NOT NULL, state TEXT NOT NULL, value TEXT NOT NULL,
  seq INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(project_id, task_id));
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, ts REAL NOT NULL,
  level TEXT NOT NULL, message TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_events_project ON events(project_id, id);
CREATE TABLE IF NOT EXISTS usage(
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, ts REAL NOT NULL,
  prompt_tokens INTEGER NOT NULL, completion_tokens INTEGER NOT NULL, cost_usd REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_usage_project ON usage(project_id);
CREATE TABLE IF NOT EXISTS docs(
  collection TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, updated_at REAL NOT NULL,
  PRIMARY KEY(collection, key));
"""


class SQLiteStorage:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()

    async def init(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)

        def _open() -> None:
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.executescript(SCHEMA)
            conn.commit()
            self._conn = conn

        await asyncio.to_thread(_open)
        if self.path != ":memory:":
            try:
                Path(self.path).chmod(0o600)
            except OSError:
                pass

    async def close(self) -> None:
        def _close() -> None:
            with self._lock:
                if self._conn is not None:
                    self._conn.commit()
                    self._conn.close()
                    self._conn = None

        await asyncio.to_thread(_close)

    async def _run(self, sql: str, params: Any = (), *, many: bool = False, fetch: Optional[str] = None):
        def _do():
            if self._conn is None:
                raise RuntimeError("storage is not initialised")
            with self._lock:
                cur = self._conn.cursor()
                if many:
                    cur.executemany(sql, params)
                else:
                    cur.execute(sql, params)
                rows = cur.fetchall() if fetch == "all" else (cur.fetchone() if fetch == "one" else None)
                self._conn.commit()
                return rows

        return await asyncio.to_thread(_do)

    # ----------------------------------------------------------------- projects
    async def save_project(self, project: Project) -> None:
        project.updated_at = time.time()
        await self._run(
            "INSERT INTO projects(id, owner_id, state, updated_at, value) VALUES (?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at, value=excluded.value",
            (project.id, project.owner_id, project.state.value, project.updated_at, project.model_dump_json()),
        )

    async def get_project(self, project_id: str) -> Optional[Project]:
        row = await self._run("SELECT value FROM projects WHERE id=?", (project_id,), fetch="one")
        return Project.model_validate_json(row["value"]) if row else None

    async def list_projects(self, owner_id: Optional[int] = None, limit: int = 50) -> list[Project]:
        if owner_id is None:
            rows = await self._run("SELECT value FROM projects ORDER BY updated_at DESC LIMIT ?", (limit,), fetch="all")
        else:
            rows = await self._run(
                "SELECT value FROM projects WHERE owner_id=? ORDER BY updated_at DESC LIMIT ?", (owner_id, limit), fetch="all")
        return [Project.model_validate_json(r["value"]) for r in rows]

    # -------------------------------------------------------------------- tasks
    async def save_task(self, project_id: str, task: Task) -> None:
        await self._run(
            "INSERT INTO tasks(project_id, task_id, state, value, seq) VALUES (?,?,?,?, "
            "COALESCE((SELECT seq FROM tasks WHERE project_id=? AND task_id=?), (SELECT COUNT(*) FROM tasks WHERE project_id=?))) "
            "ON CONFLICT(project_id, task_id) DO UPDATE SET state=excluded.state, value=excluded.value",
            (project_id, task.id, task.state.value, task.model_dump_json(), project_id, task.id, project_id),
        )

    async def save_tasks(self, project_id: str, tasks: list[Task]) -> None:
        for t in tasks:
            await self.save_task(project_id, t)

    async def get_tasks(self, project_id: str) -> list[Task]:
        rows = await self._run("SELECT value FROM tasks WHERE project_id=? ORDER BY seq", (project_id,), fetch="all")
        return [Task.model_validate_json(r["value"]) for r in rows]

    # ------------------------------------------------------------------- events
    async def add_event(self, project_id: str, level: str, message: str) -> None:
        await self._run(
            "INSERT INTO events(project_id, ts, level, message) VALUES (?,?,?,?)",
            (project_id, time.time(), level, message[:4000]),
        )

    async def get_events(self, project_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self._run(
            "SELECT ts, level, message FROM events WHERE project_id=? ORDER BY id DESC LIMIT ?",
            (project_id, limit), fetch="all")
        return [dict(r) for r in reversed(rows)]

    # -------------------------------------------------------------------- usage
    async def add_usage(self, project_id: str, prompt_tokens: int, completion_tokens: int, cost_usd: float) -> None:
        await self._run(
            "INSERT INTO usage(project_id, ts, prompt_tokens, completion_tokens, cost_usd) VALUES (?,?,?,?,?)",
            (project_id, time.time(), prompt_tokens, completion_tokens, cost_usd),
        )

    async def get_usage(self, project_id: str) -> dict[str, float]:
        row = await self._run(
            "SELECT COALESCE(SUM(prompt_tokens),0) p, COALESCE(SUM(completion_tokens),0) c, "
            "COALESCE(SUM(cost_usd),0) u, COUNT(*) n FROM usage WHERE project_id=?", (project_id,), fetch="one")
        return {"prompt_tokens": int(row["p"]), "completion_tokens": int(row["c"]),
                "cost_usd": float(row["u"]), "requests": int(row["n"])}

    # --------------------------------------------------------------------- docs
    async def put_doc(self, collection: str, key: str, value: dict[str, Any]) -> None:
        await self._run(
            "INSERT INTO docs(collection, key, value, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(collection, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (collection, key, json.dumps(value), time.time()),
        )

    async def get_doc(self, collection: str, key: str) -> Optional[dict[str, Any]]:
        row = await self._run("SELECT value FROM docs WHERE collection=? AND key=?", (collection, key), fetch="one")
        return json.loads(row["value"]) if row else None

    async def list_docs(self, collection: str) -> list[dict[str, Any]]:
        rows = await self._run("SELECT key, value FROM docs WHERE collection=? ORDER BY updated_at DESC", (collection,), fetch="all")
        out = []
        for r in rows:
            d = json.loads(r["value"])
            if isinstance(d, dict):
                d.setdefault("_key", r["key"])
            out.append(d)
        return out

    async def delete_doc(self, collection: str, key: str) -> None:
        await self._run("DELETE FROM docs WHERE collection=? AND key=?", (collection, key))
