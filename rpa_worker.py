from __future__ import annotations

import contextlib
import io
import json
import time
import traceback
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from backend_core import BackendStore, DEFAULT_CONFIG_PATH
from maochao_rpa import MaochaoRPA, load_settings


POLL_INTERVAL_SEC = 2


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _pending_run(store: BackendStore) -> dict[str, Any] | None:
    for run in reversed(store.list_runs()):
        if run["status"] == "pending":
            return run
    return None


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
    run = _pending_run(store)
    if run is None:
        return False

    run_id = run["run_id"]
    log_path = store.settings.log_dir / f"{run_id}.log"
    accounts = _accounts_for_run(store, run)
    started_at = _now()
    store.update_run(run_id, status="running", started_at=started_at)

    try:
        store.lock_accounts(run_id, accounts)
        settings = load_settings(DEFAULT_CONFIG_PATH)
        rpa = MaochaoRPA(settings, headless=not run["headed"])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            results = rpa.run(
                run["task_keys"],
                run["account_keys"],
                force_account_tasks=run["force_account_tasks"],
            )
        log_path.write_text(buffer.getvalue(), encoding="utf-8")
        result_items = [asdict(item) for item in results]
        failed = [item for item in result_items if item.get("status") != "ok"]
        store.update_run(
            run_id,
            status="failed" if failed else "succeeded",
            finished_at=_now(),
            result_json=json.dumps(result_items, ensure_ascii=False),
            error="",
        )
        return True
    except Exception as exc:
        message = f"{exc}\n{traceback.format_exc()}"
        with log_path.open("a", encoding="utf-8") as f:
            f.write(message)
        store.update_run(run_id, status="failed", finished_at=_now(), error=message)
        return True
    finally:
        store.unlock_accounts(run_id)


def main() -> None:
    store = BackendStore(DEFAULT_CONFIG_PATH)
    while True:
        did_work = run_once(store)
        if not did_work:
            time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
