from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from account_store import AccountStore
from maochao_rpa import TASKS, load_settings


BASE = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE / "config.local.json"
DB_PATH = BASE / "backend" / "rpa.db"
WORK_DIR = BASE / "backend"


@dataclass
class RunRow:
    run_id: str
    task_keys: list[str]
    account_keys: list[str]
    status: str
    created_at: str
    updated_at: str
    started_at: str = ""
    finished_at: str = ""
    force_account_tasks: bool = False
    headed: bool = True
    error: str = ""
    result_json: str = "[]"


class BackendStore:
    def __init__(self, config_path: Path = DEFAULT_CONFIG_PATH):
        self.config_path = config_path
        self.settings = load_settings(config_path)
        self.account_store = AccountStore(self.settings.accounts_db_path, self.settings.accounts_db_key_path)
        self.lock = threading.Lock()
        self._ensure_dirs()
        self._ensure_schema()

    def _ensure_dirs(self) -> None:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        self.settings.log_dir.mkdir(parents=True, exist_ok=True)
        self.settings.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self.settings.data_root.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=3000")
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    task_keys_json TEXT NOT NULL,
                    account_keys_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT '',
                    force_account_tasks INTEGER NOT NULL DEFAULT 0,
                    headed INTEGER NOT NULL DEFAULT 1,
                    error TEXT NOT NULL DEFAULT '',
                    result_json TEXT NOT NULL DEFAULT '[]'
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS account_locks (
                    account_key TEXT PRIMARY KEY,
                    port INTEGER NOT NULL,
                    profile_dir TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    acquired_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_account_locks_port ON account_locks(port)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_account_locks_profile ON account_locks(profile_dir)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    account_key TEXT NOT NULL,
                    task_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )

    def _now(self) -> str:
        return datetime.now().isoformat(timespec="seconds")

    def list_tasks(self) -> list[dict[str, Any]]:
        return [
            {"task_key": key, "title": value["title"], "file_task_text": value["file_task_text"]}
            for key, value in TASKS.items()
        ]

    def list_accounts(self, include_disabled: bool = False) -> list[dict[str, Any]]:
        return self.account_store.list_accounts(include_disabled=include_disabled)

    def create_run(self, task_keys: list[str], account_keys: list[str], force_account_tasks: bool, headed: bool) -> dict[str, Any]:
        run_id = str(uuid.uuid4())
        row = RunRow(
            run_id=run_id,
            task_keys=task_keys,
            account_keys=account_keys,
            status="pending",
            created_at=self._now(),
            updated_at=self._now(),
            force_account_tasks=force_account_tasks,
            headed=headed,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, task_keys_json, account_keys_json, status, created_at, updated_at,
                    started_at, finished_at, force_account_tasks, headed, error, result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.run_id,
                    json.dumps(row.task_keys, ensure_ascii=False),
                    json.dumps(row.account_keys, ensure_ascii=False),
                    row.status,
                    row.created_at,
                    row.updated_at,
                    row.started_at,
                    row.finished_at,
                    1 if row.force_account_tasks else 0,
                    1 if row.headed else 0,
                    row.error,
                    row.result_json,
                ),
            )
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            return self._run_row(row)

    def list_runs(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM runs ORDER BY created_at DESC").fetchall()
            return [self._run_row(row) for row in rows]

    def update_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
        error: str | None = None,
        result_json: str | None = None,
    ) -> None:
        fields: list[str] = ["updated_at = ?"]
        values: list[Any] = [self._now()]
        if status is not None:
            fields.append("status = ?")
            values.append(status)
        if started_at is not None:
            fields.append("started_at = ?")
            values.append(started_at)
        if finished_at is not None:
            fields.append("finished_at = ?")
            values.append(finished_at)
        if error is not None:
            fields.append("error = ?")
            values.append(error)
        if result_json is not None:
            fields.append("result_json = ?")
            values.append(result_json)
        values.append(run_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE runs SET {', '.join(fields)} WHERE run_id = ?", values)

    def _run_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "run_id": row["run_id"],
            "task_keys": json.loads(row["task_keys_json"] or "[]"),
            "account_keys": json.loads(row["account_keys_json"] or "[]"),
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "force_account_tasks": bool(row["force_account_tasks"]),
            "headed": bool(row["headed"]),
            "error": row["error"],
            "result": json.loads(row["result_json"] or "[]"),
        }

    def lock_accounts(self, run_id: str, accounts: list[dict[str, Any]]) -> None:
        with self.lock, self._connect() as conn:
            for account in accounts:
                existing = conn.execute(
                    """
                    SELECT account_key, port, profile_dir, run_id
                    FROM account_locks
                    WHERE account_key = ? OR port = ? OR profile_dir = ?
                    """,
                    (account["key"], int(account["port"]), account["profile_dir"]),
                ).fetchone()
                if existing and existing["run_id"] != run_id:
                    raise RuntimeError(
                        "浏览器资源已被占用: "
                        f"account={existing['account_key']} port={existing['port']} profile={existing['profile_dir']}"
                    )
            now = self._now()
            for account in accounts:
                conn.execute(
                    """
                    INSERT INTO account_locks (account_key, port, profile_dir, run_id, acquired_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(account_key) DO UPDATE SET
                        port = excluded.port,
                        profile_dir = excluded.profile_dir,
                        run_id = excluded.run_id,
                        acquired_at = excluded.acquired_at
                    """,
                    (
                        account["key"],
                        int(account["port"]),
                        account["profile_dir"],
                        run_id,
                        now,
                    ),
                )

    def unlock_accounts(self, run_id: str) -> None:
        with self.lock, self._connect() as conn:
            conn.execute("DELETE FROM account_locks WHERE run_id = ?", (run_id,))

    def add_task_event(self, run_id: str, account_key: str, task_key: str, status: str, message: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO task_events (run_id, account_key, task_key, status, message, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, account_key, task_key, status, message, self._now()),
            )

    def list_task_events(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT run_id, account_key, task_key, status, message, created_at FROM task_events WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_files(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in self.settings.data_root.rglob("*"):
            if not path.is_file():
                continue
            rows.append(
                {
                    "file_id": str(path.relative_to(self.settings.data_root)),
                    "name": path.name,
                    "path": str(path),
                    "size": path.stat().st_size,
                    "updated_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
                }
            )
        rows.sort(key=lambda item: item["updated_at"], reverse=True)
        return rows

    def list_screenshots(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in self.settings.screenshot_dir.rglob("*.png"):
            if not path.is_file():
                continue
            rows.append(
                {
                    "screenshot_id": str(path.relative_to(self.settings.screenshot_dir)),
                    "name": path.name,
                    "path": str(path),
                    "size": path.stat().st_size,
                    "updated_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
                }
            )
        rows.sort(key=lambda item: item["updated_at"], reverse=True)
        return rows
