from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from account_store import AccountStore
from backend_core import RUN_KIND_SYNC_SUPPLIERS, RUN_KIND_TASKS, BackendStore, DEFAULT_CONFIG_PATH
from maochao_rpa import load_settings, normalize_task_name, selected_tasks


app = FastAPI(title="Maochao RPA Backend", version="0.1.0")
store = BackendStore(DEFAULT_CONFIG_PATH)
WEB_ROOT = Path(__file__).resolve().parent / "web"


@app.get("/", include_in_schema=False, response_class=HTMLResponse)
def root() -> HTMLResponse:
    index_path = WEB_ROOT / "index.html"
    if not index_path.is_file():
        return HTMLResponse(
            "<h1>猫超 RPA Web 管理台</h1><p>前端文件尚未部署，请检查 web/index.html。</p>",
            status_code=503,
        )
    return HTMLResponse(index_path.read_text(encoding="utf-8"))


class AccountPayload(BaseModel):
    key: str | None = None
    name: str | None = None
    username: str | None = None
    password: str | None = None
    port: int | None = None
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


class SupplierRefPayload(BaseModel):
    account_key: str = ""
    supplier_id: str = ""
    supplier_name: str = ""


class OperatorCreate(BaseModel):
    name: str


class OperatorSupplierAssign(BaseModel):
    account_key: str
    supplier_ids: list[str] = Field(default_factory=list)


class RunCreate(BaseModel):
    task_keys: list[str] = Field(default_factory=list)
    account_keys: list[str] = Field(default_factory=list)
    force_account_tasks: bool = False
    headed: bool = True
    operator_id: str = ""
    suppliers: list[SupplierRefPayload] = Field(default_factory=list)
    run_kind: str = RUN_KIND_TASKS


def _public_account(account: dict[str, Any]) -> dict[str, Any]:
    item = dict(account)
    item["username_set"] = bool(item.get("username"))
    item["password_set"] = bool(item.get("password"))
    item["username"] = item.get("username") or ""
    item.pop("password", None)
    return item


def _slug(value: str, fallback: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_]+", "_", value.strip()).strip("_").lower()
    return text or fallback


def _next_account_key(accounts: list[dict[str, Any]], username: str) -> str:
    base = f"tmall_{_slug(username, 'account')}"
    used = {account["key"] for account in accounts}
    if base not in used:
        return base
    index = 2
    while f"{base}_{index:02d}" in used:
        index += 1
    return f"{base}_{index:02d}"


def _next_port(accounts: list[dict[str, Any]], start: int = 9231) -> int:
    used = {int(account["port"]) for account in accounts if account.get("port")}
    port = start
    while port in used:
        port += 1
    return port


def _resolve_file_id(root: Path, file_id: str) -> Path:
    path = (root / file_id).resolve()
    if not path.is_file() or root.resolve() not in path.parents:
        raise HTTPException(status_code=404, detail="file not found")
    return path


def _run_output_files(run_item: dict[str, Any]) -> list[Path]:
    files: list[Path] = []
    seen: set[Path] = set()
    for result in run_item.get("result", []):
        for field in ("raw_file", "cleaned_file"):
            value = result.get(field)
            if not value:
                continue
            path = Path(str(value)).expanduser().resolve()
            if path.is_file() and path not in seen:
                seen.add(path)
                files.append(path)
    return files


def _file_run_index() -> dict[str, str]:
    index: dict[str, str] = {}
    for run_item in store.list_runs():
        for path in _run_output_files(run_item):
            index[str(path)] = run_item["run_id"]
    return index


CODE_REVISION = "2026-08-14-download-v33"


@app.get("/health")
@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "pid": os.getpid(),
        "config": str(store.config_path),
        "db": str(store.settings.accounts_db_path),
        "tasks": len(store.list_tasks()),
        "code_revision": CODE_REVISION,
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


@app.get("/api/worker")
def worker() -> dict[str, Any]:
    return store.worker_status()


@app.get("/api/tasks")
def tasks() -> list[dict[str, Any]]:
    return store.list_tasks()


@app.get("/api/accounts")
def accounts(include_disabled: bool = False) -> list[dict[str, Any]]:
    locks = {lock["account_key"]: lock for lock in store.list_account_locks()}
    rows: list[dict[str, Any]] = []
    for account in store.list_accounts(include_disabled=include_disabled):
        item = _public_account(account)
        lock = locks.get(account["key"])
        item["browser_status"] = "占用中" if lock else "空闲"
        item["locked_by_run_id"] = lock["run_id"] if lock else ""
        rows.append(item)
    return rows


@app.post("/api/accounts")
def create_account(payload: AccountPayload) -> dict[str, Any]:
    account_store = AccountStore(store.settings.accounts_db_path, store.settings.accounts_db_key_path)
    existing_accounts = store.list_accounts(include_disabled=True)
    data = payload.dict(exclude_none=True)
    username = str(data.get("username") or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="username is required")
    data.setdefault("key", _next_account_key(existing_accounts, username))
    data.setdefault("name", username)
    data.setdefault("port", _next_port(existing_accounts))
    data.setdefault("profile_dir", f"./browser_profiles/{data['key']}")
    data.setdefault("download_dir", f"./downloads/{data['key']}")
    data["tasks"] = [normalize_task_name(task) for task in data.get("tasks", [])]
    account_store.upsert_account(data, base_dir=store.config_path.parent)
    store.settings = load_settings(store.config_path)
    return {"status": "ok", "account_key": data["key"]}


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


@app.get("/api/operators")
def operators() -> list[dict[str, Any]]:
    return store.list_operators()


@app.post("/api/operators")
def create_operator(payload: OperatorCreate) -> dict[str, Any]:
    try:
        return store.create_operator(payload.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/operators/{operator_id}/suppliers")
def operator_suppliers(operator_id: str, account_key: str = "") -> list[dict[str, Any]]:
    if store.get_operator(operator_id) is None:
        raise HTTPException(status_code=404, detail="operator not found")
    return store.list_operator_suppliers(operator_id, account_key)


@app.put("/api/operators/{operator_id}/suppliers")
def assign_operator_suppliers(operator_id: str, payload: OperatorSupplierAssign) -> list[dict[str, Any]]:
    try:
        return store.set_operator_suppliers(operator_id, payload.account_key, payload.supplier_ids)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="operator not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/suppliers")
def suppliers(account_key: str = "", include_hidden: bool = False) -> list[dict[str, Any]]:
    return store.list_account_suppliers(account_key, include_hidden=include_hidden)


@app.post("/api/accounts/{account_key}/suppliers/sync")
def sync_account_suppliers(account_key: str) -> dict[str, Any]:
    existing = [account for account in store.list_accounts() if account["key"] == account_key]
    if not existing:
        raise HTTPException(status_code=404, detail="account not found")
    return store.create_run(
        task_keys=[],
        account_keys=[account_key],
        force_account_tasks=True,
        headed=True,
        run_kind=RUN_KIND_SYNC_SUPPLIERS,
    )


@app.post("/api/runs")
def create_run(payload: RunCreate) -> dict[str, Any]:
    run_kind = payload.run_kind or RUN_KIND_TASKS
    try:
        task_keys = [] if run_kind == RUN_KIND_SYNC_SUPPLIERS else selected_tasks(payload.task_keys)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    supplier_payloads = [item.dict() for item in payload.suppliers]
    account_keys = payload.account_keys
    if not account_keys:
        supplier_account_keys = [
            str(item.get("account_key") or "")
            for item in supplier_payloads
            if item.get("account_key")
        ]
        account_keys = list(dict.fromkeys(supplier_account_keys)) or [account["key"] for account in store.list_accounts()]
    try:
        return store.create_run(
            task_keys,
            account_keys,
            payload.force_account_tasks if run_kind == RUN_KIND_SYNC_SUPPLIERS else True,
            payload.headed,
            suppliers=supplier_payloads,
            operator_id=payload.operator_id,
            run_kind=run_kind,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/runs")
def runs() -> list[dict[str, Any]]:
    return store.list_runs()


@app.get("/api/runs/{run_id}")
def run(run_id: str) -> dict[str, Any]:
    try:
        return store.get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str) -> dict[str, Any]:
    try:
        return store.cancel_pending_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/runs/{run_id}/pause")
def pause_run(run_id: str) -> dict[str, Any]:
    try:
        return store.pause_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/runs/{run_id}/resume")
def resume_run(run_id: str) -> dict[str, Any]:
    try:
        return store.resume_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/runs/{run_id}/move-up")
def move_run_up(run_id: str) -> dict[str, Any]:
    try:
        return store.move_pending_run(run_id, -1)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/runs/{run_id}/move-down")
def move_run_down(run_id: str) -> dict[str, Any]:
    try:
        return store.move_pending_run(run_id, 1)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/runs/{run_id}/logs")
def run_logs(run_id: str) -> PlainTextResponse:
    log_path = store.settings.log_dir / f"{run_id}.log"
    if not log_path.exists():
        events = store.list_task_events(run_id)
        return PlainTextResponse("\n".join(json.dumps(event, ensure_ascii=False) for event in events))
    return PlainTextResponse(log_path.read_text(encoding="utf-8", errors="replace"))


@app.get("/api/runs/{run_id}/files/download")
def download_run_files(run_id: str) -> FileResponse:
    try:
        run_item = store.get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    files = _run_output_files(run_item)
    if not files:
        raise HTTPException(status_code=404, detail="run has no downloadable files")
    zip_path = Path(tempfile.gettempdir()) / f"maochao_run_{run_id[:8]}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            try:
                arcname = path.relative_to(store.settings.data_root)
            except ValueError:
                arcname = Path(path.name)
            archive.write(path, arcname=str(arcname))
    return FileResponse(zip_path, filename=f"maochao_run_{run_id[:8]}.zip")


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
    run_index = _file_run_index()
    rows = store.list_files()
    for item in rows:
        if item.get("run_id"):
            continue
        item["run_id"] = run_index.get(str(Path(item["path"]).resolve()), "")
    return rows


@app.get("/api/files/{file_id:path}/download")
def download_file(file_id: str) -> FileResponse:
    path = _resolve_file_id(store.settings.data_root, file_id)
    return FileResponse(path, filename=path.name)


@app.get("/api/screenshots/{screenshot_id:path}")
def screenshot(screenshot_id: str) -> FileResponse:
    path = _resolve_file_id(store.settings.screenshot_dir, screenshot_id)
    return FileResponse(path, filename=path.name)


@app.get("/static/{file_path:path}", include_in_schema=False)
def static_file(file_path: str) -> FileResponse:
    path = _resolve_file_id(WEB_ROOT, file_path)
    return FileResponse(path)


def main() -> None:
    import uvicorn

    host = os.environ.get("RPA_API_HOST", "0.0.0.0")
    port = int(os.environ.get("RPA_API_PORT", "8000"))
    uvicorn.run("api_server:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
