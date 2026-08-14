from __future__ import annotations

import json

from backend_core import BackendStore, DEFAULT_CONFIG_PATH


def _ensure_operator(store: BackendStore, account_key: str) -> tuple[str, list[dict]]:
    operators = store.list_operators()
    operator = next((item for item in operators if item["name"] == "selftest"), None)
    if operator is None:
        operator = store.create_operator("selftest")
    suppliers = [{"account_key": account_key, "supplier_id": "selftest-supplier", "supplier_name": "自测供应商"}]
    store.upsert_account_suppliers(account_key, suppliers)
    store.set_operator_suppliers(operator["operator_id"], account_key, [suppliers[0]["supplier_id"]])
    return operator["operator_id"], suppliers


def main() -> int:
    store = BackendStore(DEFAULT_CONFIG_PATH)
    tasks = store.list_tasks()
    accounts = store.list_accounts()
    if len(tasks) != 6:
        raise RuntimeError(f"任务数量异常: {len(tasks)}")
    if not accounts:
        raise RuntimeError("没有可用账号")

    operator_id, suppliers = _ensure_operator(store, accounts[0]["key"])
    run = store.create_run(
        task_keys=[tasks[0]["task_key"]],
        account_keys=[accounts[0]["key"]],
        force_account_tasks=True,
        headed=True,
        operator_id=operator_id,
        suppliers=suppliers,
    )
    if not run.get("suppliers"):
        raise RuntimeError("create_run 未写入运营已分配供应商")
    store.lock_accounts(run["run_id"], [accounts[0]])
    other = None
    try:
        try:
            other = store.create_run(
                task_keys=[tasks[0]["task_key"]],
                account_keys=[accounts[0]["key"]],
                force_account_tasks=True,
                headed=True,
                operator_id=operator_id,
                suppliers=suppliers,
            )
            store.lock_accounts(other["run_id"], [accounts[0]])
            raise RuntimeError("账号锁未生效")
        except RuntimeError as exc:
            if "浏览器资源已被占用" not in str(exc):
                raise
    finally:
        store.unlock_accounts(run["run_id"])
        store.update_run(
            run["run_id"],
            status="cancelled",
            finished_at=store._now(),
            result_json=json.dumps([], ensure_ascii=False),
            error="backend_selftest: no RPA execution",
        )
        if other is not None:
            store.update_run(
                other["run_id"],
                status="cancelled",
                finished_at=store._now(),
                result_json=json.dumps([], ensure_ascii=False),
                error="backend_selftest: lock collision verified",
            )

    try:
        store.create_run(
            task_keys=[tasks[0]["task_key"]],
            account_keys=[accounts[0]["key"]],
            force_account_tasks=True,
            headed=True,
        )
        raise RuntimeError("未分配供应商时不应创建 run")
    except ValueError as exc:
        if "运营" not in str(exc):
            raise

    files = store.list_files()
    print(f"tasks={len(tasks)} accounts={len(accounts)} files={len(files)} lock=ok assigned_suppliers=ok")

    try:
        store.set_operator_suppliers(operator_id, accounts[0]["key"], ["not-synced-supplier"])
        raise RuntimeError("未同步供应商不应允许勾选")
    except ValueError as exc:
        if "已同步" not in str(exc):
            raise

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
