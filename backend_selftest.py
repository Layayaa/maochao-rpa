from __future__ import annotations

from backend_core import BackendStore, DEFAULT_CONFIG_PATH


def main() -> int:
    store = BackendStore(DEFAULT_CONFIG_PATH)
    tasks = store.list_tasks()
    accounts = store.list_accounts()
    if len(tasks) != 6:
        raise RuntimeError(f"任务数量异常: {len(tasks)}")
    if not accounts:
        raise RuntimeError("没有可用账号")

    run = store.create_run(
        task_keys=[tasks[0]["task_key"]],
        account_keys=[accounts[0]["key"]],
        force_account_tasks=True,
        headed=True,
    )
    store.lock_accounts(run["run_id"], [accounts[0]])
    try:
        try:
            other = store.create_run(
                task_keys=[tasks[0]["task_key"]],
                account_keys=[accounts[0]["key"]],
                force_account_tasks=True,
                headed=True,
            )
            store.lock_accounts(other["run_id"], [accounts[0]])
            raise RuntimeError("账号锁未生效")
        except RuntimeError as exc:
            if "浏览器资源已被占用" not in str(exc):
                raise
    finally:
        store.unlock_accounts(run["run_id"])

    files = store.list_files()
    print(f"tasks={len(tasks)} accounts={len(accounts)} files={len(files)} lock=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
