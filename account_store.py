# -*- coding: utf-8 -*-
"""
猫超 RPA 账号安全库
=================

SQLite 负责保存账号、端口、浏览器目录、任务归属和账号级 XPath 变量；
用户名和密码使用 Fernet 加密后入库，密钥单独保存在本机文件中。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


BASE = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE / "accounts" / "maochao_accounts.db"
DEFAULT_KEY_PATH = BASE / "accounts" / ".secret_key"

TEMPLATE_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}")


def _require_fernet():
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise RuntimeError(
            "账号数据库需要 cryptography 做加密。\n"
            "建议使用 WorkBuddy/Codex 自带 Python，或运行：python3 -m pip install cryptography"
        ) from exc
    return Fernet


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    return text


def _json_loads(value: str, default: Any) -> Any:
    if not value:
        return deepcopy(default)
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return deepcopy(default)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [_clean_text(item) for item in value if _clean_text(item)]
    if isinstance(value, str):
        return [_clean_text(item) for item in re.split(r"[,，\s]+", value) if _clean_text(item)]
    return [_clean_text(value)] if _clean_text(value) else []


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return deepcopy(value)
    return {}


def _to_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = _clean_text(value).lower()
    if text in {"1", "true", "yes", "y", "on", "是"}:
        return True
    if text in {"0", "false", "no", "n", "off", "否"}:
        return False
    return default


def _resolve_path(value: Any, base_dir: Path, default: str) -> str:
    raw = _clean_text(value) or default
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return str(path.resolve())


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def account_context(account: Mapping[str, Any]) -> dict[str, Any]:
    supplier_names = account.get("supplier_names") or []
    first_supplier = supplier_names[0] if supplier_names else ""
    context: dict[str, Any] = {
        "account": dict(account),
        "account_key": account.get("key", ""),
        "account_name": account.get("name", ""),
        "username": account.get("username", ""),
        "port": account.get("port", ""),
        "profile_dir": str(account.get("profile_dir", "")),
        "download_dir": str(account.get("download_dir", "")),
        "supplier_name": first_supplier,
        "supplier_text": first_supplier,
        "first_supplier_name": first_supplier,
        "primary_supplier_name": first_supplier,
        "xpath_vars": account.get("xpath_vars", {}) or {},
    }
    for key, value in (account.get("xpath_vars", {}) or {}).items():
        context.setdefault(key, value)
    return context


def _lookup_context(context: Mapping[str, Any], name: str) -> Any:
    current: Any = context
    for part in name.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            return None
    return current


def render_template(value: str, context: Mapping[str, Any]) -> str:
    def repl(match: re.Match[str]) -> str:
        found = _lookup_context(context, match.group(1))
        if found is None:
            return match.group(0)
        return str(found)

    return TEMPLATE_RE.sub(repl, value)


def render_tree(value: Any, context: Mapping[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: render_tree(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [render_tree(item, context) for item in value]
    if isinstance(value, str):
        return render_template(value, context)
    return value


def unresolved_templates(value: Any, prefix: str = "") -> list[str]:
    missing: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{prefix}.{key}" if prefix else key
            missing.extend(unresolved_templates(item, child))
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            missing.extend(unresolved_templates(item, f"{prefix}[{idx}]"))
    elif isinstance(value, str) and "{{" in value and "}}" in value:
        missing.append(prefix)
    return missing


class AccountStore:
    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH, key_path: Path | str = DEFAULT_KEY_PATH):
        self.db_path = Path(db_path).expanduser().resolve()
        self.key_path = Path(key_path).expanduser().resolve()

    def ensure_initialized(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_key()
        self.ensure_schema()

    def ensure_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    key TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    username_enc TEXT NOT NULL,
                    password_enc TEXT NOT NULL,
                    port INTEGER NOT NULL UNIQUE,
                    profile_dir TEXT NOT NULL,
                    download_dir TEXT NOT NULL,
                    supplier_names_json TEXT NOT NULL DEFAULT '[]',
                    tasks_json TEXT NOT NULL DEFAULT '[]',
                    note TEXT NOT NULL DEFAULT '',
                    xpath_vars_json TEXT NOT NULL DEFAULT '{}',
                    selector_overrides_json TEXT NOT NULL DEFAULT '{}',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_enabled ON accounts(enabled)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_profile_dir ON accounts(profile_dir)")

    def list_accounts(self, include_disabled: bool = False) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            raise FileNotFoundError(f"账号数据库不存在: {self.db_path}")
        with self._connect() as conn:
            where = "" if include_disabled else "WHERE enabled = 1"
            rows = conn.execute(
                f"""
                SELECT key, name, username_enc, password_enc, port, profile_dir, download_dir,
                       supplier_names_json, tasks_json, note, xpath_vars_json,
                       selector_overrides_json, enabled
                FROM accounts
                {where}
                ORDER BY port, key
                """
            ).fetchall()

        accounts: list[dict[str, Any]] = []
        for row in rows:
            accounts.append(
                {
                    "key": row["key"],
                    "name": row["name"],
                    "username": self._decrypt(row["username_enc"]),
                    "password": self._decrypt(row["password_enc"]),
                    "port": int(row["port"]),
                    "profile_dir": row["profile_dir"],
                    "download_dir": row["download_dir"],
                    "supplier_names": _json_loads(row["supplier_names_json"], []),
                    "tasks": _json_loads(row["tasks_json"], []),
                    "note": row["note"],
                    "xpath_vars": _json_loads(row["xpath_vars_json"], {}),
                    "selector_overrides": _json_loads(row["selector_overrides_json"], {}),
                    "enabled": bool(row["enabled"]),
                }
            )
        return accounts

    def upsert_account(self, payload: Mapping[str, Any], base_dir: Path | None = None) -> None:
        self.ensure_initialized()
        base_dir = base_dir or self.db_path.parent.parent
        record = self._normalize_payload(payload, base_dir)
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO accounts (
                    key, name, username_enc, password_enc, port, profile_dir, download_dir,
                    supplier_names_json, tasks_json, note, xpath_vars_json,
                    selector_overrides_json, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    name = excluded.name,
                    username_enc = excluded.username_enc,
                    password_enc = excluded.password_enc,
                    port = excluded.port,
                    profile_dir = excluded.profile_dir,
                    download_dir = excluded.download_dir,
                    supplier_names_json = excluded.supplier_names_json,
                    tasks_json = excluded.tasks_json,
                    note = excluded.note,
                    xpath_vars_json = excluded.xpath_vars_json,
                    selector_overrides_json = excluded.selector_overrides_json,
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (
                    record["key"],
                    record["name"],
                    self._encrypt(record["username"]),
                    self._encrypt(record["password"]),
                    record["port"],
                    record["profile_dir"],
                    record["download_dir"],
                    _json_dumps(record["supplier_names"]),
                    _json_dumps(record["tasks"]),
                    record["note"],
                    _json_dumps(record["xpath_vars"]),
                    _json_dumps(record["selector_overrides"]),
                    1 if record["enabled"] else 0,
                    now,
                    now,
                ),
            )

    def import_json(self, path: Path) -> int:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("accounts"), list):
            accounts = payload["accounts"]
        elif isinstance(payload, list):
            accounts = payload
        elif isinstance(payload, dict):
            accounts = [payload]
        else:
            raise ValueError("JSON 必须是账号对象、账号对象数组，或包含 accounts 数组的配置文件")

        count = 0
        for account in accounts:
            if isinstance(account, dict) and _clean_text(account.get("key")):
                self.upsert_account(account, base_dir=path.parent)
                count += 1
        return count

    def delete_account(self, key: str) -> None:
        self.ensure_schema()
        with self._connect() as conn:
            conn.execute("DELETE FROM accounts WHERE key = ?", (key,))

    def set_enabled(self, key: str, enabled: bool) -> None:
        self.ensure_schema()
        with self._connect() as conn:
            conn.execute("UPDATE accounts SET enabled = ?, updated_at = ? WHERE key = ?", (
                1 if enabled else 0,
                datetime.now().isoformat(timespec="seconds"),
                key,
            ))

    def _normalize_payload(self, payload: Mapping[str, Any], base_dir: Path) -> dict[str, Any]:
        key = _clean_text(payload.get("key"))
        if not key:
            raise ValueError("账号缺少 key")
        port = int(payload.get("port") or 0)
        if port <= 0:
            raise ValueError(f"账号 {key} 缺少有效 port")
        return {
            "key": key,
            "name": _clean_text(payload.get("name")) or key,
            "username": _clean_text(payload.get("username")),
            "password": _clean_text(payload.get("password")),
            "port": port,
            "profile_dir": _resolve_path(payload.get("profile_dir"), base_dir, f"./browser_profiles/{key}"),
            "download_dir": _resolve_path(payload.get("download_dir"), base_dir, f"./downloads/{key}"),
            "supplier_names": _as_list(payload.get("supplier_names")),
            "tasks": _as_list(payload.get("tasks")),
            "note": _clean_text(payload.get("note")),
            "xpath_vars": _as_dict(payload.get("xpath_vars")),
            "selector_overrides": _as_dict(payload.get("selector_overrides")),
            "enabled": _to_bool(payload.get("enabled"), default=True),
        }

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _ensure_key(self) -> None:
        if self.key_path.exists():
            return
        Fernet = _require_fernet()
        self.key_path.write_bytes(Fernet.generate_key())
        try:
            os.chmod(self.key_path, 0o600)
        except OSError:
            pass

    def _cipher(self, create: bool = False):
        if create:
            self._ensure_key()
        elif not self.key_path.exists():
            raise FileNotFoundError(f"账号库密钥不存在，无法解密账号: {self.key_path}")
        Fernet = _require_fernet()
        key = self.key_path.read_bytes()
        return Fernet(key)

    def _encrypt(self, value: str) -> str:
        return self._cipher(create=True).encrypt(value.encode("utf-8")).decode("ascii")

    def _decrypt(self, value: str) -> str:
        if not value:
            return ""
        return self._cipher(create=False).decrypt(value.encode("ascii")).decode("utf-8")


def _store_from_args(args: argparse.Namespace) -> AccountStore:
    return AccountStore(Path(args.db), Path(args.key))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="猫超 RPA 账号安全库")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite 账号库路径")
    parser.add_argument("--key", default=str(DEFAULT_KEY_PATH), help="Fernet 密钥文件路径")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("init", help="初始化数据库和加密密钥")

    p_import = sub.add_parser("import-json", help="从 JSON/配置文件导入账号记录")
    p_import.add_argument("json_file", help="账号 JSON、账号数组，或包含 accounts 数组的配置文件")

    sub.add_parser("list", help="列出账号，不显示明文密码")

    p_delete = sub.add_parser("delete", help="删除账号")
    p_delete.add_argument("account_key")

    p_disable = sub.add_parser("disable", help="停用账号")
    p_disable.add_argument("account_key")

    p_enable = sub.add_parser("enable", help="启用账号")
    p_enable.add_argument("account_key")

    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 0

    store = _store_from_args(args)
    if args.cmd == "init":
        store.ensure_initialized()
        print(f"[ok] 数据库: {store.db_path}")
        print(f"[ok] 密钥: {store.key_path}")
        return 0

    if args.cmd == "import-json":
        count = store.import_json(Path(args.json_file).expanduser().resolve())
        print(f"[ok] 已导入/更新账号数: {count}")
        return 0

    if args.cmd == "list":
        store.ensure_schema()
        for account in store.list_accounts(include_disabled=True):
            username_status = "已填" if account["username"] else "未填"
            password_status = "已填" if account["password"] else "未填"
            status = "启用" if account["enabled"] else "停用"
            print(
                f"{account['key']} | {status} | port={account['port']} | "
                f"账号={username_status} | 密码={password_status} | tasks={account['tasks']}"
            )
        return 0

    if args.cmd == "delete":
        store.delete_account(args.account_key)
        print(f"[ok] 已删除: {args.account_key}")
        return 0

    if args.cmd == "disable":
        store.set_enabled(args.account_key, False)
        print(f"[ok] 已停用: {args.account_key}")
        return 0

    if args.cmd == "enable":
        store.set_enabled(args.account_key, True)
        print(f"[ok] 已启用: {args.account_key}")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
