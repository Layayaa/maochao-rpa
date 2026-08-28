from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import secrets
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from account_store import AccountStore
from maochao_rpa import TASKS, is_placeholder_supplier as _is_placeholder_supplier, load_settings


BASE = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE / "config.local.json"
DB_PATH = BASE / "backend" / "rpa.db"
WORK_DIR = BASE / "backend"
WORKER_HEARTBEAT_PATH = WORK_DIR / "worker_heartbeat.json"


SYNC_SUPPLIERS_TASK = "__sync_suppliers__"
RUN_KIND_TASKS = "tasks"
RUN_KIND_SYNC_SUPPLIERS = "sync_suppliers"
SCHEDULE_DEFAULT_WEEKDAYS = list(range(7))
DEFAULT_OPERATOR_PASSWORD = "123456"


def _hash_operator_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    iterations = 200_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def _verify_operator_password(password: str, encoded: str) -> bool:
    try:
        algorithm, raw_iterations, raw_salt, raw_digest = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(raw_iterations)
        salt = base64.b64decode(raw_salt.encode("ascii"), validate=True)
        expected = base64.b64decode(raw_digest.encode("ascii"), validate=True)
    except (binascii.Error, TypeError, ValueError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return secrets.compare_digest(actual, expected)


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
    queue_position: int = 0
    error: str = ""
    result_json: str = "[]"
    suppliers: list[dict[str, Any]] | None = None
    operator_id: str = ""
    operator_name: str = ""
    run_kind: str = RUN_KIND_TASKS
    assignment_snapshot: list[dict[str, Any]] | None = None
    retry_attempt: int = 0
    retry_parent_run_id: str = ""
    auto_retry_enqueued: bool = False


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
        try:
            self.settings.data_root.mkdir(parents=True, exist_ok=True)
        except OSError:
            # A UNC archive may only be reachable from the interactive desktop
            # session. Do not prevent API/Worker startup; archive writes still
            # fail explicitly if the share is unavailable at execution time.
            if not str(self.settings.data_root).startswith(("\\\\", "//")):
                raise

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
                    queue_position INTEGER NOT NULL DEFAULT 0,
                    pause_requested INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    result_json TEXT NOT NULL DEFAULT '[]'
                )
                """
            )
            self._ensure_column(conn, "runs", "queue_position", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "runs", "pause_requested", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "runs", "cancel_requested", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "runs", "suppliers_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(conn, "runs", "operator_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "runs", "operator_name", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "runs", "run_kind", "TEXT NOT NULL DEFAULT 'tasks'")
            self._ensure_column(conn, "runs", "assignment_snapshot_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(conn, "runs", "retry_attempt", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "runs", "retry_parent_run_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "runs", "auto_retry_enqueued", "INTEGER NOT NULL DEFAULT 0")
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS operators (
                    operator_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self._ensure_column(conn, "operators", "password_hash", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "operators", "updated_at", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "operators", "supply_chain_user_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "operators", "active", "INTEGER NOT NULL DEFAULT 1")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_operators_supply_owner "
                "ON operators(supply_chain_user_id, operator_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS supply_chain_users (
                    user_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS account_ownership (
                    account_key TEXT PRIMARY KEY,
                    creator_role TEXT NOT NULL DEFAULT 'admin',
                    creator_user_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS account_suppliers (
                    account_key TEXT NOT NULL,
                    supplier_id TEXT NOT NULL,
                    supplier_name TEXT NOT NULL,
                    visible INTEGER NOT NULL DEFAULT 1,
                    last_synced_at TEXT NOT NULL,
                    PRIMARY KEY (account_key, supplier_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS operator_suppliers (
                    operator_id TEXT NOT NULL,
                    account_key TEXT NOT NULL,
                    supplier_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (operator_id, account_key, supplier_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS run_file_ownership (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    account_key TEXT NOT NULL,
                    supplier_id TEXT NOT NULL,
                    supplier_name TEXT NOT NULL DEFAULT '',
                    task_key TEXT NOT NULL,
                    operator_id TEXT NOT NULL DEFAULT '',
                    operator_name TEXT NOT NULL DEFAULT '',
                    raw_file TEXT NOT NULL DEFAULT '',
                    cleaned_file TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_run_file_ownership_run ON run_file_ownership(run_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_operator_suppliers_supplier ON operator_suppliers(account_key, supplier_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_schedules (
                    task_key TEXT PRIMARY KEY,
                    operator_id TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 0,
                    time_of_day TEXT NOT NULL DEFAULT '09:00',
                    weekdays_json TEXT NOT NULL DEFAULT '[0,1,2,3,4,5,6]',
                    headed INTEGER NOT NULL DEFAULT 1,
                    last_enqueued_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schedule_alarms (
                    schedule_id TEXT PRIMARY KEY,
                    task_keys_json TEXT NOT NULL,
                    operator_id TEXT NOT NULL DEFAULT '',
                    operator_ids_json TEXT NOT NULL DEFAULT '[]',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    time_of_day TEXT NOT NULL DEFAULT '09:00',
                    weekdays_json TEXT NOT NULL DEFAULT '[0,1,2,3,4,5,6]',
                    headed INTEGER NOT NULL DEFAULT 1,
                    last_enqueued_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_schedule_alarms_operator ON schedule_alarms(operator_id)"
            )
            self._ensure_column(conn, "operator_suppliers", "active", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column(conn, "operator_suppliers", "updated_at", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "schedule_alarms", "operator_ids_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(conn, "schedule_alarms", "all_operators", "INTEGER NOT NULL DEFAULT 0")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS item_id_uploads (
                    upload_id TEXT PRIMARY KEY,
                    original_name TEXT NOT NULL,
                    uploaded_by_role TEXT NOT NULL,
                    uploaded_by_user_id TEXT NOT NULL DEFAULT '',
                    uploaded_at TEXT NOT NULL,
                    row_count INTEGER NOT NULL DEFAULT 0,
                    error_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'active',
                    errors_json TEXT NOT NULL DEFAULT '[]'
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS item_id_config (
                    account_key TEXT NOT NULL,
                    supplier_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    upload_id TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (account_key, supplier_id, item_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_item_id_config_lookup "
                "ON item_id_config(account_key, supplier_id, active)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS item_id_config_history (
                    upload_id TEXT NOT NULL,
                    account_key TEXT NOT NULL,
                    supplier_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (upload_id, account_key, supplier_id, item_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS item_id_config_scopes (
                    upload_id TEXT NOT NULL,
                    account_key TEXT NOT NULL,
                    supplier_id TEXT NOT NULL,
                    PRIMARY KEY (upload_id, account_key, supplier_id)
                )
                """
            )
            self._migrate_legacy_task_schedules(conn)
            self._migrate_schedule_operator_ids(conn)
            self._cleanup_stale_account_locks(conn)
            self._normalize_pending_queue(conn)

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _migrate_legacy_task_schedules(self, conn: sqlite3.Connection) -> None:
        if conn.execute("SELECT 1 FROM schedule_alarms LIMIT 1").fetchone() is not None:
            return
        rows = conn.execute(
            """
            SELECT task_key, operator_id, enabled, time_of_day, weekdays_json,
                   headed, last_enqueued_at, updated_at
            FROM task_schedules
            """
        ).fetchall()
        for row in rows:
            task_key = str(row["task_key"] or "")
            if task_key not in TASKS:
                continue
            created_at = str(row["updated_at"] or self._now())
            conn.execute(
                """
                INSERT INTO schedule_alarms (
                    schedule_id, task_keys_json, operator_id, operator_ids_json, enabled, time_of_day,
                    weekdays_json, headed, last_enqueued_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    json.dumps([task_key], ensure_ascii=False),
                    row["operator_id"],
                    json.dumps([row["operator_id"]] if row["operator_id"] else [], ensure_ascii=False),
                    row["enabled"],
                    row["time_of_day"],
                    row["weekdays_json"],
                    row["headed"],
                    row["last_enqueued_at"],
                    created_at,
                    created_at,
                ),
            )

    def _migrate_schedule_operator_ids(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            SELECT schedule_id, operator_id, operator_ids_json
            FROM schedule_alarms
            """
        ).fetchall()
        for row in rows:
            try:
                operator_ids = json.loads(row["operator_ids_json"] or "[]")
            except (TypeError, ValueError):
                operator_ids = []
            if operator_ids or not row["operator_id"]:
                continue
            conn.execute(
                "UPDATE schedule_alarms SET operator_ids_json = ? WHERE schedule_id = ?",
                (json.dumps([row["operator_id"]], ensure_ascii=False), row["schedule_id"]),
            )

    def _normalize_pending_queue(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            SELECT run_id
            FROM runs
            WHERE status = 'pending'
            ORDER BY
                CASE WHEN queue_position > 0 THEN queue_position ELSE 999999 END,
                created_at
            """
        ).fetchall()
        for idx, row in enumerate(rows, start=1):
            conn.execute("UPDATE runs SET queue_position = ? WHERE run_id = ?", (idx, row["run_id"]))
        conn.execute("UPDATE runs SET queue_position = 0 WHERE status != 'pending' AND queue_position != 0")

    def _cleanup_stale_account_locks(self, conn: sqlite3.Connection) -> int:
        cursor = conn.execute(
            """
            DELETE FROM account_locks
            WHERE NOT EXISTS (
                SELECT 1 FROM runs
                WHERE runs.run_id = account_locks.run_id
                  AND runs.status IN ('running', 'paused')
            )
            """
        )
        return int(cursor.rowcount)

    def _ensure_default_operator_passwords(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            SELECT operator_id, created_at
            FROM operators
            WHERE COALESCE(password_hash, '') = ''
            """
        ).fetchall()
        for row in rows:
            updated_at = row["created_at"] or self._now()
            conn.execute(
                """
                UPDATE operators
                SET password_hash = ?,
                    updated_at = ?
                WHERE operator_id = ?
                """,
                (_hash_operator_password(DEFAULT_OPERATOR_PASSWORD), updated_at, row["operator_id"]),
            )

    def normalize_pending_queue(self) -> None:
        with self._connect() as conn:
            self._normalize_pending_queue(conn)

    def _next_queue_position(self, conn: sqlite3.Connection) -> int:
        value = conn.execute("SELECT COALESCE(MAX(queue_position), 0) + 1 FROM runs WHERE status = 'pending'").fetchone()[0]
        return int(value)

    def _run_account_keys(self, run: dict[str, Any]) -> set[str]:
        account_keys = {str(key or "") for key in run.get("account_keys") or [] if key}
        if run.get("run_kind") != RUN_KIND_SYNC_SUPPLIERS and run.get("suppliers"):
            supplier_accounts = {
                str(item.get("account_key") or "")
                for item in run.get("suppliers") or []
                if item.get("account_key")
            }
            if supplier_accounts:
                account_keys = supplier_accounts
        if not account_keys:
            account_keys = {str(account.get("key") or "") for account in self.list_accounts() if account.get("key")}
        return account_keys

    def _insert_account_locks(self, conn: sqlite3.Connection, run_id: str, account_keys: set[str]) -> None:
        accounts_by_key = {str(account.get("key") or ""): account for account in self.list_accounts()}
        missing = sorted(key for key in account_keys if key not in accounts_by_key)
        if missing:
            raise KeyError(f"账号不存在或未启用: {', '.join(missing)}")
        now = self._now()
        for account_key in sorted(account_keys):
            account = accounts_by_key[account_key]
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

    def _now(self) -> str:
        return datetime.now().isoformat(timespec="seconds")

    @property
    def worker_heartbeat_path(self) -> Path:
        return WORKER_HEARTBEAT_PATH

    def list_tasks(self) -> list[dict[str, Any]]:
        return [
            {"task_key": key, "title": value["title"], "file_task_text": value["file_task_text"]}
            for key, value in TASKS.items()
        ]

    def list_accounts(self, include_disabled: bool = False) -> list[dict[str, Any]]:
        return self.account_store.list_accounts(include_disabled=include_disabled)

    def delete_account(self, account_key: str) -> None:
        account = next(
            (item for item in self.list_accounts(include_disabled=True) if item["key"] == account_key),
            None,
        )
        if account is None:
            raise KeyError(account_key)
        with self._connect() as conn:
            lock = conn.execute(
                "SELECT run_id FROM account_locks WHERE account_key = ?",
                (account_key,),
            ).fetchone()
            if lock is not None:
                raise RuntimeError("账号正被运行任务占用，不能删除")
            live = conn.execute(
                """
                SELECT run_id FROM runs
                WHERE status IN ('pending', 'running', 'paused')
                  AND account_keys_json LIKE ?
                LIMIT 1
                """,
                (f'%"{account_key}"%',),
            ).fetchone()
            if live is not None:
                raise RuntimeError("账号存在未完成任务，不能删除")
            conn.execute("DELETE FROM account_suppliers WHERE account_key = ?", (account_key,))
            conn.execute("DELETE FROM operator_suppliers WHERE account_key = ?", (account_key,))
            conn.execute("DELETE FROM item_id_config WHERE account_key = ?", (account_key,))
            conn.execute("DELETE FROM account_ownership WHERE account_key = ?", (account_key,))
        self.account_store.delete_account(account_key)

    def _normalize_schedule_task_keys(self, task_keys: list[str]) -> list[str]:
        result: list[str] = []
        for task_key in task_keys:
            value = str(task_key or "").strip()
            if value in TASKS and value not in result:
                result.append(value)
        if not result:
            raise ValueError("请至少选择一个执行任务")
        return result

    def _normalize_schedule_operator_ids(self, operator_ids: list[str], enabled: bool, all_operators: bool = False) -> list[str]:
        result: list[str] = []
        for operator_id in operator_ids:
            value = str(operator_id or "").strip()
            if value and value not in result:
                if self.get_operator(value) is None:
                    raise ValueError("执行组员不存在")
                result.append(value)
        if enabled and not result and not all_operators:
            raise ValueError("启用定时任务前请先选择组员")
        return result

    def _normalize_schedule_weekdays(self, weekdays: list[int]) -> list[int]:
        normalized = sorted({int(day) for day in weekdays if 0 <= int(day) <= 6})
        if not normalized:
            raise ValueError("请至少选择一个执行日")
        return normalized

    def _schedule_row(self, row: sqlite3.Row) -> dict[str, Any]:
        try:
            raw_task_keys = json.loads(row["task_keys_json"] or "[]")
        except (TypeError, ValueError):
            raw_task_keys = []
        try:
            raw_weekdays = json.loads(row["weekdays_json"] or "[]")
        except (TypeError, ValueError):
            raw_weekdays = []
        task_keys = [
            str(task_key)
            for task_key in raw_task_keys
            if str(task_key) in TASKS
        ]
        try:
            raw_operator_ids = json.loads(row["operator_ids_json"] or "[]")
        except (TypeError, ValueError):
            raw_operator_ids = []
        operator_ids: list[str] = []
        for operator_id in raw_operator_ids:
            value = str(operator_id or "").strip()
            if value and value not in operator_ids:
                operator_ids.append(value)
        if not operator_ids and row["operator_id"]:
            operator_ids.append(row["operator_id"])
        return {
            "schedule_id": row["schedule_id"],
            "task_key": task_keys[0] if len(task_keys) == 1 else "",
            "task_keys": task_keys,
            "task_titles": [TASKS[task_key]["title"] for task_key in task_keys],
            "operator_id": operator_ids[0] if operator_ids else "",
            "operator_ids": operator_ids,
            "all_operators": bool(row["all_operators"]),
            "enabled": bool(row["enabled"]),
            "time_of_day": row["time_of_day"],
            "weekdays": sorted({int(day) for day in raw_weekdays if 0 <= int(day) <= 6}),
            "headed": bool(row["headed"]),
            "last_enqueued_at": row["last_enqueued_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_schedules(self, operator_id: str = "") -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT schedule_id, task_keys_json, operator_id, operator_ids_json, all_operators, enabled, time_of_day,
                       weekdays_json, headed, last_enqueued_at, created_at, updated_at
                FROM schedule_alarms
                ORDER BY time_of_day, created_at, schedule_id
                """
            ).fetchall()
        schedules = [self._schedule_row(row) for row in rows]
        if operator_id:
            return [schedule for schedule in schedules if schedule["all_operators"] or operator_id in schedule["operator_ids"]]
        return schedules

    def _schedule_values(
        self,
        *,
        task_keys: list[str],
        operator_ids: list[str],
        all_operators: bool,
        enabled: bool,
        time_of_day: str,
        weekdays: list[int],
        headed: bool,
    ) -> tuple[list[str], list[str], bool, bool, str, list[int], bool]:
        normalized_task_keys = self._normalize_schedule_task_keys(task_keys)
        normalized_operator_ids = self._normalize_schedule_operator_ids(operator_ids, enabled, all_operators)
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(time_of_day or "")):
            raise ValueError("执行时间格式应为 HH:MM")
        normalized_weekdays = self._normalize_schedule_weekdays(weekdays)
        return (
            normalized_task_keys,
            normalized_operator_ids,
            bool(all_operators),
            bool(enabled),
            str(time_of_day),
            normalized_weekdays,
            bool(headed),
        )

    def create_schedule(
        self,
        *,
        task_keys: list[str],
        operator_ids: list[str],
        all_operators: bool = False,
        enabled: bool,
        time_of_day: str,
        weekdays: list[int],
        headed: bool,
    ) -> dict[str, Any]:
        task_keys, operator_ids, all_operators, enabled, time_of_day, weekdays, headed = self._schedule_values(
            task_keys=task_keys,
            operator_ids=operator_ids,
            all_operators=all_operators,
            enabled=enabled,
            time_of_day=time_of_day,
            weekdays=weekdays,
            headed=headed,
        )
        schedule_id = str(uuid.uuid4())
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO schedule_alarms (
                    schedule_id, task_keys_json, operator_id, operator_ids_json, all_operators, enabled, time_of_day,
                    weekdays_json, headed, last_enqueued_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?)
                """,
                (
                    schedule_id,
                    json.dumps(task_keys, ensure_ascii=False),
                    operator_ids[0] if operator_ids else "",
                    json.dumps(operator_ids, ensure_ascii=False),
                    1 if all_operators else 0,
                    1 if enabled else 0,
                    time_of_day,
                    json.dumps(weekdays, ensure_ascii=False),
                    1 if headed else 0,
                    now,
                    now,
                ),
            )
        return self.get_schedule(schedule_id)

    def get_schedule(self, schedule_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT schedule_id, task_keys_json, operator_id, operator_ids_json, all_operators, enabled, time_of_day,
                       weekdays_json, headed, last_enqueued_at, created_at, updated_at
                FROM schedule_alarms
                WHERE schedule_id = ?
                """,
                (schedule_id,),
            ).fetchone()
        if row is None:
            raise KeyError(schedule_id)
        return self._schedule_row(row)

    def update_schedule(
        self,
        schedule_id: str,
        *,
        task_keys: list[str],
        operator_ids: list[str],
        all_operators: bool = False,
        enabled: bool,
        time_of_day: str,
        weekdays: list[int],
        headed: bool,
    ) -> dict[str, Any]:
        task_keys, operator_ids, all_operators, enabled, time_of_day, weekdays, headed = self._schedule_values(
            task_keys=task_keys,
            operator_ids=operator_ids,
            all_operators=all_operators,
            enabled=enabled,
            time_of_day=time_of_day,
            weekdays=weekdays,
            headed=headed,
        )
        now = self._now()
        with self._connect() as conn:
            updated = conn.execute(
                """
                UPDATE schedule_alarms
                SET task_keys_json = ?,
                    operator_id = ?,
                    operator_ids_json = ?,
                    all_operators = ?,
                    enabled = ?,
                    time_of_day = ?,
                    weekdays_json = ?,
                    headed = ?,
                    last_enqueued_at = '',
                    updated_at = ?
                WHERE schedule_id = ?
                """,
                (
                    json.dumps(task_keys, ensure_ascii=False),
                    operator_ids[0] if operator_ids else "",
                    json.dumps(operator_ids, ensure_ascii=False),
                    1 if all_operators else 0,
                    1 if enabled else 0,
                    time_of_day,
                    json.dumps(weekdays, ensure_ascii=False),
                    1 if headed else 0,
                    now,
                    schedule_id,
                ),
            )
        if updated.rowcount != 1:
            raise KeyError(schedule_id)
        return self.get_schedule(schedule_id)

    def delete_schedule(self, schedule_id: str) -> None:
        with self._connect() as conn:
            deleted = conn.execute(
                "DELETE FROM schedule_alarms WHERE schedule_id = ?",
                (schedule_id,),
            )
        if deleted.rowcount != 1:
            raise KeyError(schedule_id)

    def enqueue_due_schedules(self, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or datetime.now()
        stamp = now.strftime("%Y-%m-%d %H:%M")
        created: list[dict[str, Any]] = []
        for schedule in self.list_schedules():
            if not schedule["enabled"]:
                continue
            if now.weekday() not in schedule["weekdays"] or schedule["time_of_day"] != now.strftime("%H:%M"):
                continue
            if schedule["last_enqueued_at"] == stamp:
                continue
            operator_ids = [item["operator_id"] for item in self.list_operators() if item.get("active", True)] if schedule["all_operators"] else schedule["operator_ids"]
            for operator_id in operator_ids:
                suppliers = self.list_runnable_suppliers(operator_id)
                if suppliers:
                    run = self.create_run(
                        task_keys=schedule["task_keys"],
                        account_keys=sorted({item["account_key"] for item in suppliers}),
                        force_account_tasks=True,
                        headed=schedule["headed"],
                        suppliers=suppliers,
                        operator_id=operator_id,
                    )
                    created.append(run)
            with self._connect() as conn:
                conn.execute(
                    "UPDATE schedule_alarms SET last_enqueued_at = ?, updated_at = ? WHERE schedule_id = ?",
                    (stamp, self._now(), schedule["schedule_id"]),
                )
        return created

    def create_run(
        self,
        task_keys: list[str],
        account_keys: list[str],
        force_account_tasks: bool,
        headed: bool,
        *,
        suppliers: list[dict[str, Any]] | None = None,
        operator_id: str = "",
        operator_name: str = "",
        run_kind: str = RUN_KIND_TASKS,
        retry_attempt: int = 0,
        retry_parent_run_id: str = "",
    ) -> dict[str, Any]:
        run_id = str(uuid.uuid4())
        if operator_id and not operator_name:
            operator = self.get_operator(operator_id)
            if operator is None:
                raise ValueError("运营人员不存在")
            operator_name = str(operator.get("name") or "")
        supplier_rows = self.resolve_run_suppliers(
            operator_id=operator_id,
            account_keys=account_keys,
            suppliers=suppliers or [],
            run_kind=run_kind or RUN_KIND_TASKS,
        )
        snapshot = self.build_assignment_snapshot(account_keys, supplier_rows, operator_id, operator_name)
        with self._connect() as conn:
            row = RunRow(
                run_id=run_id,
                task_keys=task_keys,
                account_keys=account_keys,
                status="pending",
                created_at=self._now(),
                updated_at=self._now(),
                force_account_tasks=force_account_tasks,
                headed=headed,
                queue_position=self._next_queue_position(conn),
                suppliers=supplier_rows,
                operator_id=operator_id,
                operator_name=operator_name,
                run_kind=run_kind or RUN_KIND_TASKS,
                assignment_snapshot=snapshot,
                retry_attempt=retry_attempt,
                retry_parent_run_id=retry_parent_run_id,
            )
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, task_keys_json, account_keys_json, status, created_at, updated_at,
                    started_at, finished_at, force_account_tasks, headed, queue_position,
                    pause_requested, error, result_json, suppliers_json, operator_id,
                    operator_name, run_kind, assignment_snapshot_json, retry_attempt,
                    retry_parent_run_id, auto_retry_enqueued
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    row.queue_position,
                    0,
                    row.error,
                    row.result_json,
                    json.dumps(row.suppliers or [], ensure_ascii=False),
                    row.operator_id,
                    row.operator_name,
                    row.run_kind,
                    json.dumps(row.assignment_snapshot or [], ensure_ascii=False),
                    row.retry_attempt,
                    row.retry_parent_run_id,
                    1 if row.auto_retry_enqueued else 0,
                ),
            )
        return self.get_run(run_id)

    def enqueue_auto_retry_runs(self, run_id: str, result_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        run = self.get_run(run_id)
        if (
            run.get("run_kind") != RUN_KIND_TASKS
            or int(run.get("retry_attempt") or 0) >= 1
            or run.get("auto_retry_enqueued")
        ):
            return []

        results = {
            (
                str(item.get("account") or item.get("account_key") or ""),
                str(item.get("supplier_id") or ""),
                str(item.get("task") or item.get("task_key") or ""),
            ): item
            for item in result_items
        }
        retry_items: list[tuple[str, str, dict[str, Any]]] = []
        for supplier in run.get("suppliers") or []:
            account_key = str(supplier.get("account_key") or "")
            supplier_id = str(supplier.get("supplier_id") or "")
            if not account_key or not supplier_id:
                continue
            for task_key in run.get("task_keys") or []:
                item = results.get((account_key, supplier_id, str(task_key)))
                successful = bool(
                    item
                    and item.get("status") == "ok"
                    and (item.get("raw_file") or item.get("cleaned_file"))
                )
                if not successful:
                    retry_items.append((account_key, str(task_key), dict(supplier)))

        created_ids: list[str] = []
        now = self._now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            parent = conn.execute(
                "SELECT retry_attempt, auto_retry_enqueued FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if parent is None:
                raise KeyError(run_id)
            if int(parent["retry_attempt"] or 0) >= 1 or bool(parent["auto_retry_enqueued"]):
                return []
            queue_position = self._next_queue_position(conn)
            for offset, (account_key, task_key, supplier) in enumerate(retry_items):
                child_id = str(uuid.uuid4())
                snapshot = self.build_assignment_snapshot(
                    [account_key], [supplier], str(run.get("operator_id") or ""), str(run.get("operator_name") or "")
                )
                conn.execute(
                    """
                    INSERT INTO runs (
                        run_id, task_keys_json, account_keys_json, status, created_at, updated_at,
                        started_at, finished_at, force_account_tasks, headed, queue_position,
                        pause_requested, cancel_requested, error, result_json, suppliers_json,
                        operator_id, operator_name, run_kind, assignment_snapshot_json,
                        retry_attempt, retry_parent_run_id, auto_retry_enqueued
                    ) VALUES (?, ?, ?, 'pending', ?, ?, '', '', 1, ?, ?, 0, 0, '', '[]', ?, ?, ?, ?, ?, 1, ?, 0)
                    """,
                    (
                        child_id,
                        json.dumps([task_key], ensure_ascii=False),
                        json.dumps([account_key], ensure_ascii=False),
                        now,
                        now,
                        1 if run.get("headed", True) else 0,
                        queue_position + offset,
                        json.dumps([supplier], ensure_ascii=False),
                        str(run.get("operator_id") or ""),
                        str(run.get("operator_name") or ""),
                        RUN_KIND_TASKS,
                        json.dumps(snapshot, ensure_ascii=False),
                        run_id,
                    ),
                )
                created_ids.append(child_id)
            conn.execute(
                "UPDATE runs SET auto_retry_enqueued = 1, updated_at = ? WHERE run_id = ?",
                (now, run_id),
            )
        return [self.get_run(child_id) for child_id in created_ids]

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            return self._run_row(row)

    def list_runs(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM runs
                ORDER BY
                    CASE status WHEN 'running' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,
                    CASE WHEN status = 'pending' THEN queue_position ELSE 0 END,
                    created_at DESC
                """
            ).fetchall()
            return [self._run_row(row) for row in rows]

    def update_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
        queue_position: int | None = None,
        pause_requested: bool | None = None,
        cancel_requested: bool | None = None,
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
        if queue_position is not None:
            fields.append("queue_position = ?")
            values.append(queue_position)
        elif status is not None and status != "pending":
            fields.append("queue_position = ?")
            values.append(0)
        if pause_requested is not None:
            fields.append("pause_requested = ?")
            values.append(1 if pause_requested else 0)
        if cancel_requested is not None:
            fields.append("cancel_requested = ?")
            values.append(1 if cancel_requested else 0)
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
        keys = row.keys()
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
            "queue_position": int(row["queue_position"] or 0),
            "pause_requested": bool(row["pause_requested"]),
            "cancel_requested": bool(row["cancel_requested"]) if "cancel_requested" in keys else False,
            "error": row["error"],
            "result": json.loads(row["result_json"] or "[]"),
            "suppliers": json.loads(row["suppliers_json"] or "[]") if "suppliers_json" in keys else [],
            "operator_id": row["operator_id"] if "operator_id" in keys else "",
            "operator_name": row["operator_name"] if "operator_name" in keys else "",
            "run_kind": row["run_kind"] if "run_kind" in keys else RUN_KIND_TASKS,
            "assignment_snapshot": json.loads(row["assignment_snapshot_json"] or "[]") if "assignment_snapshot_json" in keys else [],
            "retry_attempt": int(row["retry_attempt"] or 0) if "retry_attempt" in keys else 0,
            "retry_parent_run_id": row["retry_parent_run_id"] if "retry_parent_run_id" in keys else "",
            "auto_retry_enqueued": bool(row["auto_retry_enqueued"]) if "auto_retry_enqueued" in keys else False,
        }

    def claim_next_pending_run(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._cleanup_stale_account_locks(conn)
            self._normalize_pending_queue(conn)
            rows = conn.execute(
                """
                SELECT * FROM runs
                WHERE status = 'pending'
                ORDER BY queue_position, created_at
                """
            ).fetchall()
            if not rows:
                return None
            locked_accounts = {
                str(row["account_key"] or "")
                for row in conn.execute("SELECT account_key FROM account_locks").fetchall()
                if row["account_key"]
            }
            row = None
            claimed_accounts: set[str] = set()
            for candidate in rows:
                run = self._run_row(candidate)
                run_accounts = self._run_account_keys(run)
                if run_accounts & locked_accounts:
                    continue
                row = candidate
                claimed_accounts = run_accounts
                break
            if row is None:
                return None
            now = self._now()
            updated = conn.execute(
                """
                UPDATE runs
                SET status = 'running',
                    started_at = CASE WHEN started_at = '' THEN ? ELSE started_at END,
                    updated_at = ?,
                    queue_position = 0
                WHERE run_id = ? AND status = 'pending' AND pause_requested = 0
                """,
                (now, now, row["run_id"]),
            ).rowcount
            if not updated:
                return None
            self._insert_account_locks(conn, row["run_id"], claimed_accounts)
            self._normalize_pending_queue(conn)
            claimed = conn.execute("SELECT * FROM runs WHERE run_id = ?", (row["run_id"],)).fetchone()
            return self._run_row(claimed) if claimed is not None else None

    def next_pending_run(self) -> dict[str, Any] | None:
        return self.claim_next_pending_run()

    def requeue_run(self, run_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT status FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["status"] != "running":
                raise RuntimeError("只能将运行中的任务重新排队")
            now = self._now()
            conn.execute(
                """
                UPDATE runs
                SET status = 'pending',
                    updated_at = ?,
                    started_at = '',
                    finished_at = '',
                    queue_position = (
                        SELECT COALESCE(MAX(queue_position), 0) + 1
                        FROM runs
                        WHERE status = 'pending'
                    ),
                    pause_requested = 0,
                    cancel_requested = 0,
                    error = ''
                WHERE run_id = ?
                """,
                (now, run_id),
            )
            self._normalize_pending_queue(conn)
        return self.get_run(run_id)

    def pause_run(self, run_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status, pause_requested FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            now = self._now()
            if row["status"] == "pending":
                conn.execute(
                    """
                    UPDATE runs
                    SET status = 'paused',
                        updated_at = ?,
                        queue_position = 0,
                        pause_requested = 0,
                        cancel_requested = 0,
                        error = ?
                    WHERE run_id = ?
                    """,
                    (now, "用户暂停任务，尚未执行", run_id),
                )
                self._normalize_pending_queue(conn)
            elif row["status"] == "running":
                conn.execute(
                    """
                    UPDATE runs
                    SET updated_at = ?,
                        pause_requested = 1,
                        cancel_requested = 0,
                        error = ?
                    WHERE run_id = ?
                    """,
                    (now, "已请求暂停，将在当前子任务完成后暂停", run_id),
                )
            elif row["status"] == "paused":
                pass
            else:
                raise RuntimeError("只能暂停排队中或运行中的任务")
        return self.get_run(run_id)

    def resume_run(self, run_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status, pause_requested FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            now = self._now()
            if row["status"] == "paused":
                conn.execute(
                    """
                    UPDATE runs
                    SET status = 'pending',
                        updated_at = ?,
                        queue_position = (
                            SELECT COALESCE(MAX(queue_position), 0) + 1
                            FROM runs
                            WHERE status = 'pending'
                        ),
                        pause_requested = 0,
                        cancel_requested = 0,
                        finished_at = '',
                        error = ''
                    WHERE run_id = ?
                    """,
                    (now, run_id),
                )
                self._normalize_pending_queue(conn)
            elif row["status"] == "running" and row["pause_requested"]:
                conn.execute(
                    """
                    UPDATE runs
                    SET updated_at = ?,
                        pause_requested = 0,
                        cancel_requested = 0,
                        error = ''
                    WHERE run_id = ?
                    """,
                    (now, run_id),
                )
            else:
                raise RuntimeError("只能恢复已暂停或正在等待暂停的任务")
        return self.get_run(run_id)

    def is_pause_requested(self, run_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT pause_requested FROM runs WHERE run_id = ? AND status = 'running'",
                (run_id,),
            ).fetchone()
            return bool(row and row["pause_requested"])

    def is_cancel_requested(self, run_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cancel_requested FROM runs WHERE run_id = ? AND status = 'running'",
                (run_id,),
            ).fetchone()
            return bool(row and row["cancel_requested"])

    def cancel_pending_run(self, run_id: str, reason: str = "用户取消排队") -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT status FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            now = self._now()
            if row["status"] == "running":
                conn.execute(
                    """
                    UPDATE runs
                    SET updated_at = ?,
                        pause_requested = 1,
                        cancel_requested = 1,
                        error = ?
                    WHERE run_id = ?
                    """,
                    (now, "已请求取消，将在当前子任务完成后停止", run_id),
                )
            elif row["status"] not in {"pending", "paused"}:
                raise RuntimeError("只能取消排队中、运行中或已暂停的任务")
            else:
                cancel_reason = "用户取消已暂停任务" if row["status"] == "paused" and reason == "用户取消排队" else reason
                conn.execute(
                    """
                    UPDATE runs
                    SET status = 'cancelled',
                        updated_at = ?,
                        finished_at = ?,
                        queue_position = 0,
                        pause_requested = 0,
                        cancel_requested = 0,
                        error = ?
                    WHERE run_id = ?
                    """,
                    (now, now, cancel_reason, run_id),
                )
                self._normalize_pending_queue(conn)
        return self.get_run(run_id)

    def move_pending_run(self, run_id: str, direction: int) -> dict[str, Any]:
        if direction not in {-1, 1}:
            raise ValueError("direction must be -1 or 1")
        with self._connect() as conn:
            self._normalize_pending_queue(conn)
            rows = conn.execute(
                """
                SELECT run_id, queue_position
                FROM runs
                WHERE status = 'pending'
                ORDER BY queue_position, created_at
                """
            ).fetchall()
            ids = [row["run_id"] for row in rows]
            if run_id not in ids:
                existing = conn.execute("SELECT status FROM runs WHERE run_id = ?", (run_id,)).fetchone()
                if existing is None:
                    raise KeyError(run_id)
                raise RuntimeError("只能调整排队中的任务")
            idx = ids.index(run_id)
            target_idx = idx + direction
            if target_idx < 0 or target_idx >= len(rows):
                return self.get_run(run_id)
            current = rows[idx]
            target = rows[target_idx]
            conn.execute("UPDATE runs SET queue_position = ? WHERE run_id = ?", (target["queue_position"], current["run_id"]))
            conn.execute("UPDATE runs SET queue_position = ? WHERE run_id = ?", (current["queue_position"], target["run_id"]))
            self._normalize_pending_queue(conn)
        return self.get_run(run_id)

    def worker_status(self, stale_after_sec: int = 8) -> dict[str, Any]:
        heartbeat_path = self.worker_heartbeat_path
        heartbeat_at = ""
        worker_pid: int | None = None
        heartbeat_age: float | None = None
        if heartbeat_path.exists():
            try:
                payload = json.loads(heartbeat_path.read_text(encoding="utf-8"))
                heartbeat_at = str(payload.get("updated_at") or "")
                worker_pid = payload.get("pid")
            except (OSError, ValueError, TypeError):
                heartbeat_at = ""
            try:
                heartbeat_age = max(0.0, datetime.now().timestamp() - heartbeat_path.stat().st_mtime)
            except OSError:
                heartbeat_age = None

        with self._connect() as conn:
            pending_count = int(
                conn.execute("SELECT COUNT(*) FROM runs WHERE status = 'pending'").fetchone()[0]
            )
            running_row = conn.execute(
                "SELECT run_id FROM runs WHERE status = 'running' ORDER BY started_at DESC LIMIT 1"
            ).fetchone()

        return {
            "worker_online": bool(heartbeat_age is not None and heartbeat_age <= stale_after_sec),
            "running_run_id": running_row["run_id"] if running_row else "",
            "pending_count": pending_count,
            "running_count": 1 if running_row else 0,
            "heartbeat_at": heartbeat_at,
            "heartbeat_age_seconds": round(heartbeat_age, 1) if heartbeat_age is not None else None,
            "worker_pid": worker_pid,
        }

    def list_account_locks(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT account_key, port, profile_dir, run_id, acquired_at
                FROM account_locks
                ORDER BY acquired_at
                """
            ).fetchall()
            return [dict(row) for row in rows]

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

    def _normalize_supplier_payload(self, suppliers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for item in suppliers:
            if not isinstance(item, dict):
                continue
            supplier_id = str(item.get("supplier_id") or "").strip()
            supplier_name = str(item.get("supplier_name") or "").strip()
            account_key = str(item.get("account_key") or "").strip()
            if not supplier_id and not supplier_name:
                continue
            if not supplier_id:
                supplier_id = f"name:{supplier_name}"
            if _is_placeholder_supplier(supplier_id, supplier_name):
                continue
            key = (account_key, supplier_id)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "account_key": account_key,
                    "supplier_id": supplier_id,
                    "supplier_name": supplier_name,
                }
            )
        return rows

    def list_supply_chain_users(self, include_disabled: bool = False) -> list[dict[str, Any]]:
        query = """
            SELECT user_id, username, name, enabled, created_at, updated_at
            FROM supply_chain_users
        """
        if not include_disabled:
            query += " WHERE enabled = 1"
        query += " ORDER BY created_at, name"
        with self._connect() as conn:
            rows = conn.execute(query).fetchall()
            return [
                {
                    "user_id": row["user_id"],
                    "username": row["username"],
                    "name": row["name"],
                    "enabled": bool(row["enabled"]),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
                for row in rows
            ]

    def get_supply_chain_user(self, user_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT user_id, username, name, enabled, created_at, updated_at
                FROM supply_chain_users WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            if row is None:
                return None
            item = dict(row)
            item["enabled"] = bool(item["enabled"])
            return item

    def authenticate_supply_chain_user(self, username: str, password: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT user_id, username, name, password_hash, enabled, created_at, updated_at
                FROM supply_chain_users WHERE username = ?
                """,
                (str(username or "").strip(),),
            ).fetchone()
            if row is None or not bool(row["enabled"]):
                return None
            if not _verify_operator_password(str(password or ""), str(row["password_hash"] or "")):
                return None
            return {
                "user_id": row["user_id"],
                "username": row["username"],
                "name": row["name"],
                "enabled": True,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }

    def create_supply_chain_user(self, username: str, name: str, password: str) -> dict[str, Any]:
        username = str(username or "").strip()
        name = str(name or "").strip()
        password = str(password or "")
        if not username or not name or not password:
            raise ValueError("账号、姓名和密码均为必填")
        user_id = str(uuid.uuid4())
        now = self._now()
        with self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO supply_chain_users (
                        user_id, username, name, password_hash, enabled, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 1, ?, ?)
                    """,
                    (user_id, username, name, _hash_operator_password(password), now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"供应链账号已存在: {username}") from exc
        user = self.get_supply_chain_user(user_id)
        if user is None:
            raise RuntimeError("创建供应链账号失败")
        return user

    def update_supply_chain_user(
        self,
        user_id: str,
        *,
        username: str | None = None,
        name: str | None = None,
        password: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        current = self.get_supply_chain_user(user_id)
        if current is None:
            raise KeyError(user_id)
        values = {
            "username": str(username).strip() if username is not None else current["username"],
            "name": str(name).strip() if name is not None else current["name"],
            "enabled": int(enabled) if enabled is not None else int(current["enabled"]),
        }
        if not values["username"] or not values["name"]:
            raise ValueError("账号和姓名不能为空")
        with self._connect() as conn:
            try:
                if password is None:
                    conn.execute(
                        """
                        UPDATE supply_chain_users
                        SET username = ?, name = ?, enabled = ?, updated_at = ?
                        WHERE user_id = ?
                        """,
                        (values["username"], values["name"], values["enabled"], self._now(), user_id),
                    )
                else:
                    if not str(password):
                        raise ValueError("密码不能为空")
                    conn.execute(
                        """
                        UPDATE supply_chain_users
                        SET username = ?, name = ?, password_hash = ?, enabled = ?, updated_at = ?
                        WHERE user_id = ?
                        """,
                        (
                            values["username"], values["name"], _hash_operator_password(str(password)),
                            values["enabled"], self._now(), user_id,
                        ),
                    )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"供应链账号已存在: {values['username']}") from exc
        updated = self.get_supply_chain_user(user_id)
        if updated is None:
            raise RuntimeError("更新供应链账号失败")
        return updated

    def delete_supply_chain_user(self, user_id: str) -> dict[str, Any]:
        user = self.get_supply_chain_user(user_id)
        if user is None:
            raise KeyError(user_id)
        with self._connect() as conn:
            assigned = conn.execute(
                "SELECT 1 FROM operators WHERE supply_chain_user_id = ? AND active = 1 LIMIT 1",
                (user_id,),
            ).fetchone()
            if assigned is not None:
                raise RuntimeError("该供应链账号还有运营组员，请先调整归属")
            conn.execute("DELETE FROM supply_chain_users WHERE user_id = ?", (user_id,))
        return user

    def set_account_owner(self, account_key: str, creator_role: str, creator_user_id: str = "") -> None:
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO account_ownership (
                    account_key, creator_role, creator_user_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(account_key) DO UPDATE SET
                    creator_role = excluded.creator_role,
                    creator_user_id = excluded.creator_user_id,
                    updated_at = excluded.updated_at
                """,
                (account_key, creator_role, creator_user_id, now, now),
            )

    def get_account_owner(self, account_key: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT account_key, creator_role, creator_user_id, created_at, updated_at
                FROM account_ownership WHERE account_key = ?
                """,
                (account_key,),
            ).fetchone()
            if row is None:
                return {
                    "account_key": account_key,
                    "creator_role": "admin",
                    "creator_user_id": "",
                    "created_at": "",
                    "updated_at": "",
                }
            return dict(row)

    def replace_item_id_config(
        self,
        rows: list[dict[str, Any]],
        *,
        original_name: str,
        uploaded_by_role: str,
        uploaded_by_user_id: str = "",
    ) -> dict[str, Any]:
        normalized: list[tuple[str, str, str]] = []
        errors: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        affected: set[tuple[str, str]] = set()
        known_accounts = {account["key"] for account in self.list_accounts(include_disabled=True)}
        for index, item in enumerate(rows, start=2):
            account_key = str(item.get("account_key") or "").strip()
            supplier_id = str(item.get("supplier_id") or "").strip()
            item_id = str(item.get("item_id") or "").strip()
            if not account_key and not supplier_id and not item_id:
                continue
            if not account_key or not supplier_id:
                errors.append({"row": index, "error": "猫超账号和二级供应商ID为必填"})
                continue
            if account_key not in known_accounts:
                errors.append({"row": index, "error": f"猫超账号不存在: {account_key}"})
                continue
            if self.get_account_supplier(account_key, supplier_id) is None:
                errors.append({"row": index, "error": f"二级供应商不属于该账号: {supplier_id}"})
                continue
            affected.add((account_key, supplier_id))
            if not item_id:
                continue
            key = (account_key, supplier_id, item_id)
            if key in seen:
                continue
            seen.add(key)
            normalized.append(key)
        if errors:
            return {"status": "rejected", "row_count": len(normalized), "error_count": len(errors), "errors": errors}
        upload_id = str(uuid.uuid4())
        now = self._now()
        with self._connect() as conn:
            conn.execute("UPDATE item_id_uploads SET status = 'superseded' WHERE status = 'active'")
            conn.execute(
                """
                INSERT INTO item_id_uploads (
                    upload_id, original_name, uploaded_by_role, uploaded_by_user_id,
                    uploaded_at, row_count, error_count, status, errors_json
                ) VALUES (?, ?, ?, ?, ?, ?, 0, 'active', '[]')
                """,
                (upload_id, original_name, uploaded_by_role, uploaded_by_user_id, now, len(normalized)),
            )
            for account_key, supplier_id in affected:
                conn.execute(
                    "INSERT INTO item_id_config_scopes (upload_id, account_key, supplier_id) VALUES (?, ?, ?)",
                    (upload_id, account_key, supplier_id),
                )
                conn.execute(
                    "UPDATE item_id_config SET active = 0 WHERE account_key = ? AND supplier_id = ?",
                    (account_key, supplier_id),
                )
            for account_key, supplier_id, item_id in normalized:
                conn.execute(
                    """
                    INSERT INTO item_id_config_history (
                        upload_id, account_key, supplier_id, item_id, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (upload_id, account_key, supplier_id, item_id, now),
                )
                conn.execute(
                    """
                    INSERT INTO item_id_config (
                        account_key, supplier_id, item_id, upload_id, active, created_at
                    ) VALUES (?, ?, ?, ?, 1, ?)
                    ON CONFLICT(account_key, supplier_id, item_id) DO UPDATE SET
                        upload_id = excluded.upload_id,
                        active = 1,
                        created_at = excluded.created_at
                    """,
                    (account_key, supplier_id, item_id, upload_id, now),
                )
        return {"status": "active", "upload_id": upload_id, "row_count": len(normalized), "error_count": 0, "errors": []}

    def rollback_item_id_config(self, upload_id: str) -> dict[str, Any]:
        now = self._now()
        with self._connect() as conn:
            upload = conn.execute(
                "SELECT upload_id FROM item_id_uploads WHERE upload_id = ?", (upload_id,)
            ).fetchone()
            if upload is None:
                raise KeyError(upload_id)
            scopes = conn.execute(
                "SELECT account_key, supplier_id FROM item_id_config_scopes WHERE upload_id = ?",
                (upload_id,),
            ).fetchall()
            rows = conn.execute(
                """
                SELECT account_key, supplier_id, item_id
                FROM item_id_config_history
                WHERE upload_id = ? ORDER BY rowid
                """,
                (upload_id,),
            ).fetchall()
            if not scopes:
                raise ValueError("该上传版本没有可恢复的配置范围")
            affected = {(row["account_key"], row["supplier_id"]) for row in scopes}
            for account_key, supplier_id in affected:
                conn.execute(
                    "UPDATE item_id_config SET active = 0 WHERE account_key = ? AND supplier_id = ?",
                    (account_key, supplier_id),
                )
            for row in rows:
                conn.execute(
                    """
                    INSERT INTO item_id_config (
                        account_key, supplier_id, item_id, upload_id, active, created_at
                    ) VALUES (?, ?, ?, ?, 1, ?)
                    ON CONFLICT(account_key, supplier_id, item_id) DO UPDATE SET
                        upload_id = excluded.upload_id, active = 1, created_at = excluded.created_at
                    """,
                    (row["account_key"], row["supplier_id"], row["item_id"], upload_id, now),
                )
            conn.execute("UPDATE item_id_uploads SET status = 'superseded' WHERE status = 'active'")
            conn.execute("UPDATE item_id_uploads SET status = 'active' WHERE upload_id = ?", (upload_id,))
        return {"status": "active", "upload_id": upload_id, "row_count": len(rows)}

    def list_item_ids(self, account_key: str, supplier_id: str) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT item_id FROM item_id_config
                WHERE account_key = ? AND supplier_id = ? AND active = 1
                ORDER BY rowid
                """,
                (account_key, supplier_id),
            ).fetchall()
            return [str(row["item_id"]) for row in rows]

    def list_item_id_config(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT account_key, supplier_id, item_id, upload_id, created_at
                FROM item_id_config WHERE active = 1
                ORDER BY account_key, supplier_id, rowid
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def list_item_id_uploads(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT upload_id, original_name, uploaded_by_role, uploaded_by_user_id,
                       uploaded_at, row_count, error_count, status, errors_json
                FROM item_id_uploads ORDER BY uploaded_at DESC
                """
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["errors"] = json.loads(item.pop("errors_json") or "[]")
                result.append(item)
            return result

    def list_operators(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT operator_id, name, created_at, updated_at,
                       COALESCE(supply_chain_user_id, '') AS supply_chain_user_id,
                       COALESCE(active, 1) AS active
                FROM operators
                ORDER BY created_at, name
                """
            ).fetchall()
            return [
                {
                    "operator_id": row["operator_id"],
                    "name": row["name"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "supply_chain_user_id": row["supply_chain_user_id"],
                    "active": bool(row["active"]),
                }
                for row in rows
            ]

    def get_operator(self, operator_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT operator_id, name, created_at, updated_at,
                       COALESCE(supply_chain_user_id, '') AS supply_chain_user_id,
                       COALESCE(active, 1) AS active
                FROM operators
                WHERE operator_id = ?
                """,
                (operator_id,),
            ).fetchone()
            if row is None:
                return None
            return {
                "operator_id": row["operator_id"],
                "name": row["name"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "supply_chain_user_id": row["supply_chain_user_id"],
                "active": bool(row["active"]),
            }

    def verify_operator_password(self, operator_id: str, password: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT password_hash FROM operators WHERE operator_id = ?",
                (operator_id,),
            ).fetchone()
            if row is None:
                return False
            encoded = str(row["password_hash"] or "")
            if not encoded:
                encoded = _hash_operator_password(DEFAULT_OPERATOR_PASSWORD)
                conn.execute(
                    "UPDATE operators SET password_hash = ?, updated_at = ? WHERE operator_id = ?",
                    (encoded, self._now(), operator_id),
                )
            return _verify_operator_password(str(password or ""), encoded)

    def set_operator_password(self, operator_id: str, password: str) -> dict[str, Any]:
        password = str(password or "")
        if not password:
            raise ValueError("密码不能为空")
        with self._connect() as conn:
            existing = conn.execute("SELECT operator_id FROM operators WHERE operator_id = ?", (operator_id,)).fetchone()
            if existing is None:
                raise KeyError(operator_id)
            conn.execute(
                """
                UPDATE operators
                SET password_hash = ?,
                    updated_at = ?
                WHERE operator_id = ?
                """,
                (_hash_operator_password(password), self._now(), operator_id),
            )
        operator = self.get_operator(operator_id)
        if operator is None:
            raise RuntimeError("更新组员密码失败")
        return operator

    def reset_operator_password(self, operator_id: str) -> dict[str, Any]:
        return self.set_operator_password(operator_id, DEFAULT_OPERATOR_PASSWORD)

    def create_operator(self, name: str, supply_chain_user_id: str = "") -> dict[str, Any]:
        name = str(name or "").strip()
        if not name:
            raise ValueError("运营人员名称不能为空")
        operator_id = str(uuid.uuid4())
        now = self._now()
        with self._connect() as conn:
            existing = conn.execute("SELECT operator_id FROM operators WHERE name = ?", (name,)).fetchone()
            if existing:
                raise ValueError(f"运营人员已存在: {name}")
            conn.execute(
                """
                INSERT INTO operators (
                    operator_id, name, password_hash, supply_chain_user_id, active, created_at, updated_at
                ) VALUES (?, ?, '', ?, 1, ?, ?)
                """,
                (operator_id, name, supply_chain_user_id, now, now),
            )
        operator = self.get_operator(operator_id)
        if operator is None:
            raise RuntimeError("创建运营人员失败")
        return operator

    def update_operator(
        self,
        operator_id: str,
        *,
        name: str | None = None,
        supply_chain_user_id: str | None = None,
        active: bool | None = None,
    ) -> dict[str, Any]:
        current = self.get_operator(operator_id)
        if current is None:
            raise KeyError(operator_id)
        next_name = str(name).strip() if name is not None else current["name"]
        next_owner = str(supply_chain_user_id).strip() if supply_chain_user_id is not None else current["supply_chain_user_id"]
        next_active = int(active) if active is not None else int(current["active"])
        if not next_name:
            raise ValueError("运营人员名称不能为空")
        if next_owner and self.get_supply_chain_user(next_owner) is None:
            raise ValueError("供应链账号不存在")
        with self._connect() as conn:
            try:
                conn.execute(
                    """
                    UPDATE operators
                    SET name = ?, supply_chain_user_id = ?, active = ?, updated_at = ?
                    WHERE operator_id = ?
                    """,
                    (next_name, next_owner, next_active, self._now(), operator_id),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"运营人员已存在: {next_name}") from exc
        updated = self.get_operator(operator_id)
        if updated is None:
            raise RuntimeError("更新运营人员失败")
        return updated

    def delete_operator(self, operator_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT operator_id, name FROM operators WHERE operator_id = ?",
                (operator_id,),
            ).fetchone()
            if row is None:
                raise KeyError(operator_id)
            live = conn.execute(
                """
                SELECT run_id
                FROM runs
                WHERE operator_id = ?
                  AND status IN ('pending', 'running', 'paused')
                LIMIT 1
                """,
                (operator_id,),
            ).fetchone()
            if live is not None:
                raise RuntimeError("该组员还有未完成任务，不能删除")
            conn.execute("DELETE FROM operator_suppliers WHERE operator_id = ?", (operator_id,))
            now = self._now()
            schedules = conn.execute(
                """
                SELECT schedule_id, operator_id, operator_ids_json, enabled
                FROM schedule_alarms
                WHERE operator_id = ? OR operator_ids_json LIKE ?
                """,
                (operator_id, f'%"{operator_id}"%'),
            ).fetchall()
            for schedule in schedules:
                try:
                    operator_ids = json.loads(schedule["operator_ids_json"] or "[]")
                except (TypeError, ValueError):
                    operator_ids = []
                normalized = []
                for value in operator_ids:
                    text = str(value or "").strip()
                    if text and text != operator_id and text not in normalized:
                        normalized.append(text)
                conn.execute(
                    """
                    UPDATE schedule_alarms
                    SET enabled = ?,
                        operator_id = ?,
                        operator_ids_json = ?,
                        updated_at = ?
                    WHERE schedule_id = ?
                    """,
                    (
                        1 if normalized and schedule["enabled"] else 0,
                        normalized[0] if normalized else "",
                        json.dumps(normalized, ensure_ascii=False),
                        now,
                        schedule["schedule_id"],
                    ),
                )
            conn.execute("DELETE FROM operators WHERE operator_id = ?", (operator_id,))
            return dict(row)

    def list_account_suppliers(self, account_key: str = "", include_hidden: bool = False) -> list[dict[str, Any]]:
        query = """
            SELECT s.account_key, s.supplier_id, s.supplier_name, s.visible, s.last_synced_at
            FROM account_suppliers s
        """
        params: list[Any] = []
        where: list[str] = []
        if account_key:
            where.append("s.account_key = ?")
            params.append(account_key)
        if not include_hidden:
            where.append("s.visible = 1")
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY s.account_key, s.supplier_name"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                if _is_placeholder_supplier(item.get("supplier_id") or "", item.get("supplier_name") or ""):
                    continue
                item["visible"] = bool(item["visible"])
                item["operators"] = self.list_supplier_operators(item["account_key"], item["supplier_id"])
                result.append(item)
            return result

    def list_supplier_operators(self, account_key: str, supplier_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT o.operator_id, o.name
                FROM operator_suppliers os
                JOIN operators o ON o.operator_id = os.operator_id
                WHERE os.account_key = ? AND os.supplier_id = ? AND COALESCE(os.active, 1) = 1
                ORDER BY o.name
                """,
                (account_key, supplier_id),
            ).fetchall()
            return [{"operator_id": row["operator_id"], "name": row["name"]} for row in rows]

    def upsert_account_suppliers(self, account_key: str, suppliers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        now = self._now()
        rows = self._normalize_supplier_payload(suppliers)
        menu_pattern = re.compile(r"Hi[,，]|我的账号|我的权限|协同任务|任务中心|账号管理|我的反馈|反馈问题|廉正举报|版本更新日志|搜索历史")
        if any(menu_pattern.search(item["supplier_name"] or item["supplier_id"]) for item in rows):
            raise ValueError("同步结果包含账号菜单项，已拒绝刷新供应商清单")
        visible_ids = {item["supplier_id"] for item in rows}
        with self._connect() as conn:
            conn.execute(
                "UPDATE account_suppliers SET visible = 0 WHERE account_key = ?",
                (account_key,),
            )
            for item in rows:
                conn.execute(
                    """
                    INSERT INTO account_suppliers (
                        account_key, supplier_id, supplier_name, visible, last_synced_at
                    ) VALUES (?, ?, ?, 1, ?)
                    ON CONFLICT(account_key, supplier_id) DO UPDATE SET
                        supplier_name = excluded.supplier_name,
                        visible = 1,
                        last_synced_at = excluded.last_synced_at
                    """,
                    (account_key, item["supplier_id"], item["supplier_name"] or item["supplier_id"], now),
                )
            if visible_ids:
                placeholders = ",".join("?" for _ in visible_ids)
                conn.execute(
                    f"UPDATE account_suppliers SET visible = 1 WHERE account_key = ? AND supplier_id IN ({placeholders})",
                    [account_key, *visible_ids],
                )
            conn.execute(
                """
                UPDATE operator_suppliers
                SET active = 0, updated_at = ?
                WHERE account_key = ?
                  AND COALESCE(active, 1) = 1
                  AND supplier_id NOT IN (
                      SELECT supplier_id
                      FROM account_suppliers
                      WHERE account_key = ? AND visible = 1
                  )
                """,
                (now, account_key, account_key),
            )
        return self.list_account_suppliers(account_key, include_hidden=True)

    def resolve_run_suppliers(
        self,
        *,
        operator_id: str,
        account_keys: list[str],
        suppliers: list[dict[str, Any]],
        run_kind: str = RUN_KIND_TASKS,
    ) -> list[dict[str, Any]]:
        if run_kind == RUN_KIND_SYNC_SUPPLIERS:
            return []
        if not operator_id:
            raise ValueError("请选择运营人员，再按其已分配的供应商执行")
        requested = self._normalize_supplier_payload(suppliers)
        had_input = any(
            isinstance(item, dict) and (item.get("supplier_id") or item.get("supplier_name"))
            for item in (suppliers or [])
        )
        if had_input and not requested:
            raise ValueError("所选不是真实供应商。右上角的「全部」不能用来执行任务，请勾选具体供应商。")
        runnable = self.list_runnable_suppliers(operator_id, account_keys)
        runnable_map = {(row["account_key"], row["supplier_id"]): row for row in runnable}
        source = requested or [
            {
                "account_key": row["account_key"],
                "supplier_id": row["supplier_id"],
                "supplier_name": row["supplier_name"],
            }
            for row in runnable
        ]
        resolved: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for item in source:
            account_key = item["account_key"] or (account_keys[0] if len(account_keys) == 1 else "")
            supplier_id = item["supplier_id"]
            key = (account_key, supplier_id)
            if not account_key:
                raise ValueError("供应商缺少猫超账户")
            if account_keys and account_key not in account_keys:
                raise ValueError(f"供应商不属于本次选择的猫超账户: {account_key}")
            if key in seen:
                continue
            master = runnable_map.get(key)
            if master is None:
                assigned = self.list_operator_suppliers(operator_id, account_key, active_only=True)
                assigned_ids = {row["supplier_id"] for row in assigned}
                if supplier_id not in assigned_ids:
                    raise ValueError(f"供应商不在该运营负责范围内: {item.get('supplier_name') or supplier_id}")
                raise ValueError(f"供应商当前不可见，不能作为新任务执行对象: {item.get('supplier_name') or supplier_id}")
            seen.add(key)
            resolved.append(
                {
                    "account_key": account_key,
                    "supplier_id": supplier_id,
                    "supplier_name": item.get("supplier_name") or master["supplier_name"],
                }
            )
        if not resolved:
            raise ValueError("请选择该运营已分配且当前可见的供应商")
        return resolved

    def list_runnable_suppliers(self, operator_id: str, account_keys: list[str] | None = None) -> list[dict[str, Any]]:
        rows = self.list_operator_suppliers(operator_id, active_only=True)
        result = []
        for row in rows:
            if account_keys and row["account_key"] not in account_keys:
                continue
            if not row.get("visible"):
                continue
            if _is_placeholder_supplier(row.get("supplier_id") or "", row.get("supplier_name") or ""):
                continue
            result.append(row)
        return result

    def list_operator_suppliers(
        self,
        operator_id: str,
        account_key: str = "",
        active_only: bool = True,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT os.operator_id, os.account_key, os.supplier_id, os.created_at,
                   COALESCE(os.active, 1) AS active,
                   COALESCE(s.supplier_name, os.supplier_id) AS supplier_name,
                   COALESCE(s.visible, 0) AS visible
            FROM operator_suppliers os
            LEFT JOIN account_suppliers s
              ON s.account_key = os.account_key AND s.supplier_id = os.supplier_id
            WHERE os.operator_id = ?
        """
        params: list[Any] = [operator_id]
        if account_key:
            query += " AND os.account_key = ?"
            params.append(account_key)
        if active_only:
            query += " AND COALESCE(os.active, 1) = 1"
        query += " ORDER BY os.account_key, supplier_name"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
            result = []
            for row in rows:
                if _is_placeholder_supplier(row["supplier_id"] or "", row["supplier_name"] or ""):
                    continue
                result.append(
                    {
                        "operator_id": row["operator_id"],
                        "account_key": row["account_key"],
                        "supplier_id": row["supplier_id"],
                        "supplier_name": row["supplier_name"],
                        "visible": bool(row["visible"]),
                        "active": bool(row["active"]),
                        "created_at": row["created_at"],
                    }
                )
            return result

    def set_operator_suppliers(
        self,
        operator_id: str,
        account_key: str,
        supplier_ids: list[str],
    ) -> list[dict[str, Any]]:
        if self.get_operator(operator_id) is None:
            raise KeyError(operator_id)
        wanted = []
        seen: set[str] = set()
        for value in supplier_ids:
            supplier_id = str(value or "").strip()
            if not supplier_id or supplier_id in seen:
                continue
            seen.add(supplier_id)
            wanted.append(supplier_id)
        now = self._now()
        with self._connect() as conn:
            active_syncs = conn.execute(
                """
                SELECT account_keys_json
                FROM runs
                WHERE run_kind = ? AND status IN ('pending', 'running', 'paused')
                """,
                (RUN_KIND_SYNC_SUPPLIERS,),
            ).fetchall()
            for row in active_syncs:
                try:
                    syncing_accounts = json.loads(row["account_keys_json"] or "[]")
                except json.JSONDecodeError:
                    syncing_accounts = []
                if account_key in syncing_accounts:
                    raise RuntimeError("账号供应商清单正在同步，请完成后再分配")

            rows = conn.execute(
                """
                SELECT supplier_id, supplier_name, visible
                FROM account_suppliers
                WHERE account_key = ?
                """,
                (account_key,),
            ).fetchall()
            synced = {
                row["supplier_id"]: {
                    "supplier_name": row["supplier_name"],
                    "visible": bool(row["visible"]),
                }
                for row in rows
            }
            for supplier_id in wanted:
                master = synced.get(supplier_id)
                if master is None:
                    raise ValueError(f"只能勾选已同步到该猫超账户的供应商: {supplier_id}")
                if not master["visible"]:
                    raise ValueError(f"供应商当前不可见，不能作为新的负责对象: {master['supplier_name'] or supplier_id}")

            conn.execute(
                """
                UPDATE operator_suppliers
                SET active = 0, updated_at = ?
                WHERE operator_id = ? AND account_key = ? AND COALESCE(active, 1) = 1
                """,
                (now, operator_id, account_key),
            )
            for supplier_id in wanted:
                conn.execute(
                    """
                    INSERT INTO operator_suppliers (operator_id, account_key, supplier_id, created_at, active, updated_at)
                    VALUES (?, ?, ?, ?, 1, ?)
                    ON CONFLICT(operator_id, account_key, supplier_id) DO UPDATE SET
                        active = 1,
                        updated_at = excluded.updated_at
                    """,
                    (operator_id, account_key, supplier_id, now, now),
                )
        return self.list_operator_suppliers(operator_id, account_key, active_only=True)

    def get_account_supplier(self, account_key: str, supplier_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT account_key, supplier_id, supplier_name, visible, last_synced_at
                FROM account_suppliers
                WHERE account_key = ? AND supplier_id = ?
                """,
                (account_key, supplier_id),
            ).fetchone()
            if row is None:
                return None
            item = dict(row)
            item["visible"] = bool(item["visible"])
            return item

    def build_assignment_snapshot(
        self,
        account_keys: list[str],
        suppliers: list[dict[str, Any]],
        operator_id: str = "",
        operator_name: str = "",
    ) -> list[dict[str, Any]]:
        snapshot: list[dict[str, Any]] = []
        for item in suppliers:
            account_key = str(item.get("account_key") or "")
            if not account_key and len(account_keys) == 1:
                account_key = account_keys[0]
            supplier_id = str(item.get("supplier_id") or "")
            supplier_name = str(item.get("supplier_name") or "")
            owners = self.list_supplier_operators(account_key, supplier_id) if account_key and supplier_id else []
            snapshot.append(
                {
                    "account_key": account_key,
                    "supplier_id": supplier_id,
                    "supplier_name": supplier_name,
                    "operators": owners,
                }
            )
        return snapshot

    def record_run_file_ownership(self, run_id: str, results: list[dict[str, Any]], snapshot: list[dict[str, Any]] | None = None) -> None:
        owners_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
        names_by_key: dict[tuple[str, str], str] = {}
        for item in snapshot or []:
            key = (str(item.get("account_key") or ""), str(item.get("supplier_id") or ""))
            owners_by_key[key] = list(item.get("operators") or [])
            names_by_key[key] = str(item.get("supplier_name") or "")
        now = self._now()
        with self._connect() as conn:
            conn.execute("DELETE FROM run_file_ownership WHERE run_id = ?", (run_id,))
            for result in results:
                account_key = str(result.get("account") or result.get("account_key") or "")
                supplier_id = str(result.get("supplier_id") or "")
                supplier_name = str(result.get("supplier_name") or names_by_key.get((account_key, supplier_id), ""))
                task_key = str(result.get("task") or result.get("task_key") or "")
                owners = owners_by_key.get((account_key, supplier_id), [])
                if not owners:
                    owners = [{"operator_id": "", "name": "未分配"}]
                for owner in owners:
                    conn.execute(
                        """
                        INSERT INTO run_file_ownership (
                            run_id, account_key, supplier_id, supplier_name, task_key,
                            operator_id, operator_name, raw_file, cleaned_file, status, note, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            account_key,
                            supplier_id,
                            supplier_name,
                            task_key,
                            str(owner.get("operator_id") or ""),
                            str(owner.get("name") or ""),
                            str(result.get("raw_file") or ""),
                            str(result.get("cleaned_file") or ""),
                            str(result.get("status") or ""),
                            str(result.get("note") or ""),
                            now,
                        ),
                    )

    def list_file_ownership(self, run_id: str = "") -> list[dict[str, Any]]:
        query = """
            SELECT run_id, account_key, supplier_id, supplier_name, task_key,
                   operator_id, operator_name, raw_file, cleaned_file, status, note, created_at
            FROM run_file_ownership
        """
        params: list[Any] = []
        if run_id:
            query += " WHERE run_id = ?"
            params.append(run_id)
        query += " ORDER BY operator_name, supplier_name, task_key, created_at"
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def list_files(self) -> list[dict[str, Any]]:
        physical: dict[str, dict[str, Any]] = {}
        try:
            physical_paths = list(self.settings.data_root.rglob("*"))
        except OSError:
            physical_paths = []
        for path in physical_paths:
            if not path.is_file():
                continue
            resolved = str(path.resolve())
            physical[resolved] = {
                "file_id": str(path.relative_to(self.settings.data_root)),
                "name": path.name,
                "path": str(path),
                "size": path.stat().st_size,
                "updated_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
            }
        ownership_rows = self.list_file_ownership()
        owned_paths: set[str] = set()
        rows: list[dict[str, Any]] = []
        for item in ownership_rows:
            for field in ("cleaned_file", "raw_file"):
                value = str(item.get(field) or "")
                if not value:
                    continue
                path = Path(value).expanduser()
                try:
                    resolved = str(path.resolve())
                except OSError:
                    continue
                owned_paths.add(resolved)
                file_meta = physical.get(resolved)
                if file_meta is None:
                    continue
                rows.append(
                    {
                        **file_meta,
                        "kind": "cleaned" if field == "cleaned_file" else "raw",
                        "run_id": item["run_id"],
                        "account_key": item["account_key"],
                        "supplier_id": item["supplier_id"],
                        "supplier_name": item["supplier_name"],
                        "task_key": item["task_key"],
                        "operator_id": item["operator_id"],
                        "operator_name": item["operator_name"],
                        "status": item["status"],
                        "note": item["note"],
                    }
                )
        for resolved, file_meta in physical.items():
            if resolved in owned_paths:
                continue
            parts = Path(file_meta["file_id"]).parts
            kind = "raw" if "raw" in parts else "cleaned" if "cleaned" in parts else "文件"
            rows.append(
                {
                    **file_meta,
                    "kind": kind,
                    "run_id": "",
                    "account_key": "",
                    "supplier_id": "",
                    "supplier_name": "",
                    "task_key": "",
                    "operator_id": "",
                    "operator_name": "未分配",
                    "status": "",
                    "note": "",
                }
            )
        task_order = {key: idx for idx, key in enumerate(TASKS)}
        rows.sort(
            key=lambda item: (
                str(item.get("operator_name") or "未分配"),
                str(item.get("supplier_name") or item.get("supplier_id") or ""),
                task_order.get(str(item.get("task_key") or ""), 99),
                str(item.get("updated_at") or ""),
            )
        )
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
