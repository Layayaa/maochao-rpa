from __future__ import annotations

import contextlib
import io
import json
import os
import threading
import time
import traceback
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from backend_core import RUN_KIND_SYNC_SUPPLIERS, BackendStore, DEFAULT_CONFIG_PATH
from maochao_rpa import MaochaoRPA, is_placeholder_supplier, load_settings


POLL_INTERVAL_SEC = 2
HEARTBEAT_INTERVAL_SEC = 2
SCHEDULE_INTERVAL_SEC = 5


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _write_heartbeat(store: BackendStore) -> None:
    payload = {
        "pid": os.getpid(),
        "updated_at": _now(),
    }
    store.worker_heartbeat_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


class _LiveLog:
    """Write worker stdout/stderr to the run log immediately, not after the run ends."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("a", encoding="utf-8")
        self._buf = io.StringIO()

    def write(self, s: str) -> int:
        self._buf.write(s)
        self._file.write(s)
        self._file.flush()
        return len(s)

    def flush(self) -> None:
        self._file.flush()

    def getvalue(self) -> str:
        return self._buf.getvalue()

    def close(self) -> None:
        try:
            self._file.close()
        except Exception:
            pass


def _pending_run(store: BackendStore) -> dict[str, Any] | None:
    return store.next_pending_run()


def _accounts_for_run(store: BackendStore, run: dict[str, Any]) -> list[dict[str, Any]]:
    wanted = set(run["account_keys"])
    if run.get("run_kind") != RUN_KIND_SYNC_SUPPLIERS and run.get("suppliers"):
        supplier_accounts = {
            str(item.get("account_key") or "")
            for item in run.get("suppliers") or []
            if item.get("account_key")
        }
        if supplier_accounts:
            wanted = supplier_accounts
    accounts = store.list_accounts()
    if not wanted:
        return accounts
    selected = [account for account in accounts if account["key"] in wanted]
    missing = sorted(wanted - {account["key"] for account in selected})
    if missing:
        raise KeyError(f"账号不存在或未启用: {', '.join(missing)}")
    return selected


def _executable_suppliers(store: BackendStore, run: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    executable: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for item in run.get("suppliers") or []:
        account_key = str(item.get("account_key") or "")
        supplier_id = str(item.get("supplier_id") or "")
        supplier_name = str(item.get("supplier_name") or supplier_id)
        if is_placeholder_supplier(supplier_id, supplier_name):
            skipped.append(
                {
                    "task": "__supplier__",
                    "title": f"供应商 {supplier_name}",
                    "account": account_key,
                    "status": "failed",
                    "supplier_id": supplier_id,
                    "supplier_name": supplier_name,
                    "error": "「全部」不是真实供应商，不能作为本次执行对象。请重新同步右上角并勾选具体供应商。",
                    "note": "占位供应商已跳过",
                    "raw_file": "",
                    "cleaned_file": "",
                }
            )
            continue
        master = store.get_account_supplier(account_key, supplier_id) if account_key and supplier_id else None
        if master is None or not master.get("visible"):
            skipped.append(
                {
                    "task": "__supplier__",
                    "title": f"供应商 {supplier_name}",
                    "account": account_key,
                    "status": "failed",
                    "supplier_id": supplier_id,
                    "supplier_name": supplier_name,
                    "error": "供应商当前不可见，已跳过该供应商，未使用其他供应商的文件",
                    "note": "执行时已不可见，不能作为本次执行对象",
                    "raw_file": "",
                    "cleaned_file": "",
                }
            )
            continue
        executable.append(
            {
                "account_key": account_key,
                "supplier_id": supplier_id,
                "supplier_name": str(master.get("supplier_name") or item.get("supplier_name") or supplier_id),
            }
        )
    return executable, skipped


def _is_browser_resource_busy(exc: Exception) -> bool:
    message = str(exc)
    return all(token in message for token in ("account=", "port=", "profile="))


def _has_active_desktop_session() -> bool:
    """Only drive a browser from a Windows session that is currently visible."""
    if os.name != "nt":
        return True

    import ctypes
    from ctypes import wintypes

    session_id = wintypes.DWORD()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session_id)):
        return False

    wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
    buffer = ctypes.c_void_p()
    bytes_returned = wintypes.DWORD()
    try:
        ok = wtsapi32.WTSQuerySessionInformationW(
            None,
            session_id.value,
            8,  # WTSConnectState
            ctypes.byref(buffer),
            ctypes.byref(bytes_returned),
        )
        if not ok or bytes_returned.value < ctypes.sizeof(wintypes.DWORD):
            return False
        state = ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD)).contents.value
        return state == 0  # WTSActive
    finally:
        if buffer:
            wtsapi32.WTSFreeMemory(buffer)


def run_once(store: BackendStore) -> bool:
    run = store.claim_next_pending_run()
    if run is None:
        return False

    run_id = run["run_id"]
    if run.get("headed", True) and not _has_active_desktop_session():
        store.requeue_run(run_id)
        store.unlock_accounts(run_id)
        return False

    log_path = store.settings.log_dir / f"{run_id}.log"
    accounts = _accounts_for_run(store, run)

    buffer = _LiveLog(log_path)
    try:
        try:
            store.lock_accounts(run_id, accounts)
        except RuntimeError as exc:
            if _is_browser_resource_busy(exc):
                store.requeue_run(run_id)
                return False
            raise
        settings = load_settings(DEFAULT_CONFIG_PATH)
        rpa = MaochaoRPA(settings, headless=not run["headed"])
        should_stop = lambda: store.is_pause_requested(run_id) or store.is_cancel_requested(run_id)
        completed = {
            (
                str(item.get("account") or ""),
                str(item.get("supplier_id") or ""),
                str(item.get("task") or ""),
            )
            for item in run.get("result", [])
            if item.get("status") == "ok"
        }
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            if run.get("run_kind") == RUN_KIND_SYNC_SUPPLIERS:
                synced = rpa.sync_header_suppliers(run["account_keys"])
                by_account: dict[str, list[dict[str, Any]]] = {}
                for item in synced:
                    by_account.setdefault(str(item.get("account_key") or ""), []).append(item)
                for account_key, rows in by_account.items():
                    store.upsert_account_suppliers(account_key, rows)
                result_items = [
                    {
                        "task": "__sync_suppliers__",
                        "title": "同步右上角供应商",
                        "account": item.get("account_key") or "",
                        "status": "ok",
                        "supplier_id": item.get("supplier_id") or "",
                        "supplier_name": item.get("supplier_name") or "",
                        "note": "已同步右上角可见供应商",
                    }
                    for item in synced
                ]
            else:
                executable, skipped = _executable_suppliers(store, run)
                results: list[Any] = []
                if executable:
                    executable_account_keys = list(dict.fromkeys(
                        str(item.get("account_key") or "")
                        for item in executable
                        if item.get("account_key")
                    ))
                    results = rpa.run(
                        run["task_keys"],
                        executable_account_keys or run["account_keys"],
                        force_account_tasks=True,
                        should_pause=should_stop,
                        skip_completed=completed,
                        suppliers=executable,
                        operator_name=str(run.get("operator_name") or ""),
                        item_ids_by_supplier={
                            (str(item.get("account_key") or ""), str(item.get("supplier_id") or "")):
                                store.list_item_ids(
                                    str(item.get("account_key") or ""),
                                    str(item.get("supplier_id") or ""),
                                )
                            for item in executable
                        },
                    )
                merged_results: dict[tuple[str, str, str], dict[str, Any]] = {}
                for item in run.get("result", []):
                    key = (
                        str(item.get("account") or ""),
                        str(item.get("supplier_id") or ""),
                        str(item.get("task") or ""),
                    )
                    merged_results[key] = item
                for item in skipped:
                    key = (
                        str(item.get("account") or ""),
                        str(item.get("supplier_id") or ""),
                        str(item.get("task") or ""),
                    )
                    merged_results[key] = item
                for item in results:
                    result_item = asdict(item)
                    key = (
                        str(result_item.get("account") or ""),
                        str(result_item.get("supplier_id") or ""),
                        str(result_item.get("task") or ""),
                    )
                    merged_results[key] = result_item
                result_items = list(merged_results.values())
                store.record_run_file_ownership(run_id, result_items, run.get("assignment_snapshot") or [])
        cancel_requested = store.is_cancel_requested(run_id)
        pause_requested = store.is_pause_requested(run_id)
        manual_login_pending = (
            rpa.last_run_paused
            and getattr(rpa, "last_run_pause_reason", "") == "manual_login"
        )
        if cancel_requested:
            store.update_run(
                run_id,
                status="cancelled",
                finished_at=_now(),
                pause_requested=False,
                cancel_requested=False,
                result_json=json.dumps(result_items, ensure_ascii=False),
                error="用户取消运行中任务",
            )
            return True
        if manual_login_pending:
            store.update_run(
                run_id,
                status="paused",
                finished_at="",
                pause_requested=False,
                cancel_requested=False,
                result_json=json.dumps(result_items, ensure_ascii=False),
                error="已填写账号密码，等待人工完成滑块验证和登录后点击继续",
            )
            return True
        if rpa.last_run_paused and not pause_requested:
            # A resume request arrived after the RPA observed pause_requested.
            # Keep completed results and let the worker claim the run again.
            store.update_run(
                run_id,
                result_json=json.dumps(result_items, ensure_ascii=False),
            )
            store.requeue_run(run_id)
            return True
        if pause_requested:
            store.update_run(
                run_id,
                status="paused",
                finished_at="",
                pause_requested=False,
                cancel_requested=False,
                result_json=json.dumps(result_items, ensure_ascii=False),
                error="任务已暂停，可点击继续运行",
            )
            return True
        failed = [item for item in result_items if item.get("status") != "ok"]
        store.update_run(
            run_id,
            status="failed" if failed else "succeeded",
            finished_at=_now(),
            pause_requested=False,
            cancel_requested=False,
            result_json=json.dumps(result_items, ensure_ascii=False),
            error="",
        )
        store.enqueue_auto_retry_runs(run_id, result_items)
        return True
    except Exception as exc:
        message = f"{exc}\n{traceback.format_exc()}"
        try:
            buffer.write(message if message.endswith("\n") else message + "\n")
        except Exception:
            with log_path.open("a", encoding="utf-8") as f:
                f.write(message if message.endswith("\n") else message + "\n")
        store.update_run(run_id, status="failed", finished_at=_now(), pause_requested=False, cancel_requested=False, error=message)
        store.enqueue_auto_retry_runs(run_id, run.get("result") or [])
        return True
    finally:
        try:
            buffer.close()
        except Exception:
            pass
        store.unlock_accounts(run_id)


def main() -> None:
    store = BackendStore(DEFAULT_CONFIG_PATH)
    stop_event = threading.Event()

    def heartbeat_loop() -> None:
        while not stop_event.is_set():
            try:
                _write_heartbeat(store)
            except Exception:
                pass
            stop_event.wait(HEARTBEAT_INTERVAL_SEC)

    heartbeat_thread = threading.Thread(target=heartbeat_loop, name="worker-heartbeat", daemon=True)
    heartbeat_thread.start()

    def schedule_loop() -> None:
        while not stop_event.is_set():
            try:
                store.enqueue_due_schedules()
            except Exception:
                pass
            stop_event.wait(SCHEDULE_INTERVAL_SEC)

    schedule_thread = threading.Thread(target=schedule_loop, name="task-scheduler", daemon=True)
    schedule_thread.start()
    try:
        while True:
            did_work = run_once(store)
            if not did_work:
                time.sleep(POLL_INTERVAL_SEC)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()


if __name__ == "__main__":
    main()
