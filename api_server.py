from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from account_store import AccountStore
from backend_core import BackendStore, DEFAULT_CONFIG_PATH
from maochao_rpa import load_settings, normalize_task_name, selected_tasks


app = FastAPI(title="Maochao RPA Backend", version="0.1.0")
store = BackendStore(DEFAULT_CONFIG_PATH)


class AccountPayload(BaseModel):
    key: str
    name: str | None = None
    username: str | None = None
    password: str | None = None
    port: int
    profile_dir: str | None = None
    download_dir: str | None = None
    supplier_names: list[str] = Field(default_factory=list)
    tasks: list[str] = Field(default_factory=list)
    note: str | None = ""
    xpath_vars: dict[str, Any] = Field(default_factory=dict)
    selector_overrides: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class AccountPatch(BaseModel):
    name: str | None = None
    username: str | None = None
    password: str | None = None
    port: int | None = None
    profile_dir: str | None = None
    download_dir: str | None = None
    supplier_names: list[str] | None = None
    tasks: list[str] | None = None
    note: str | None = None
    xpath_vars: dict[str, Any] | None = None
    selector_overrides: dict[str, Any] | None = None
    enabled: bool | None = None


class RunCreate(BaseModel):
    task_keys: list[str] = Field(default_factory=list)
    account_keys: list[str] = Field(default_factory=list)
    force_account_tasks: bool = False
    headed: bool = True


def _public_account(account: dict[str, Any]) -> dict[str, Any]:
    item = dict(account)
    item["username_set"] = bool(item.get("username"))
    item["password_set"] = bool(item.get("password"))
    item["username"] = item.get("username") or ""
    item.pop("password", None)
    return item


def _resolve_file_id(root: Path, file_id: str) -> Path:
    path = (root / file_id).resolve()
    if not path.is_file() or root.resolve() not in path.parents:
        raise HTTPException(status_code=404, detail="file not found")
    return path


@app.get("/health")
@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "pid": os.getpid(),
        "config": str(store.config_path),
        "db": str(store.settings.accounts_db_path),
        "tasks": len(store.list_tasks()),
    }


@app.get("/api/ready")
def ready() -> dict[str, Any]:
    checks = {
        "config_exists": store.config_path.exists(),
        "accounts_db_exists": store.settings.accounts_db_path.exists(),
        "data_root_exists": store.settings.data_root.exists(),
        "log_dir_exists": store.settings.log_dir.exists(),
        "screenshot_dir_exists": store.settings.screenshot_dir.exists(),
    }
    return {"status": "ok" if all(checks.values()) else "degraded", "checks": checks}


@app.get("/api/tasks")
def tasks() -> list[dict[str, Any]]:
    return store.list_tasks()


@app.get("/api/accounts")
def accounts(include_disabled: bool = False) -> list[dict[str, Any]]:
    return [_public_account(account) for account in store.list_accounts(include_disabled=include_disabled)]


@app.post("/api/accounts")
def create_account(payload: AccountPayload) -> dict[str, Any]:
    account_store = AccountStore(store.settings.accounts_db_path, store.settings.accounts_db_key_path)
    data = payload.dict()
    data["tasks"] = [normalize_task_name(task) for task in data["tasks"]]
    account_store.upsert_account(data, base_dir=store.config_path.parent)
    store.settings = load_settings(store.config_path)
    return {"status": "ok", "account_key": payload.key}


@app.patch("/api/accounts/{account_key}")
def patch_account(account_key: str, payload: AccountPatch) -> dict[str, Any]:
    existing = None
    for account in store.list_accounts(include_disabled=True):
        if account["key"] == account_key:
            existing = account
            break
    if existing is None:
        raise HTTPException(status_code=404, detail="account not found")
    data = dict(existing)
    for key, value in payload.dict(exclude_unset=True).items():
        if value is not None:
            data[key] = value
    data["tasks"] = [normalize_task_name(task) for task in data.get("tasks", [])]
    account_store = AccountStore(store.settings.accounts_db_path, store.settings.accounts_db_key_path)
    account_store.upsert_account(data, base_dir=store.config_path.parent)
    store.settings = load_settings(store.config_path)
    return {"status": "ok", "account_key": account_key}


@app.post("/api/runs")
def create_run(payload: RunCreate) -> dict[str, Any]:
    try:
        task_keys = selected_tasks(payload.task_keys)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    account_keys = payload.account_keys
    if not account_keys:
        account_keys = [account["key"] for account in store.list_accounts()]
    return store.create_run(task_keys, account_keys, payload.force_account_tasks, payload.headed)


@app.get("/api/runs")
def runs() -> list[dict[str, Any]]:
    return store.list_runs()


@app.get("/api/runs/{run_id}")
def run(run_id: str) -> dict[str, Any]:
    try:
        return store.get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc


@app.get("/api/runs/{run_id}/logs")
def run_logs(run_id: str) -> PlainTextResponse:
    log_path = store.settings.log_dir / f"{run_id}.log"
    if not log_path.exists():
        events = store.list_task_events(run_id)
        return PlainTextResponse("\n".join(json.dumps(event, ensure_ascii=False) for event in events))
    return PlainTextResponse(log_path.read_text(encoding="utf-8", errors="replace"))


@app.get("/api/runs/{run_id}/errors")
def run_errors(run_id: str) -> list[dict[str, Any]]:
    try:
        run_item = store.get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    errors = [item for item in run_item.get("result", []) if item.get("status") != "ok" or item.get("error")]
    if run_item.get("error"):
        errors.append({"run_id": run_id, "error": run_item["error"]})
    return errors


@app.get("/api/errors")
def errors() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for run_item in store.list_runs():
        if run_item.get("error"):
            rows.append({"run_id": run_item["run_id"], "error": run_item["error"], "updated_at": run_item["updated_at"]})
        for result in run_item.get("result", []):
            if result.get("status") != "ok" or result.get("error"):
                rows.append({"run_id": run_item["run_id"], **result})
    return rows


@app.get("/api/files")
def files() -> list[dict[str, Any]]:
    return store.list_files()


@app.get("/api/files/{file_id:path}/download")
def download_file(file_id: str) -> FileResponse:
    path = _resolve_file_id(store.settings.data_root, file_id)
    return FileResponse(path, filename=path.name)


@app.get("/api/screenshots/{screenshot_id:path}")
def screenshot(screenshot_id: str) -> FileResponse:
    path = _resolve_file_id(store.settings.screenshot_dir, screenshot_id)
    return FileResponse(path, filename=path.name)


def main() -> None:
    import uvicorn

    host = os.environ.get("RPA_API_HOST", "0.0.0.0")
    port = int(os.environ.get("RPA_API_PORT", "8000"))
    uvicorn.run("api_server:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
