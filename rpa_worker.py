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

from backend_core import BackendStore, DEFAULT_CONFIG_PATH
from maochao_rpa import MaochaoRPA, load_settings


POLL_INTERVAL_SEC = 2
HEARTBEAT_INTERVAL_SEC = 2


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


def _pending_run(store: BackendStore) -> dict[str, Any] | None:
    return store.next_pending_run()


def _accounts_for_run(store: BackendStore, run: dict[str, Any]) -> list[dict[str, Any]]:
    wanted = set(run["account_keys"])
    accounts = store.list_accounts()
    if not wanted:
        return accounts
    selected = [account for account in accounts if account["key"] in wanted]
    missing = sorted(wanted - {account["key"] for account in selected})
    if missing:
        raise KeyError(f"账号不存在或未启用: {', '.join(missing)}")
    return selected


def run_once(store: BackendStore) -> bool:
    run = store.claim_next_pending_run()
    if run is None:
        return False

    run_id = run["run_id"]
    log_path = store.settings.log_dir / f"{run_id}.log"
    accounts = _accounts_for_run(store, run)

    try:
        store.lock_accounts(run_id, accounts)
        settings = load_settings(DEFAULT_CONFIG_PATH)
        rpa = MaochaoRPA(settings, headless=not run["headed"])
        completed = {
            (str(item.get("account") or ""), str(item.get("task") or ""))
            for item in run.get("result", [])
            if item.get("status") == "ok"
        }
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            results = rpa.run(
                run["task_keys"],
                run["account_keys"],
                force_account_tasks=run["force_account_tasks"],
                should_pause=lambda: store.is_pause_requested(run_id),
                skip_completed=completed,
            )
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(buffer.getvalue())
        merged_results: dict[tuple[str, str], dict[str, Any]] = {}
        for item in run.get("result", []):
            key = (str(item.get("account") or ""), str(item.get("task") or ""))
            merged_results[key] = item
        for item in results:
            result_item = asdict(item)
            key = (str(result_item.get("account") or ""), str(result_item.get("task") or ""))
            merged_results[key] = result_item
        result_items = list(merged_results.values())
        pause_requested = rpa.last_run_paused or store.is_pause_requested(run_id)
        if pause_requested:
            store.update_run(
                run_id,
                status="paused",
                finished_at="",
                pause_requested=False,
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
            result_json=json.dumps(result_items, ensure_ascii=False),
            error="",
        )
        return True
    except Exception as exc:
        message = f"{exc}\n{traceback.format_exc()}"
        with log_path.open("a", encoding="utf-8") as f:
            f.write(message)
        store.update_run(run_id, status="failed", finished_at=_now(), pause_requested=False, error=message)
        return True
    finally:
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
