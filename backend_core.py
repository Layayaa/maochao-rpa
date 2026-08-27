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
from maochao_rpa import TASKS, is_placeholder_supplier as _is_placeholder_supplier, load_settings


BASE = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE / "config.local.json"
DB_PATH = BASE / "backend" / "rpa.db"
WORK_DIR = BASE / "backend"
WORKER_HEARTBEAT_PATH = WORK_DIR / "worker_heartbeat.json"


SYNC_SUPPLIERS_TASK = "__sync_suppliers__"
RUN_KIND_TASKS = "tasks"
RUN_KIND_SYNC_SUPPLIERS = "sync_suppliers"


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
        self.settings.output_root.mkdir(parents=True, exist_ok=True)

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
                    error TEXT NOT NULL DEFAULT '',
                    result_json TEXT NOT NULL DEFAULT '[]'
                )
                """
            )
            self._ensure_column(conn, "runs", "queue_position", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "runs", "pause_requested", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "runs", "suppliers_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(conn, "runs", "operator_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "runs", "operator_name", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "runs", "run_kind", "TEXT NOT NULL DEFAULT 'tasks'")
            self._ensure_column(conn, "runs", "assignment_snapshot_json", "TEXT NOT NULL DEFAULT '[]'")
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
                    created_at TEXT NOT NULL
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
            self._ensure_column(conn, "operator_suppliers", "active", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column(conn, "operator_suppliers", "updated_at", "TEXT NOT NULL DEFAULT ''")
            self._normalize_pending_queue(conn)

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

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

    def normalize_pending_queue(self) -> None:
        with self._connect() as conn:
            self._normalize_pending_queue(conn)

    def _next_queue_position(self, conn: sqlite3.Connection) -> int:
        value = conn.execute("SELECT COALESCE(MAX(queue_position), 0) + 1 FROM runs WHERE status = 'pending'").fetchone()[0]
        return int(value)

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
            )
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, task_keys_json, account_keys_json, status, created_at, updated_at,
                    started_at, finished_at, force_account_tasks, headed, queue_position,
                    pause_requested, error, result_json, suppliers_json, operator_id,
                    operator_name, run_kind, assignment_snapshot_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            "error": row["error"],
            "result": json.loads(row["result_json"] or "[]"),
            "suppliers": json.loads(row["suppliers_json"] or "[]") if "suppliers_json" in keys else [],
            "operator_id": row["operator_id"] if "operator_id" in keys else "",
            "operator_name": row["operator_name"] if "operator_name" in keys else "",
            "run_kind": row["run_kind"] if "run_kind" in keys else RUN_KIND_TASKS,
            "assignment_snapshot": json.loads(row["assignment_snapshot_json"] or "[]") if "assignment_snapshot_json" in keys else [],
        }

    def claim_next_pending_run(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            self._normalize_pending_queue(conn)
            row = conn.execute(
                """
                SELECT * FROM runs
                WHERE status = 'pending'
                ORDER BY queue_position, created_at
                LIMIT 1
                """
            ).fetchone()
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
            self._normalize_pending_queue(conn)
            claimed = conn.execute("SELECT * FROM runs WHERE run_id = ?", (row["run_id"],)).fetchone()
            return self._run_row(claimed) if claimed is not None else None

    def next_pending_run(self) -> dict[str, Any] | None:
        return self.claim_next_pending_run()

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

    def cancel_pending_run(self, run_id: str, reason: str = "用户取消排队") -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT status FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["status"] not in {"pending", "paused"}:
                raise RuntimeError("只能取消排队中或已暂停的任务")
            now = self._now()
            cancel_reason = "用户取消已暂停任务" if row["status"] == "paused" and reason == "用户取消排队" else reason
            conn.execute(
                """
                UPDATE runs
                SET status = 'cancelled',
                    updated_at = ?,
                    finished_at = ?,
                    queue_position = 0,
                    pause_requested = 0,
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

    def list_operators(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT operator_id, name, created_at FROM operators ORDER BY created_at, name"
            ).fetchall()
            return [dict(row) for row in rows]

    def get_operator(self, operator_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT operator_id, name, created_at FROM operators WHERE operator_id = ?",
                (operator_id,),
            ).fetchone()
            return dict(row) if row else None

    def create_operator(self, name: str) -> dict[str, Any]:
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
                "INSERT INTO operators (operator_id, name, created_at) VALUES (?, ?, ?)",
                (operator_id, name, now),
            )
        operator = self.get_operator(operator_id)
        if operator is None:
            raise RuntimeError("创建运营人员失败")
        return operator

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
        synced = {row["supplier_id"]: row for row in self.list_account_suppliers(account_key, include_hidden=True)}
        for supplier_id in wanted:
            master = synced.get(supplier_id)
            if master is None:
                raise ValueError(f"只能勾选已同步到该猫超账户的供应商: {supplier_id}")
            if not master.get("visible"):
                raise ValueError(f"供应商当前不可见，不能作为新的负责对象: {master.get('supplier_name') or supplier_id}")
        now = self._now()
        with self._connect() as conn:
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
        output_root = self.settings.output_root
        for path in output_root.rglob("*"):
            if not path.is_file():
                continue
            resolved = str(path.resolve())
            physical[resolved] = {
                "file_id": str(path.relative_to(output_root)),
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
