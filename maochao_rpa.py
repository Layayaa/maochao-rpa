# -*- coding: utf-8 -*-
"""
猫超补货数据源下载 RPA
====================
覆盖需求文档中本次纳入的 6 个数据源：
  1、实时库存
  2、库存分析 - 品仓明细表
  3、系统单
  4、补货单列表
  10、库位明细
  11、调拨单

设计约束：
  - 一个账号对应一个独立 Chrome remote debugging port 和 user-data-dir；
  - 初期顺序执行，不并发；
  - 账号放在加密 SQLite 安全库中，XPath 留在 JSON 配置并支持账号级覆盖；
  - 下载文件归档到 data/YYYYMMDD/<账号>/raw，并清洗到 cleaned。
"""

from __future__ import annotations

import argparse
import csv
from difflib import SequenceMatcher
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from account_store import AccountStore, account_context, deep_merge, render_tree, unresolved_templates


BASE = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE / "config.example.json"


@dataclass
class Account:
    key: str
    name: str
    username: str
    password: str
    port: int
    profile_dir: Path
    download_dir: Path
    supplier_names: list[str]
    tasks: list[str]
    note: str = ""
    xpath_vars: dict[str, Any] = field(default_factory=dict)
    selector_overrides: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True


@dataclass
class Settings:
    config_path: Path
    accounts_source: str
    accounts_db_path: Path
    accounts_db_key_path: Path
    login_url: str
    chrome_executable_path: str
    data_root: Path
    log_dir: Path
    screenshot_dir: Path
    headless: bool
    download_timeout_sec: int
    task_timeout_sec: int
    poll_interval_sec: float
    accounts: list[Account]
    direct_urls: dict[str, str]
    selectors: dict[str, Any]
    cleanup: dict[str, Any]


@dataclass
class RunResult:
    task: str
    title: str
    account: str
    status: str
    raw_file: str = ""
    cleaned_file: str = ""
    screenshot: str = ""
    error: str = ""
    started_at: str = ""
    finished_at: str = ""
    note: str = ""


TASKS: dict[str, dict[str, str]] = {
    "realtime-inventory": {
        "title": "1、实时库存",
        "file_task_text": "导出 实时库存",
        "prefix": "01_realtime_inventory",
    },
    "pincang-detail": {
        "title": "2、库存分析-品仓明细表",
        "file_task_text": "导出 品仓明细表",
        "prefix": "02_pincang_detail",
    },
    "system-order": {
        "title": "3、系统单",
        "file_task_text": "导出 PO明细确认分页导出",
        "prefix": "03_system_order",
    },
    "po-list": {
        "title": "4、补货单列表",
        "file_task_text": "PO明细分页导出",
        "prefix": "04_po_list",
    },
    "channel-goods": {
        "title": "10、库位明细",
        "file_task_text": "货品生命周期导出结果",
        "prefix": "10_channel_goods",
    },
    "transfer-order": {
        "title": "11、调拨单",
        "file_task_text": "导出 调拨单货品明细",
        "prefix": "11_transfer_order",
    },
}

TASK_ALIASES = {
    "1": "realtime-inventory",
    "实时库存": "realtime-inventory",
    "2": "pincang-detail",
    "品仓明细": "pincang-detail",
    "3": "system-order",
    "系统单": "system-order",
    "4": "po-list",
    "补货单": "po-list",
    "10": "channel-goods",
    "库位明细": "channel-goods",
    "11": "transfer-order",
    "调拨单": "transfer-order",
}

REQUIRED_SELECTORS: dict[str, tuple[str, ...]] = {
    "realtime-inventory": (
        "realtime.menu_inventory",
        "realtime.menu_inventory_query",
        "realtime.supplier_field",
        "realtime.query_button",
        "realtime.export_button",
        "realtime.export_all_option",
    ),
    "pincang-detail": (
        "pincang.menu_tianji",
        "pincang.menu_inventory_analysis",
        "pincang.tab_pincang_detail",
        "pincang.export_button",
    ),
    "system-order": (
        "purchase.menu_purchase",
        "purchase.menu_replenishment_order",
        "purchase.po_status_field",
        "purchase.query_button",
        "system_order.import_button",
        "system_order.import_confirm_option",
        "system_order.dialog_export_data",
    ),
    "po-list": (
        "purchase.menu_purchase",
        "purchase.menu_replenishment_order",
        "po_list.more_button",
        "po_list.start_date_input",
        "po_list.end_date_input",
        "po_list.date_confirm_button",
        "purchase.po_status_field",
        "purchase.query_button",
        "po_list.export_button",
    ),
    "channel-goods": (
        "channel_goods.menu_goods",
        "channel_goods.menu_channel_goods",
        "channel_goods.filter_button",
        "channel_goods.export_button",
    ),
    "transfer-order": (
        "purchase.menu_purchase",
        "transfer_order.menu_transfer_order",
        "transfer_order.export_button",
        "transfer_order.export_goods_detail_option",
    ),
}


def _require_playwright():
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "缺少 Playwright 依赖。请先运行：\n"
            "  python3 -m pip install -r requirements.txt\n"
            "或使用 WorkBuddy Python 执行同样命令。"
        ) from exc
    return sync_playwright, PlaywrightTimeoutError


def _resolve_path(raw: str | None, base: Path, default: str) -> Path:
    value = (raw or default).strip()
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    return text


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
        return value
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


def _slug(value: str) -> str:
    value = re.sub(r"\s+", "_", value.strip())
    value = re.sub(r'[\\/:*?"<>|]+', "_", value)
    return value or "item"


def _months_ago(base: date, months: int) -> date:
    year = base.year
    month = base.month - months
    while month <= 0:
        month += 12
        year -= 1
    month_lengths = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                     31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    day = min(base.day, month_lengths[month - 1])
    return date(year, month, day)


def _xpath_literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    parts = value.split("'")
    return "concat(" + ', "\'", '.join(f"'{part}'" for part in parts) + ")"


def _pw_selector(selector: str) -> str:
    selector = selector.strip()
    if selector.startswith("xpath="):
        return selector
    if selector.startswith("/") or selector.startswith("("):
        return f"xpath={selector}"
    return selector


def load_settings(config_path: Path) -> Settings:
    config_path = config_path.expanduser().resolve()
    with config_path.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    base = config_path.parent

    default_download_root = _resolve_path(raw.get("download_root"), base, "./downloads")
    accounts_source = (_clean_text(raw.get("accounts_source", "db")) or "db").lower()
    accounts_db_path = _resolve_path(raw.get("accounts_db_path"), base, "./accounts/maochao_accounts.db")
    accounts_db_key_path = _resolve_path(raw.get("accounts_db_key_path"), base, "./accounts/.secret_key")

    if accounts_source == "db":
        store = AccountStore(accounts_db_path, accounts_db_key_path)
        try:
            account_items = store.list_accounts()
        except FileNotFoundError as exc:
            raise RuntimeError(
                "账号库还没有初始化。请先运行：\n"
                f"  python account_store.py --db \"{accounts_db_path}\" --key \"{accounts_db_key_path}\" init\n"
                f"  python account_store.py --db \"{accounts_db_path}\" --key \"{accounts_db_key_path}\" import-json \"{config_path}\""
            ) from exc
    elif accounts_source == "json":
        account_items = raw.get("accounts", [])
    else:
        raise ValueError("accounts_source 只支持 db 或 json")

    accounts = [
        _account_from_mapping(item, base, default_download_root)
        for item in account_items
        if isinstance(item, dict)
        and _clean_text(item.get("key"))
        and _to_bool(item.get("enabled"), default=True)
    ]
    if accounts_source == "db" and not accounts:
        raise RuntimeError("账号库中没有启用账号，请先导入或启用账号记录。")

    return Settings(
        config_path=config_path,
        accounts_source=accounts_source,
        accounts_db_path=accounts_db_path,
        accounts_db_key_path=accounts_db_key_path,
        login_url=_clean_text(raw.get("login_url")),
        chrome_executable_path=_clean_text(raw.get("chrome_executable_path")),
        data_root=_resolve_path(raw.get("data_root"), base, "./data"),
        log_dir=_resolve_path(raw.get("log_dir"), base, "./logs"),
        screenshot_dir=_resolve_path(raw.get("screenshot_dir"), base, "./logs/screenshots"),
        headless=bool(raw.get("headless", False)),
        download_timeout_sec=int(raw.get("download_timeout_sec") or 300),
        task_timeout_sec=int(raw.get("task_timeout_sec") or 600),
        poll_interval_sec=float(raw.get("poll_interval_sec") or 2),
        accounts=accounts,
        direct_urls={k: _clean_text(v) for k, v in raw.get("direct_urls", {}).items()},
        selectors=raw.get("selectors", {}),
        cleanup=raw.get("cleanup", {}),
    )


def _account_from_mapping(item: dict[str, Any], base: Path, default_download_root: Path) -> Account:
    key = _clean_text(item.get("key"))
    account_download_dir = _resolve_path(item.get("download_dir"), base, f"./downloads/{key}")
    return Account(
        key=key,
        name=_clean_text(item.get("name")) or key,
        username=_clean_text(item.get("username")),
        password=_clean_text(item.get("password")),
        port=int(item.get("port") or 0),
        profile_dir=_resolve_path(item.get("profile_dir"), base, f"./browser_profiles/{key}"),
        download_dir=account_download_dir if item.get("download_dir") else default_download_root / key,
        supplier_names=_as_list(item.get("supplier_names")),
        tasks=[normalize_task_name(v) for v in _as_list(item.get("tasks"))],
        note=_clean_text(item.get("note")),
        xpath_vars=_as_dict(item.get("xpath_vars")),
        selector_overrides=_as_dict(item.get("selector_overrides")),
        enabled=_to_bool(item.get("enabled"), default=True),
    )


def normalize_task_name(value: str) -> str:
    text = _clean_text(value)
    if not text:
        return text
    return TASK_ALIASES.get(text, text)


def selected_tasks(task_names: Iterable[str] | None) -> list[str]:
    values = [normalize_task_name(v) for v in (task_names or []) if _clean_text(v)]
    if not values or "all" in values:
        return list(TASKS)
    unknown = [v for v in values if v not in TASKS]
    if unknown:
        raise KeyError(f"未知猫超任务: {', '.join(unknown)}")
    return values


class MaochaoRPA:
    def __init__(
        self,
        settings: Settings,
        manual_login: bool = False,
        headless: bool | None = None,
    ):
        self.settings = settings
        self.manual_login = manual_login
        self.headless = settings.headless if headless is None else headless
        self.sync_playwright, self.PlaywrightTimeoutError = _require_playwright()
        self._active_account: Account | None = None
        self._active_selectors: dict[str, Any] = settings.selectors
        self._handlers: dict[str, Callable[[Any, Account], list[RunResult]]] = {
            "realtime-inventory": self._task_realtime_inventory,
            "pincang-detail": self._task_pincang_detail,
            "system-order": self._task_system_order,
            "po-list": self._task_po_list,
            "channel-goods": self._task_channel_goods,
            "transfer-order": self._task_transfer_order,
        }

    def run(
        self,
        tasks: list[str],
        account_keys: list[str] | None = None,
        force_account_tasks: bool = False,
    ) -> list[RunResult]:
        self._ensure_dirs()
        selected_accounts = self._selected_accounts(account_keys)
        results: list[RunResult] = []

        with self.sync_playwright() as p:
            for account in selected_accounts:
                account_tasks = tasks if force_account_tasks and account_keys else [task for task in tasks if task in account.tasks]
                if not account_tasks:
                    continue

                print(f"[猫超] 接管账号: {account.name} ({account.key})")
                self._ensure_account_dirs(account)
                self._ensure_chrome(account)
                account_started = datetime.now().isoformat(timespec="seconds")
                browser = None
                page = None
                try:
                    browser = p.chromium.connect_over_cdp(
                        f"http://127.0.0.1:{account.port}",
                        no_defaults=True,
                        is_local=True,
                        timeout=30000,
                    )
                    context = browser.contexts[0] if browser.contexts else browser.new_context()
                    page = context.pages[0] if context.pages else context.new_page()
                    self._set_download_behavior(context, page, account.download_dir)
                    self._set_active_account(account)
                    self._login_or_reuse_session(page, account)

                    for task_key in account_tasks:
                        started = datetime.now().isoformat(timespec="seconds")
                        try:
                            print(f"[猫超] 开始: {TASKS[task_key]['title']} / {account.name}")
                            task_results = self._handlers[task_key](page, account)
                            results.extend(task_results)
                        except Exception as exc:
                            shot = self._screenshot(page, f"{task_key}_{account.key}_failed")
                            results.append(
                                RunResult(
                                    task=task_key,
                                    title=TASKS[task_key]["title"],
                                    account=account.key,
                                    status="failed",
                                    screenshot=str(shot),
                                    error=f"{exc}\n{traceback.format_exc(limit=4)}",
                                    started_at=started,
                                    finished_at=datetime.now().isoformat(timespec="seconds"),
                                )
                            )
                            print(f"[猫超] 失败: {TASKS[task_key]['title']} -> {exc}")
                except Exception as exc:
                    shot = self._capture_error_screenshot(page, f"{account.key}_account_error")
                    results.append(
                        RunResult(
                            task="__account__",
                            title=f"{account.name} 账户阶段",
                            account=account.key,
                            status="failed",
                            screenshot=shot,
                            error=f"{exc}\n{traceback.format_exc(limit=4)}",
                            started_at=account_started,
                            finished_at=datetime.now().isoformat(timespec="seconds"),
                        )
                    )
                    print(f"[猫超] 账户阶段失败: {account.name} -> {exc}")
                finally:
                    # 这里不主动 close 已接管的浏览器，避免把账号资料目录里的登录态一并关掉。
                    pass

        self._write_manifest(results)
        return results

    def login_only(self, account_keys: list[str] | None = None) -> None:
        self._ensure_dirs()
        selected_accounts = self._selected_accounts(account_keys)
        with self.sync_playwright() as p:
            for account in selected_accounts:
                print(f"[猫超] 打开账号浏览器用于首次登录: {account.name} ({account.key})")
                self._ensure_account_dirs(account)
                self._ensure_chrome(account)
                browser = None
                page = None
                try:
                    browser = p.chromium.connect_over_cdp(
                        f"http://127.0.0.1:{account.port}",
                        no_defaults=True,
                        is_local=True,
                        timeout=30000,
                    )
                    context = browser.contexts[0] if browser.contexts else browser.new_context()
                    page = context.pages[0] if context.pages else context.new_page()
                    self._set_download_behavior(context, page, account.download_dir)
                    self._set_active_account(account)
                    self._login_or_reuse_session(page, account)
                    print("[猫超] 请确认已进入业务页面；完成后回到终端按 Enter 继续下一个账号。")
                    input()
                except Exception as exc:
                    shot = self._capture_error_screenshot(page, f"{account.key}_login_error")
                    print(f"[猫超] 首次登录失败: {account.name} -> {exc}")
                    if shot:
                        print(f"[猫超] 已保存登录错误截图: {shot}")
                    raise
                finally:
                    # 同样不主动关闭，保持 profile 与登录态。
                    pass

    def _selected_accounts(self, account_keys: list[str] | None) -> list[Account]:
        if not account_keys:
            return self.settings.accounts
        wanted = set(account_keys)
        accounts = [account for account in self.settings.accounts if account.key in wanted]
        missing = sorted(wanted - {account.key for account in accounts})
        if missing:
            raise KeyError(f"配置中找不到账号: {', '.join(missing)}")
        return accounts

    def _ensure_dirs(self) -> None:
        self.settings.data_root.mkdir(parents=True, exist_ok=True)
        self.settings.log_dir.mkdir(parents=True, exist_ok=True)
        self.settings.screenshot_dir.mkdir(parents=True, exist_ok=True)

    def _ensure_account_dirs(self, account: Account) -> None:
        account.profile_dir.mkdir(parents=True, exist_ok=True)
        account.download_dir.mkdir(parents=True, exist_ok=True)
        for path in self._account_data_dirs(account):
            path.mkdir(parents=True, exist_ok=True)

    def _account_data_dirs(self, account: Account) -> tuple[Path, Path]:
        today = date.today().strftime("%Y%m%d")
        root = self.settings.data_root / today / account.key
        return root / "raw", root / "cleaned"

    def _set_active_account(self, account: Account) -> None:
        self._active_account = account
        context = account_context(
            {
                "key": account.key,
                "name": account.name,
                "username": account.username,
                "password": account.password,
                "port": account.port,
                "profile_dir": str(account.profile_dir),
                "download_dir": str(account.download_dir),
                "supplier_names": account.supplier_names,
                "tasks": account.tasks,
                "note": account.note,
                "xpath_vars": account.xpath_vars,
                "selector_overrides": account.selector_overrides,
                "enabled": account.enabled,
            }
        )
        rendered = render_tree(self.settings.selectors, context)
        overrides = render_tree(account.selector_overrides, context)
        self._active_selectors = deep_merge(rendered, overrides)
        unresolved = unresolved_templates(self._active_selectors)
        if unresolved:
            print(f"[猫超] 提醒：账号 {account.key} 的部分 XPath 变量未展开: {unresolved}")

    def _ensure_chrome(self, account: Account) -> None:
        if account.port <= 0:
            raise RuntimeError(f"账号 {account.key} 缺少有效 browser port")
        if self._port_open(account.port):
            print(f"[猫超] 端口 {account.port} 已打开，直接接管。")
            return
        chrome_path = self.settings.chrome_executable_path
        if not chrome_path or not Path(chrome_path).exists():
            raise RuntimeError(f"找不到 Chrome: {chrome_path or '未配置'}")

        chrome_args = [
            chrome_path,
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={account.port}",
            f"--user-data-dir={account.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-popup-blocking",
            "about:blank",
        ]
        if self.headless:
            chrome_args.insert(-1, "--headless=new")
            cmd = chrome_args
        else:
            app_bundle = self._chrome_app_bundle(Path(chrome_path))
            cmd = ["open", "-na", str(app_bundle), "--args", *chrome_args[1:]] if app_bundle else chrome_args
        print(f"[猫超] 启动 Chrome: {account.name} / port={account.port}")
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 60
        while time.time() < deadline:
            if self._port_open(account.port):
                return
            time.sleep(0.3)
        raise RuntimeError(f"Chrome 启动后端口未打开: {account.port}")

    def _chrome_app_bundle(self, chrome_path: Path) -> Path | None:
        for parent in chrome_path.parents:
            if parent.suffix == ".app":
                return parent
        return None

    def _port_open(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            return sock.connect_ex(("127.0.0.1", port)) == 0

    def _set_download_behavior(self, context: Any, page: Any, download_dir: Path) -> None:
        download_dir.mkdir(parents=True, exist_ok=True)
        try:
            session = context.new_cdp_session(page)
            try:
                session.send(
                    "Browser.setDownloadBehavior",
                    {"behavior": "allow", "downloadPath": str(download_dir), "eventsEnabled": True},
                )
            except Exception:
                session.send("Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(download_dir)})
        except Exception as exc:
            print(f"[猫超] 下载目录 CDP 设置失败，将使用浏览器默认目录: {exc}")

    def _login_or_reuse_session(self, page: Any, account: Account) -> None:
        if not self.settings.login_url:
            raise RuntimeError("config 缺少 login_url")

        if self._wait_business_home(page, 3000):
            self._dismiss_blocking_popups(page)
            print("[猫超] 检测到现有登录态，跳过登录页。")
            return

        try:
            page.goto("https://web.txcs.tmall.com/", wait_until="domcontentloaded")
            self._wait_quiet(page, 8000)
            if self._wait_business_home(page, 8000):
                self._dismiss_blocking_popups(page)
                print("[猫超] 已复用现有登录态，跳过登录页。")
                return
        except Exception as exc:
            print(f"[猫超] 复用现有登录态失败，改走登录页: {exc}")

        page.goto(self.settings.login_url, wait_until="domcontentloaded")
        self._wait_quiet(page, 8000)

        login_scope = self._frame_with_selectors(
            page,
            ("login.username_input", "login.password_input", "login.login_button"),
            timeout=5000,
        )
        if login_scope is None and self._selector_visible(page, "login.password_input", timeout=2000):
            login_scope = page

        if login_scope:
            if account.username and account.password:
                print(f"[猫超] 检测到登录页，尝试填写账号: {account.name}")
                self._scope_fill(login_scope, "login.username_input", account.username, "账号")
                self._scope_fill(login_scope, "login.password_input", account.password, "密码")
                self._wait_quiet(page, 1000)
                self._dismiss_blocking_popups(page)
                self._scope_click(login_scope, "login.login_button", "登录", timeout=30000)
                try:
                    self._wait_login_transition(page, 60000)
                except Exception:
                    if not self.manual_login:
                        raise
                    print("[猫超] 自动登录未完成，请在当前浏览器完成验证码/滑块/人工确认后回到终端按 Enter。")
                    input()
                    self._wait_login_transition(page, 120000)
            else:
                print(f"[猫超] 检测到登录页，但账号 {account.key} 未配置账号密码。请人工登录后回车。")
                input()

        if self.manual_login:
            print("[猫超] 如有验证码/扫码/手机确认，请处理完成后回到终端按 Enter。")
            input()
            self._wait_quiet(page, 5000)

        self._handle_merchant_selector(page)
        if not self._wait_business_home(page, 30000):
            raise RuntimeError("登录未完成：未进入商家主页，请检查验证码/滑块/登录态。")
        self._dismiss_blocking_popups(page)
        print("[猫超] 登录态可用，继续执行。")

    def _wait_login_transition(self, page: Any, timeout_ms: int) -> None:
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            if self._frame_with_selectors(page, ("merchant.enter_button",), timeout=500):
                return
            if self._wait_business_home(page, 500):
                return
            time.sleep(1)
        raise RuntimeError("登录提交后未进入商家选择页或商家主页。")

    def _wait_business_home(self, page: Any, timeout_ms: int) -> bool:
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            if self._visible_locator(page, "li.auto-more", "顶部更多菜单", timeout=500):
                return True
            if self._visible_locator(page, "a:has-text(\"首页\")", "首页菜单", timeout=500):
                return True
            time.sleep(0.5)
        return False

    def _handle_merchant_selector(self, page: Any) -> None:
        merchant_scope = self._frame_with_selectors(
            page,
            ("merchant.enter_button",),
            timeout=2500,
        )
        if merchant_scope is None and self._selector_visible(page, "merchant.enter_button", timeout=1500):
            merchant_scope = page
        if merchant_scope is None:
            return
        print("[猫超] 检测到选择商家账号中间页。")
        second_supplier = self._selector_optional("merchant.second_supplier_dropdown") or "div.next-select-wrapper"
        try:
            dropdowns = merchant_scope.locator(_pw_selector(second_supplier))
            count = dropdowns.count()
            if count:
                locator = dropdowns.nth(1) if count > 1 else dropdowns.first
                if locator.is_visible(timeout=1500):
                    locator.click(timeout=1500)
                    self._wait_quiet(page, 800)
                    second_option = self._selector_optional("merchant.second_supplier_first_option") or ".next-overlay-wrapper .menu-item"
                    option = merchant_scope.locator(_pw_selector(second_option)).first
                    if option.is_visible(timeout=1500):
                        option.click(timeout=1500)
                    else:
                        try:
                            page.keyboard.press("ArrowDown")
                            page.keyboard.press("Enter")
                        except Exception:
                            pass
        except Exception:
            pass
        self._scope_click(merchant_scope, "merchant.enter_button", "进入商家")
        try:
            page.wait_for_url(re.compile(r"^https://web\.txcs\.tmall\.com/(?:\?|$)"), timeout=15000)
        except Exception:
            pass
        self._wait_quiet(page, 10000)

    def _task_realtime_inventory(self, page: Any, account: Account) -> list[RunResult]:
        menu_selectors = (
            "realtime.menu_inventory",
            "realtime.menu_inventory_query",
        )
        self._open_task_page(page, "realtime-inventory", menu_selectors)
        suppliers = self._realtime_supplier_names(page, account)
        results: list[RunResult] = []
        for index, supplier in enumerate(suppliers):
            if index:
                # Keep switching suppliers on the same page when the form is still there.
                # Only recover the direct page if the form was really lost.
                self._dismiss_notification_center(page)
                if not self._selector_visible(page, "realtime.supplier_field", timeout=1500):
                    self._open_task_page(page, "realtime-inventory", menu_selectors)
            self._select_realtime_supplier(page, supplier)
            self._wait_quiet(page, 1500)
            self._click(page, "realtime.query_button", "查询")
            self._wait_quiet(page, 5000)
            result = self._export_realtime_supplier(page, account, supplier)
            results.append(result)
        return results

    def _task_pincang_detail(self, page: Any, account: Account) -> list[RunResult]:
        self._open_task_page(page, "pincang-detail", (
            "pincang.menu_tianji",
            "pincang.menu_inventory_analysis",
            "pincang.tab_pincang_detail",
        ))
        self._click(page, "pincang.export_button", "导出")
        return [self._download_and_clean(page, "pincang-detail", account)]

    def _task_system_order(self, page: Any, account: Account) -> list[RunResult]:
        self._open_purchase_replenishment(page)
        statuses = self._list_config("system_order.statuses", ["待供应商确认", "PB审批:库控小二"])
        self._select_first_purchase_status(page, statuses)
        self._click(page, "purchase.query_button", "查询")
        self._wait_quiet(page, 5000)
        self._click(page, "system_order.import_button", "导入")
        self._click(page, "system_order.import_confirm_option", "导入确认")
        self._wait_quiet(page, 2000)
        self._click(page, "system_order.dialog_export_data", "导出数据", timeout=5000)
        return [self._download_and_clean(page, "system-order", account)]

    def _task_po_list(self, page: Any, account: Account) -> list[RunResult]:
        self._open_purchase_replenishment(page, task_key="po-list")
        self._click_optional(page, "po_list.more_button", "更多筛选")
        self._wait_quiet(page, 1000)
        if not self._selector_visible(page, "po_list.start_date_input", timeout=1000):
            self._click_optional(page, "po_list.more_button", "更多筛选")
            self._wait_quiet(page, 1000)
        self._fill_last_two_months(page)
        self._select_po_list_statuses(page)
        self._click(page, "purchase.query_button", "查询")
        self._wait_quiet(page, 5000)
        if self._page_has_no_items(page):
            return [self._no_data_result("po-list", account, "补货单列表无数据，未生成下载文件")]
        self._unclick_current_page_only_if_present(page)
        self._click(page, "po_list.export_button", "导出")
        self._click_text(page, "导出明细", timeout=5000)
        return [self._download_and_clean(page, "po-list", account)]

    def _select_po_list_statuses(self, page: Any) -> None:
        requested = self._list_config(
            "po_list.statuses",
            ["待供应商预约", "供应商已确认", "待收货", "部分收货"],
        )
        aliases = {
            "待供应商预约": ["待供应商预约"],
            "供应商已确认": ["供应商已确认"],
            "待收货": ["待收货"],
            "部分收货": ["部分收货", "待部分收货"],
            "待部分收货": ["待部分收货", "部分收货"],
        }
        for status in requested:
            candidates = aliases.get(status, [status])
            try:
                self._select_first_purchase_status(
                    page,
                    candidates,
                    field_selector_key="purchase.po_status_field",
                    field_label="采购单状态",
                )
            except Exception as exc:
                print(f"[猫超] 采购单状态未选中，已跳过: {status} ({exc})")

    def _task_channel_goods(self, page: Any, account: Account) -> list[RunResult]:
        self._open_task_page(page, "channel-goods", (
            "channel_goods.menu_goods",
            "channel_goods.menu_channel_goods",
        ))
        try:
            self._click(page, "channel_goods.filter_button", "筛选")
        except Exception:
            self._dismiss_blocking_popups(page)
            self._wait_quiet(page, 1000)
            self._click_force(page, "channel_goods.filter_button", "筛选")
        self._wait_quiet(page, 5000)
        try:
            self._click(page, "channel_goods.export_button", "导出")
        except Exception:
            self._dismiss_blocking_popups(page)
            self._wait_quiet(page, 1000)
            self._click_force(page, "channel_goods.export_button", "导出")
        return [self._download_and_clean(page, "channel-goods", account, note="商品→渠道货品→筛选→导出")]

    def _task_transfer_order(self, page: Any, account: Account) -> list[RunResult]:
        self._open_task_page(page, "transfer-order", (
            "purchase.menu_purchase",
            "transfer_order.menu_transfer_order",
        ))
        self._reset_transfer_filters(page)
        if self._page_has_no_items(page):
            return [self._no_data_result("transfer-order", account, "调拨单无数据，未生成下载文件")]
        self._click(page, "transfer_order.export_button", "导出")
        self._click(page, "transfer_order.export_goods_detail_option", "导出货品明细")
        return [self._download_and_clean(page, "transfer-order", account)]

    def _open_purchase_replenishment(self, page: Any, task_key: str = "system-order") -> None:
        self._open_task_page(page, task_key, (
            "purchase.menu_purchase",
            "purchase.menu_replenishment_order",
        ))

    def _open_task_page(self, page: Any, task_key: str, menu_selectors: tuple[str, ...]) -> None:
        self._dismiss_notification_center(page)
        self._dismiss_blocking_popups(page)
        direct_url = self.settings.direct_urls.get(task_key, "")
        if direct_url:
            print(f"[猫超] 打开直达 URL: {TASKS[task_key]['title']}")
            page.goto(direct_url, wait_until="domcontentloaded")
            self._wait_quiet(page, 8000)
            self._dismiss_blocking_popups(page)
            return
        for idx, selector_key in enumerate(menu_selectors):
            self._dismiss_blocking_popups(page)
            try:
                self._reveal_top_menu(page)
                self._click(page, selector_key, selector_key)
                self._wait_quiet(page, 5000)
            except Exception as exc:
                if idx + 1 < len(menu_selectors):
                    next_selector = self._selector_optional(menu_selectors[idx + 1])
                    if next_selector and self._visible_locator(page, next_selector, menu_selectors[idx + 1], timeout=1000):
                        continue
                raise RuntimeError(f"打开{TASKS[task_key]['title']}失败: {selector_key}: {exc}") from exc

    def _reveal_top_menu(self, page: Any) -> None:
        for selector in ("li.auto-more", "a:has-text(\"更多\")", "button:has-text(\"更多\")"):
            try:
                more = page.locator(selector).first
                if not more.is_visible(timeout=800):
                    continue
                more.hover(timeout=1500)
                more.click(timeout=1500)
                self._wait_quiet(page, 500)
                return
            except Exception:
                continue

    def _dismiss_blocking_popups(self, page: Any) -> None:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass

        if self._click_close_button_by_script(page):
            self._wait_quiet(page, 800)
            print("[猫超] 已关闭遮挡弹窗。")
            return

        close_selectors = (
            ".next-overlay-wrapper:visible .next-dialog-close",
            ".next-overlay-wrapper:visible .next-overlay-close",
            ".next-overlay-wrapper:visible [aria-label=\"Close\"]",
            ".next-overlay-wrapper:visible [aria-label=\"关闭\"]",
            ".next-overlay-wrapper:visible button:has-text(\"取消\")",
            ".next-overlay-wrapper:visible button:has-text(\"我知道了\")",
            ".next-overlay-wrapper:visible button:has-text(\"×\")",
            ".next-overlay-wrapper:visible [role=\"button\"]:has-text(\"×\")",
            ".next-dialog:visible .next-dialog-close",
            ".next-dialog:visible [aria-label=\"Close\"]",
            ".next-dialog:visible [aria-label=\"关闭\"]",
            ".next-dialog:visible button:has-text(\"取消\")",
            ".next-dialog:visible button:has-text(\"我知道了\")",
            ".next-dialog:visible [class*=\"close\"]",
            ".ant-modal-root:visible .ant-modal-close",
            ".ant-modal:visible .ant-modal-close",
            "[role=\"dialog\"]:visible [aria-label=\"Close\"]",
            "[role=\"dialog\"]:visible [aria-label=\"关闭\"]",
            "[role=\"dialog\"]:visible button:has-text(\"取消\")",
            "[role=\"dialog\"]:visible button:has-text(\"我知道了\")",
            "[role=\"dialog\"]:visible button:has-text(\"×\")",
            "[class*=\"modal\"]:visible [class*=\"close\"]",
            "[class*=\"dialog\"]:visible [class*=\"close\"]",
            "[class*=\"popup\"]:visible [class*=\"close\"]",
        )
        for selector in close_selectors:
            locator = self._quick_visible_locator(page, selector, timeout=120)
            if locator is None:
                continue
            try:
                locator.click(timeout=1000)
                self._wait_quiet(page, 800)
                print("[猫超] 已关闭遮挡弹窗。")
                return
            except Exception:
                continue

    def _click_close_button_by_script(self, page: Any) -> bool:
        script = (
            """
            () => {
              const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                  style.visibility !== 'hidden' && style.display !== 'none' &&
                  Number(style.opacity || 1) > 0;
              };
              const zIndex = (el) => {
                const raw = window.getComputedStyle(el).zIndex;
                const value = Number.parseInt(raw, 10);
                return Number.isFinite(value) ? value : 0;
              };
              const candidates = [];
              for (const el of document.querySelectorAll('button,[role="button"],a,span,div,i,svg')) {
                if (!visible(el)) continue;
                const rect = el.getBoundingClientRect();
                if (rect.top < 110) continue;
                const text = (el.innerText || el.textContent || '').trim();
                const label = [
                  el.getAttribute('aria-label') || '',
                  el.getAttribute('title') || '',
                  typeof el.className === 'string' ? el.className : ''
                ].join(' ').toLowerCase();
                const looksClose =
                  text === '×' || text === 'x' || text === 'X' || text === '✕' ||
                  label.includes('close') || label.includes('关闭');
                if (!looksClose) continue;
                candidates.push({ el, score: zIndex(el) * 10000 + rect.top + rect.left / 1000 });
              }
              candidates.sort((a, b) => b.score - a.score);
              const target = candidates[0]?.el;
              if (!target) return false;
              target.click();
              return true;
            }
            """
        )
        for scope in self._iter_scopes(page):
            try:
                if scope.evaluate(script):
                    return True
            except Exception:
                continue
        return False

    def _quick_visible_locator(self, page: Any, selector: str, timeout: int = 120) -> Any | None:
        pw_selector = _pw_selector(selector)
        for scope in self._iter_scopes(page):
            try:
                item = scope.locator(pw_selector).first
                if item.is_visible(timeout=timeout):
                    return item
            except Exception:
                continue
        return None

    def _select_realtime_supplier(self, page: Any, supplier: str) -> None:
        self._dismiss_notification_center(page)
        self._open_realtime_supplier_dropdown(page)
        if supplier and supplier != "__first__":
            if self._click_realtime_supplier_option(page, supplier, timeout=3000):
                return
            raise RuntimeError(f"供应商下拉中找不到: {supplier}")
        first_option = self._first_realtime_supplier_option(page, timeout=3000)
        if first_option is not None:
            first_option.click(timeout=3000)
            self._wait_quiet(page, 800)
            return
        try:
            page.keyboard.press("ArrowDown")
            page.keyboard.press("Enter")
        except Exception:
            pass

    def _open_realtime_supplier_dropdown(self, page: Any, optional: bool = False) -> bool:
        self._dismiss_notification_center(page)
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        self._wait_quiet(page, 1000)

        clicked = False
        fallbacks = (
            self._selector_optional("realtime.supplier_field"),
            "xpath=//*[contains(normalize-space(.), '供应商名称')]/following::*[contains(@class, 'next-select')][1]",
            "xpath=//*[contains(normalize-space(.), '供应商名称')]/following::*[contains(@class, 'next-select-selector')][1]",
            "xpath=//div[contains(@class, 'next-form-item')][.//*[contains(normalize-space(.), '供应商名称')]]//*[contains(normalize-space(.), '请选择')][1]",
            "text=请选择",
        )
        for fallback in fallbacks:
            if not fallback:
                continue
            try:
                locator = self._visible_locator(page, fallback, "供应商名称", timeout=8000)
                if locator is None:
                    continue
                try:
                    locator.click(timeout=3000)
                except Exception:
                    locator.click(timeout=3000, force=True)
                clicked = True
                break
            except Exception:
                continue
        if not clicked:
            if optional:
                return False
            raise RuntimeError("找不到供应商名称下拉")
        self._wait_quiet(page, 1000)
        return True

    def _dismiss_notification_center(self, page: Any) -> None:
        try:
            page.evaluate(
                """
                () => {
                  for (const selector of [
                    '.notification-center-mask.show',
                    '.notification-center-mask',
                    '.notification-center',
                    '.notification-drawer-container'
                  ]) {
                    document.querySelectorAll(selector).forEach((el) => {
                      el.classList.remove('show');
                      el.style.display = 'none';
                      el.style.pointerEvents = 'none';
                    });
                  }
                  document.body.style.overflow = '';
                }
                """
            )
        except Exception:
            pass
        self._wait_quiet(page, 200)

    def _realtime_supplier_names(self, page: Any, account: Account) -> list[str]:
        configured = [_clean_text(item) for item in account.supplier_names if _clean_text(item)]
        discovered = self._discover_realtime_supplier_names(page)
        if configured and not all(item in {"__first__", "__all__", "__auto__"} for item in configured):
            if discovered:
                matched: list[str] = []
                for item in configured:
                    hit = self._match_supplier_name(item, discovered)
                    if hit and hit not in matched:
                        matched.append(hit)
                    else:
                        matched.append(item)
                print(f"[猫超] 实时库存供应商按账号配置匹配: 配置={len(configured)} / 命中={len(matched)}")
                return matched
            return configured

        if discovered:
            print(f"[猫超] 已自动发现实时库存供应商数: {len(discovered)}")
            return discovered

        if configured:
            return configured
        return ["__first__"]

    def _discover_realtime_supplier_names(self, page: Any) -> list[str]:
        def collect() -> list[str]:
            candidates: list[str] = []
            seen: set[str] = set()
            for _, text in self._visible_realtime_supplier_items(page):
                if not text or text in seen:
                    continue
                if text in {"请选择", "全部"}:
                    continue
                seen.add(text)
                candidates.append(text)
            return candidates

        if not self._open_realtime_supplier_dropdown(page, optional=True):
            return []

        candidates = collect()
        if not candidates:
            self._wait_quiet(page, 1500)
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            if self._open_realtime_supplier_dropdown(page, optional=True):
                candidates = collect()
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return candidates

    def _visible_realtime_supplier_items(self, page: Any) -> list[tuple[Any, str]]:
        selectors = (
            ".next-overlay-wrapper:visible .next-select-menu [role='option']",
            ".next-overlay-wrapper:visible .next-select-menu .next-menu-item",
            ".next-overlay-wrapper:visible [role='option']",
            ".next-select-menu:visible [role='option']",
            ".next-select-menu:visible .next-menu-item",
        )
        items: list[tuple[Any, str]] = []
        seen: set[str] = set()
        for scope in self._iter_scopes(page):
            for selector in selectors:
                try:
                    locator = scope.locator(selector)
                    count = min(locator.count(), 100)
                except Exception:
                    continue
                for idx in range(count):
                    item = locator.nth(idx)
                    try:
                        if not item.is_visible(timeout=200):
                            continue
                        text = _clean_text(item.inner_text(timeout=300))
                    except Exception:
                        continue
                    if not text or text in seen:
                        continue
                    seen.add(text)
                    items.append((item, text))
        return items

    def _first_realtime_supplier_option(self, page: Any, timeout: int = 3000) -> Any | None:
        deadline = time.time() + timeout / 1000
        while time.time() < deadline:
            items = self._visible_realtime_supplier_items(page)
            if items:
                return items[0][0]
            time.sleep(0.2)
        return None

    def _click_realtime_supplier_option(self, page: Any, text: str, timeout: int = 5000) -> bool:
        target = self._normalize_supplier_text(text)
        deadline = time.time() + timeout / 1000
        while time.time() < deadline:
            best_item = None
            best_score = 0.0
            for item, candidate in self._visible_realtime_supplier_items(page):
                score = self._supplier_name_score(target, candidate)
                if score > best_score:
                    best_score = score
                    best_item = item
            if best_item is not None and best_score >= 0.78:
                try:
                    best_item.click(timeout=1000)
                except Exception:
                    try:
                        best_item.evaluate("(el) => el.click()")
                    except Exception:
                        best_item.click(timeout=1000, force=True)
                self._wait_quiet(page, 800)
                return True
            time.sleep(0.2)
        return False

    def _match_supplier_name(self, expected: str, candidates: list[str]) -> str:
        expected_norm = self._normalize_supplier_text(expected)
        best_candidate = ""
        best_score = 0.0
        for candidate in candidates:
            score = self._supplier_name_score(expected_norm, candidate)
            if score > best_score:
                best_score = score
                best_candidate = candidate
        return best_candidate if best_score >= 0.78 else ""

    def _normalize_supplier_text(self, value: str) -> str:
        text = _clean_text(value)
        if not text:
            return ""
        text = re.sub(r"[\(（\[].*?[\)）\]]", "", text)
        text = re.sub(r"[\s\-_/／·—–,，.。]+", "", text)
        return text

    def _supplier_name_matches(self, expected: str, actual: str) -> bool:
        return self._supplier_name_score(expected, actual) >= 0.78

    def _supplier_name_score(self, expected: str, actual: str) -> float:
        expected_norm = self._normalize_supplier_text(expected)
        actual_norm = self._normalize_supplier_text(actual)
        if not expected_norm or not actual_norm:
            return 0.0
        if expected_norm == actual_norm:
            return 1.0
        score = SequenceMatcher(None, expected_norm, actual_norm).ratio()
        if expected_norm in actual_norm or actual_norm in expected_norm:
            score = max(score, 0.9)
        return score

    def _export_realtime_supplier(self, page: Any, account: Account, supplier: str) -> RunResult:
        started = datetime.now().isoformat(timespec="seconds")
        self._click(page, "realtime.export_button", "导出")
        self._click(page, "realtime.export_all_option", "导出全部")
        self._wait_quiet(page, 1500)

        if self._page_has_text(page, "没有数据需要导出", timeout=1500) or self._page_has_text(page, "没有数据", timeout=1000):
            note = f"{supplier} 已尝试后台导出，平台提示无数据"
        else:
            note = f"{supplier} 已发起后台导出"
        print(f"[猫超] 实时库存: {note}")
        finished = datetime.now().isoformat(timespec="seconds")
        return RunResult(
            task="realtime-inventory",
            title=TASKS["realtime-inventory"]["title"],
            account=account.key,
            status="ok",
            note=note,
            started_at=started,
            finished_at=finished,
        )

    def _select_purchase_statuses(
        self,
        page: Any,
        statuses: list[str],
        optional_statuses: list[str] | None = None,
        strict: bool = True,
        field_selector_key: str = "purchase.po_status_field",
        field_label: str = "采购单状态",
    ) -> None:
        optional = set(optional_statuses or [])
        for status in statuses + list(optional):
            self._click(page, field_selector_key, field_label)
            self._wait_quiet(page, 600)
            ok = self._click_text(page, status, timeout=3000, optional=True, loose=True)
            if not ok and status not in optional:
                if strict:
                    raise RuntimeError(f"{field_label}下拉中找不到: {status}")
                print(f"[猫超] {field_label}未找到，已跳过: {status}")
            if not ok and status in optional:
                print(f"[猫超] 可选状态未找到，已跳过: {status}")
            self._wait_quiet(page, 500)
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass

    def _select_first_purchase_status(
        self,
        page: Any,
        statuses: list[str],
        field_selector_key: str = "purchase.po_status_field",
        field_label: str = "采购单状态",
    ) -> str:
        tried: list[str] = []
        for status in statuses:
            tried.append(status)
            self._click(page, field_selector_key, field_label)
            self._wait_quiet(page, 600)
            if self._click_text(page, status, timeout=3000, optional=True, loose=True):
                print(f"[猫超] {field_label}已选择: {status}")
                return status
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            self._wait_quiet(page, 300)
        raise RuntimeError(f"{field_label}下拉中找不到: {', '.join(tried)}")

    def _fill_last_two_months(self, page: Any) -> None:
        if not self._selector_visible(page, "po_list.start_date_input", timeout=1000):
            print("[猫超] 创建时间筛选未展开，使用页面默认日期范围。")
            return
        today = date.today()
        start = _months_ago(today, 2)
        self._fill(page, "po_list.start_date_input", start.strftime("%Y-%m-%d"), "创建开始时间")
        self._fill(page, "po_list.end_date_input", today.strftime("%Y-%m-%d"), "创建结束时间")
        self._click_optional(page, "po_list.date_confirm_button", "时间确定")

    def _reset_transfer_filters(self, page: Any) -> None:
        self._click_text(page, "重置", timeout=1500, optional=True)
        self._clear_visible_date_inputs(page)
        self._wait_quiet(page, 1000)

    def _clear_visible_date_inputs(self, page: Any) -> None:
        selectors = (
            "input[placeholder*='日期']",
            "input[placeholder*='时间']",
            "input[placeholder*='开始']",
            "input[placeholder*='结束']",
            "input[placeholder*='YYYY']",
            "input[placeholder*='yyyy']",
        )
        for selector in selectors:
            for scope in self._iter_scopes(page):
                try:
                    locator = scope.locator(selector)
                    count = min(locator.count(), 50)
                except Exception:
                    continue
                for idx in range(count):
                    item = locator.nth(idx)
                    try:
                        if item.is_visible(timeout=100):
                            self._set_input_value(item, "")
                    except Exception:
                        continue

    def _page_has_no_items(self, page: Any) -> bool:
        return self._page_has_text(page, "共 0 项", timeout=1000) or self._page_has_text(page, "共0项", timeout=500)

    def _realtime_inventory_result_count(self, page: Any) -> int | None:
        counts: list[int] = []
        script = """
        () => {
          const visible = (el) => {
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return rect.width > 0 && rect.height > 0 &&
              style.visibility !== 'hidden' && style.display !== 'none' &&
              Number(style.opacity || 1) > 0;
          };
          const counts = [];
          for (const el of document.querySelectorAll('body *')) {
            if (!visible(el)) continue;
            const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            if (!text || text.length > 100) continue;
            const match = text.match(/共\\s*([0-9,]+)\\s*项/);
            if (!match) continue;
            const count = Number(match[1].replace(/,/g, ''));
            if (Number.isFinite(count)) counts.push(count);
          }
          return counts;
        }
        """
        for scope in self._iter_scopes(page):
            try:
                values = scope.evaluate(script)
            except Exception:
                continue
            for value in values or []:
                try:
                    counts.append(int(value))
                except Exception:
                    continue
        if not counts:
            return None
        return max(counts)

    def _no_data_result(self, task_key: str, account: Account, note: str) -> RunResult:
        print(f"[猫超] {note}")
        started = datetime.now().isoformat(timespec="seconds")
        finished = datetime.now().isoformat(timespec="seconds")
        return RunResult(
            task=task_key,
            title=TASKS[task_key]["title"],
            account=account.key,
            status="ok",
            note=note,
            started_at=started,
            finished_at=finished,
        )

    def _unclick_current_page_only_if_present(self, page: Any) -> None:
        selector = self._selector_optional("po_list.current_page_only_checkbox")
        if not selector:
            return
        try:
            locator = page.locator(_pw_selector(selector)).first
            locator.wait_for(state="visible", timeout=1000)
            checked = locator.is_checked()
            if checked:
                locator.click(timeout=1000)
                print("[猫超] 已取消勾选“只下载当前页”。")
        except Exception:
            pass

    def _download_and_clean(
        self,
        page: Any,
        task_key: str,
        account: Account,
        file_task_id_contains: str = "",
        prefix_extra: str = "",
        note: str = "",
        task_wait_timeout_sec: int | None = None,
    ) -> RunResult:
        task = TASKS[task_key]
        started = datetime.now().isoformat(timespec="seconds")
        raw_dir, cleaned_dir = self._account_data_dirs(account)
        raw_dir.mkdir(parents=True, exist_ok=True)
        cleaned_dir.mkdir(parents=True, exist_ok=True)

        try:
            raw_file = self._wait_and_click_task_download(
                page,
                account,
                raw_dir,
                task_key,
                task["file_task_text"],
                task["prefix"],
                file_task_id_contains=file_task_id_contains,
                prefix_extra=prefix_extra,
                task_wait_timeout_sec=task_wait_timeout_sec,
            )
        except RuntimeError as exc:
            if self._is_null_download_error(exc):
                self._dismiss_notification_center(page)
                return self._no_data_result(task_key, account, f"{task['title']} 文件任务返回 null/无下载文件，已跳过: {exc}")
            raise
        cleaned_file = self._clean_file(task_key, raw_file, cleaned_dir)
        self._dismiss_notification_center(page)
        finished = datetime.now().isoformat(timespec="seconds")
        print(f"[猫超] 完成: {task['title']} -> {cleaned_file}")
        return RunResult(
            task=task_key,
            title=task["title"],
            account=account.key,
            status="ok",
            raw_file=str(raw_file),
            cleaned_file=str(cleaned_file),
            started_at=started,
            finished_at=finished,
            note=note,
        )

    def _wait_and_click_task_download(
        self,
        page: Any,
        account: Account,
        raw_dir: Path,
        task_key: str,
        file_task_text: str,
        prefix: str,
        file_task_id_contains: str = "",
        prefix_extra: str = "",
        task_wait_timeout_sec: int | None = None,
    ) -> Path:
        wait_started = time.time()
        file_task_texts = self._file_task_text_candidates(task_key, file_task_text)
        print(f"[猫超] 等待文件任务完成: {' / '.join(file_task_texts)}")
        locator = self._file_download_locator(page, file_task_texts, file_task_id_contains)
        if task_key == "realtime-inventory":
            try:
                if not locator.count():
                    locator = page.locator(
                        "div.river-notification-center_file a:has-text(\"下载\")"
                    )
            except Exception:
                pass
        click_target = self._first_visible_in_locator(locator, timeout=3000)
        realtime_fallback_used = False
        if click_target is None and task_key == "realtime-inventory":
            realtime_locator = self._visible_locator(
                page,
                "div.river-notification-center_file a:has-text(\"下载\")",
                "实时库存下载链接",
                timeout=3000,
            )
            if realtime_locator is not None:
                locator = realtime_locator
                click_target = realtime_locator
                realtime_fallback_used = True
        wait_timeout = task_wait_timeout_sec if task_wait_timeout_sec is not None else min(self.settings.task_timeout_sec, 45)
        deadline = time.time() + wait_timeout
        while time.time() < deadline and click_target is None:
            click_target = self._first_visible_in_locator(locator, timeout=1000)
            if click_target is None and task_key == "realtime-inventory" and not realtime_fallback_used:
                realtime_locator = self._visible_locator(
                    page,
                    "div.river-notification-center_file a:has-text(\"下载\")",
                    "实时库存下载链接",
                    timeout=1000,
                )
                if realtime_locator is not None:
                    locator = realtime_locator
                    click_target = realtime_locator
                    realtime_fallback_used = True
                    break
            time.sleep(self.settings.poll_interval_sec)
        if click_target is None:
            existing = self._latest_matching_download(account.download_dir, task_key, wait_started - 30)
            if existing is None:
                existing = self._latest_matching_download(account.download_dir, task_key, 0)
            if existing is not None:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                name_parts = [prefix, timestamp]
                if prefix_extra:
                    name_parts.append(prefix_extra)
                local_prefix = "_".join(name_parts)
                target = self._unique_path(raw_dir / f"{local_prefix}_{existing.name}")
                if existing.resolve() != target.resolve():
                    shutil.copy2(existing, target)
                return target
            raise RuntimeError(f"等待文件任务下载按钮超时: {' / '.join(file_task_texts)}")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        name_parts = [prefix, timestamp]
        if prefix_extra:
            name_parts.append(prefix_extra)
        local_prefix = "_".join(name_parts)
        before = self._download_snapshot(account.download_dir)

        try:
            download_event_timeout = min(self.settings.download_timeout_sec, 10) * 1000
            with page.expect_download(timeout=download_event_timeout) as download_info:
                try:
                    click_target.evaluate("(el) => el.click()")
                except Exception:
                    click_target.click(timeout=5000)
            download = download_info.value
            suggested = _slug(download.suggested_filename or f"{task_key}.download")
            target = self._unique_path(raw_dir / f"{local_prefix}_{suggested}")
            download.save_as(str(target))
            if target.stat().st_size > 0:
                return target
            print("[猫超] Playwright 保存的下载文件为空，改用浏览器下载目录文件。")
            try:
                target.unlink()
            except Exception:
                pass
            downloaded = self._wait_new_download(account.download_dir, before)
            target = self._unique_path(raw_dir / f"{local_prefix}_{downloaded.name}")
            if downloaded.resolve() != target.resolve():
                shutil.copy2(downloaded, target)
            return target
        except Exception as exc:
            print(f"[猫超] Playwright download 事件未捕获，改用目录轮询: {exc}")
            try:
                try:
                    click_target.evaluate("(el) => el.click()")
                except Exception:
                    click_target.click(timeout=5000)
            except Exception:
                pass
            downloaded = self._wait_new_download(account.download_dir, before)
            target = self._unique_path(raw_dir / f"{local_prefix}_{downloaded.name}")
            if downloaded.resolve() != target.resolve():
                shutil.copy2(downloaded, target)
            return target

    def _file_download_locator(self, page: Any, file_task_texts: Iterable[str], file_task_id_contains: str = "") -> Any:
        if file_task_id_contains:
            by_id = (
                f"//*[contains(@id, {_xpath_literal(file_task_id_contains)})]"
                "//a[contains(normalize-space(.), '下载') or .//span[contains(normalize-space(.), '下载')]]"
            )
            locator = page.locator(f"xpath={by_id}")
            try:
                if locator.count():
                    return locator
            except Exception:
                pass

        text_conditions = [
            f"contains(normalize-space(.), {_xpath_literal(text)})"
            for text in file_task_texts
            if _clean_text(text)
        ]
        if not text_conditions:
            text_conditions = ["false()"]
        by_text = (
            f"//*[{' or '.join(text_conditions)}]"
            "/ancestor::*[self::li or contains(@id, 'fileTask')][1]"
            "//a[contains(normalize-space(.), '下载') or .//span[contains(normalize-space(.), '下载')]]"
        )
        locator = page.locator(f"xpath={by_text}")
        try:
            if locator.count():
                return locator
        except Exception:
            pass

        return locator

    def _file_task_text_candidates(self, task_key: str, file_task_text: str) -> list[str]:
        candidates: list[str] = []

        def add(value: str) -> None:
            text = _clean_text(value)
            if text and text not in candidates:
                candidates.append(text)

        add(file_task_text)
        if file_task_text.startswith("导出 "):
            add(file_task_text.removeprefix("导出 "))
        else:
            add(f"导出 {file_task_text}")

        aliases = {
            "channel-goods": ["货品生命周期导出结果", "导出 库位明细"],
            "transfer-order": ["调拨明细数据导出", "导出 调拨单货品明细"],
        }
        for alias in aliases.get(task_key, []):
            add(alias)
        return candidates

    def _latest_matching_download(self, directory: Path, task_key: str, modified_after: float) -> Path | None:
        keywords = self._expected_download_keywords(task_key)
        if not keywords:
            return None
        try:
            files = [
                path
                for path in directory.iterdir()
                if path.is_file()
                and path.suffix not in {".crdownload", ".tmp"}
                and path.stat().st_mtime >= modified_after
                and any(keyword in path.name for keyword in keywords)
            ]
        except Exception:
            return None
        if not files:
            return None
        latest = max(files, key=lambda path: path.stat().st_mtime)
        print(f"[猫超] 文件任务按钮未出现，改用下载目录已生成文件: {latest.name}")
        return latest

    def _expected_download_keywords(self, task_key: str) -> list[str]:
        return {
            "realtime-inventory": ["实时库存"],
            "pincang-detail": ["品仓明细表"],
            "system-order": ["PO明细确认分页导出"],
            "po-list": ["PO明细分页导出"],
            "channel-goods": ["货品生命周期导出结果"],
            "transfer-order": ["调拨明细数据导出", "调拨单货品明细"],
        }.get(task_key, [])

    def _is_null_download_error(self, exc: RuntimeError) -> bool:
        message = str(exc)
        return "等待文件任务下载按钮超时" in message or "下载目录未出现新文件" in message

    def _first_visible_in_locator(self, locator: Any, timeout: int = 1000) -> Any | None:
        deadline = time.time() + timeout / 1000
        while time.time() < deadline:
            try:
                count = min(locator.count(), 100)
            except Exception:
                time.sleep(0.2)
                continue
            for idx in range(count):
                item = locator.nth(idx)
                try:
                    if item.is_visible(timeout=200):
                        return item
                except Exception:
                    continue
            time.sleep(0.2)
        return None

    def _download_snapshot(self, directory: Path) -> dict[Path, float]:
        directory.mkdir(parents=True, exist_ok=True)
        return {path: path.stat().st_mtime for path in directory.iterdir() if path.is_file()}

    def _wait_new_download(self, directory: Path, before: dict[Path, float]) -> Path:
        deadline = time.time() + self.settings.download_timeout_sec
        latest: Path | None = None
        while time.time() < deadline:
            candidates = []
            for path in directory.iterdir():
                if not path.is_file():
                    continue
                if path.suffix in {".crdownload", ".tmp"}:
                    continue
                if path not in before or path.stat().st_mtime > before.get(path, 0):
                    candidates.append(path)
            if candidates:
                latest = max(candidates, key=lambda p: p.stat().st_mtime)
                size1 = latest.stat().st_size
                time.sleep(0.8)
                if latest.exists() and latest.stat().st_size == size1:
                    return latest
            time.sleep(self.settings.poll_interval_sec)
        raise RuntimeError(f"下载目录未出现新文件: {directory}")

    def _clean_file(self, task_key: str, raw_file: Path, cleaned_dir: Path) -> Path:
        suffix = raw_file.suffix.lower()
        target = self._unique_path(cleaned_dir / raw_file.name)
        if suffix == ".csv":
            return self._clean_csv(task_key, raw_file, target)
        if suffix == ".xlsx":
            return self._clean_xlsx(task_key, raw_file, target)
        shutil.copy2(raw_file, target)
        print(f"[猫超] 暂不识别该格式，仅复制原文件: {raw_file.name}")
        return target

    def _clean_csv(self, task_key: str, source: Path, target: Path) -> Path:
        text = self._read_text_auto(source)
        rows = list(csv.reader(text.splitlines()))
        rows = self._clean_rows(task_key, rows)
        with target.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerows(rows)
        return target

    def _clean_xlsx(self, task_key: str, source: Path, target: Path) -> Path:
        try:
            from openpyxl import load_workbook
        except ImportError as exc:
            raise RuntimeError("清洗 xlsx 需要 openpyxl，请安装 requirements.txt") from exc

        workbook = load_workbook(source)
        preserve_columns = set(self.settings.cleanup.get("preserve_columns", []))
        for sheet in workbook.worksheets:
            headers = {
                idx: _clean_text(cell.value)
                for idx, cell in enumerate(sheet[1], start=1)
            } if sheet.max_row >= 1 else {}

            if task_key == "transfer-order":
                self._delete_transfer_rows(sheet, headers)

            for row in sheet.iter_rows():
                for cell in row:
                    header = headers.get(cell.column, "")
                    if isinstance(cell.value, str):
                        cell.value = self._normalize_cell_value(
                            cell.value,
                            preserve=header in preserve_columns,
                        )
        workbook.save(target)
        return target

    def _clean_rows(self, task_key: str, rows: list[list[Any]]) -> list[list[Any]]:
        if not rows:
            return rows
        headers = [_clean_text(v) for v in rows[0]]
        preserve_columns = set(self.settings.cleanup.get("preserve_columns", []))
        keep_rows = [rows[0]]
        status_col = self._transfer_status_index(headers) if task_key == "transfer-order" else None
        drop_statuses = set(self.settings.cleanup.get("transfer_drop_statuses", ["全部出库全部入库"]))
        for row in rows[1:]:
            if status_col is not None and status_col < len(row):
                if _clean_text(row[status_col]) in drop_statuses:
                    continue
            cleaned = []
            for idx, value in enumerate(row):
                cleaned.append(
                    self._normalize_cell_value(
                        value,
                        preserve=idx < len(headers) and headers[idx] in preserve_columns,
                    )
                )
            keep_rows.append(cleaned)
        return keep_rows

    def _delete_transfer_rows(self, sheet: Any, headers: dict[int, str]) -> None:
        status_col = None
        for col, header in headers.items():
            if header == self.settings.cleanup.get("transfer_status_column", "调拨单状态"):
                status_col = col
                break
        if status_col is None:
            print("[猫超] 调拨单未找到“调拨单状态”列，跳过状态过滤。")
            return
        drop_statuses = set(self.settings.cleanup.get("transfer_drop_statuses", ["全部出库全部入库"]))
        delete_rows = []
        for row_idx in range(2, sheet.max_row + 1):
            if _clean_text(sheet.cell(row_idx, status_col).value) in drop_statuses:
                delete_rows.append(row_idx)
        for row_idx in reversed(delete_rows):
            sheet.delete_rows(row_idx)
        if delete_rows:
            print(f"[猫超] 调拨单已删除状态过滤行数: {len(delete_rows)}")

    def _transfer_status_index(self, headers: list[str]) -> int | None:
        wanted = self.settings.cleanup.get("transfer_status_column", "调拨单状态")
        for idx, header in enumerate(headers):
            if header == wanted:
                return idx
        print("[猫超] 调拨单 CSV 未找到“调拨单状态”列，跳过状态过滤。")
        return None

    def _normalize_cell_value(self, value: Any, preserve: bool = False) -> Any:
        text = _clean_text(value)
        if preserve or text == "":
            return text
        text = text.replace("\u3000", " ").strip()
        text_no_comma = text.replace(",", "")
        if re.fullmatch(r"-?\d+(\.0+)?", text_no_comma):
            return int(float(text_no_comma))
        if re.fullmatch(r"-?\d+\.\d+", text_no_comma):
            return float(text_no_comma)
        return text

    def _read_text_auto(self, path: Path) -> str:
        raw = path.read_bytes()
        for encoding in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    def _unique_path(self, path: Path) -> Path:
        if not path.exists():
            return path
        stem = path.stem
        suffix = path.suffix
        for idx in range(1, 1000):
            candidate = path.with_name(f"{stem}_{idx}{suffix}")
            if not candidate.exists():
                return candidate
        raise RuntimeError(f"无法生成唯一文件名: {path}")

    def _selector(self, dotted_key: str) -> str:
        value = self._selector_optional(dotted_key)
        if not value:
            raise RuntimeError(f"config 缺少 XPath/selectors.{dotted_key}")
        return value

    def _selector_optional(self, dotted_key: str) -> str:
        node: Any = self._active_selectors
        for part in dotted_key.split("."):
            if not isinstance(node, dict) or part not in node:
                return ""
            node = node[part]
        return _clean_text(node)

    def _list_config(self, dotted_key: str, default: list[str]) -> list[str]:
        node: Any = self._active_selectors
        for part in dotted_key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        if isinstance(node, list):
            return [_clean_text(v) for v in node if _clean_text(v)]
        return default

    def _click(self, page: Any, selector_key: str, label: str, timeout: int = 10000) -> None:
        selector = self._selector(selector_key)
        locator = self._visible_locator(page, selector, label, timeout=timeout)
        if locator is None:
            raise RuntimeError(f"找不到{label}: selectors.{selector_key}")
        try:
            locator.click(timeout=timeout)
        except Exception:
            try:
                locator.evaluate("(el) => el.click()")
            except Exception:
                locator.click(timeout=timeout, force=True)

    def _click_optional(self, page: Any, selector_key: str, label: str, timeout: int = 2000) -> bool:
        selector = self._selector_optional(selector_key)
        if not selector:
            return False
        try:
            locator = self._visible_locator(page, selector, label, timeout=timeout)
            if locator is None:
                return False
            try:
                locator.click(timeout=timeout)
            except Exception:
                try:
                    locator.evaluate("(el) => el.click()")
                except Exception:
                    locator.click(timeout=timeout, force=True)
            return True
        except Exception:
            pass
        return False

    def _click_force(self, page: Any, selector_key: str, label: str, timeout: int = 10000) -> None:
        selector = self._selector(selector_key)
        locator = self._visible_locator(page, selector, label, timeout=timeout)
        if locator is None:
            raise RuntimeError(f"找不到{label}: selectors.{selector_key}")
        locator.click(timeout=timeout, force=True)

    def _frame_with_selectors(self, page: Any, selector_keys: tuple[str, ...], timeout: int = 5000) -> Any | None:
        selectors = [self._selector_optional(key) for key in selector_keys]
        if any(not selector for selector in selectors):
            return None

        deadline = time.time() + timeout / 1000
        while time.time() < deadline:
            for frame in getattr(page, "frames", []) or []:
                try:
                    matched = True
                    for selector in selectors:
                        locator = self._scoped_visible_locator(frame, selector, timeout=200)
                        if locator is None:
                            matched = False
                            break
                    if matched:
                        return frame
                except Exception:
                    continue
            time.sleep(0.2)
        return None

    def _scope_fill(self, scope: Any, selector_key: str, value: str, label: str, timeout: int = 10000) -> None:
        selector = self._selector(selector_key)
        locator = self._scoped_visible_locator(scope, selector, timeout=timeout)
        if locator is None:
            raise RuntimeError(f"找不到{label}: selectors.{selector_key}")
        locator.fill(value, timeout=timeout)

    def _scope_click(self, scope: Any, selector_key: str, label: str, timeout: int = 10000) -> None:
        selector = self._selector(selector_key)
        locator = self._scoped_visible_locator(scope, selector, timeout=timeout)
        if locator is None:
            raise RuntimeError(f"找不到{label}: selectors.{selector_key}")
        try:
            locator.click(timeout=timeout)
        except Exception:
            locator.click(timeout=2000, force=True)

    def _fill(self, page: Any, selector_key: str, value: str, label: str, timeout: int = 10000) -> None:
        selector = self._selector(selector_key)
        locator = self._visible_locator(page, selector, label, timeout=timeout)
        if locator is None:
            raise RuntimeError(f"找不到{label}: selectors.{selector_key}")
        readonly = False
        try:
            readonly = bool(locator.evaluate("(el) => !!el.readOnly || el.hasAttribute('readonly')"))
        except Exception:
            pass
        if readonly:
            if self._set_input_value(locator, value):
                return
        try:
            locator.fill(value, timeout=timeout)
            return
        except Exception:
            if self._set_input_value(locator, value):
                return
            raise

    def _set_input_value(self, locator: Any, value: str) -> bool:
        try:
            locator.evaluate(
                """
                (el, nextValue) => {
                  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                  setter.call(el, nextValue);
                  el.dispatchEvent(new Event('input', { bubbles: true }));
                  el.dispatchEvent(new Event('change', { bubbles: true }));
                }
                """,
                value,
            )
            return True
        except Exception:
            return False

    def _selector_visible(self, page: Any, selector_key: str, timeout: int = 1000) -> bool:
        selector = self._selector_optional(selector_key)
        if not selector:
            return False
        return self._visible_locator(page, selector, selector_key, timeout=timeout) is not None

    def _iter_scopes(self, page: Any) -> Iterable[Any]:
        yield page
        for frame in getattr(page, "frames", []) or []:
            yield frame

    def _visible_locator(self, page: Any, selector: str, label: str, timeout: int = 10000) -> Any | None:
        pw_selector = _pw_selector(selector)
        deadline = time.time() + timeout / 1000
        while time.time() < deadline:
            for scope in self._iter_scopes(page):
                try:
                    locator = scope.locator(pw_selector)
                    count = min(locator.count(), 100)
                except Exception:
                    continue
                # Some Tmall pages render duplicate controls with the same text.
                # Prefer the first visible match instead of the first DOM node.
                for idx in range(count):
                    item = locator.nth(idx)
                    try:
                        if item.is_visible(timeout=200):
                            return item
                    except Exception:
                        continue
            time.sleep(0.2)
        return None

    def _scoped_visible_locator(self, scope: Any, selector: str, timeout: int = 10000) -> Any | None:
        pw_selector = _pw_selector(selector)
        deadline = time.time() + timeout / 1000
        while time.time() < deadline:
            try:
                locator = scope.locator(pw_selector)
                count = min(locator.count(), 100)
            except Exception:
                time.sleep(0.2)
                continue
            for idx in range(count):
                item = locator.nth(idx)
                try:
                    if item.is_visible(timeout=200):
                        return item
                except Exception:
                    continue
            time.sleep(0.2)
        return None

    def _click_text(
        self,
        page: Any,
        text: str,
        timeout: int = 5000,
        optional: bool = False,
        loose: bool = False,
    ) -> bool:
        literal = _xpath_literal(text)
        compact_target = self._compact_text(text) if loose else ""
        if loose:
            xpath = "//*[self::li or self::div or self::span or self::a or self::button or self::label]"
        else:
            xpath = (
                "//*[self::li or self::div or self::span or self::a or self::button or self::label]"
                f"[contains(normalize-space(.), {literal})]"
            )
        option_selectors = (
            ".next-overlay-wrapper:visible .next-menu-item",
            ".next-overlay-wrapper:visible [role='option']",
            ".next-select-menu:visible .next-menu-item",
            ".next-select-menu:visible [role='option']",
            "[role='listbox']:visible [role='option']",
        )
        deadline = time.time() + timeout / 1000
        while time.time() < deadline:
            if loose:
                for scope in self._iter_scopes(page):
                    for selector in option_selectors:
                        try:
                            locator = scope.locator(selector)
                            count = min(locator.count(), 100)
                        except Exception:
                            continue
                        for idx in range(count):
                            item = locator.nth(idx)
                            try:
                                if not item.is_visible(timeout=200):
                                    continue
                                candidate = self._compact_text(item.inner_text(timeout=300))
                            except Exception:
                                continue
                            if not candidate or compact_target not in candidate:
                                continue
                            try:
                                item.evaluate("(el) => el.click()")
                            except Exception:
                                try:
                                    item.scroll_into_view_if_needed(timeout=1000)
                                    item.click(timeout=3000, force=True)
                                except Exception:
                                    item.click(timeout=3000, force=True)
                            return True
                    try:
                        exact_locator = scope.get_by_text(text, exact=True)
                        count = min(exact_locator.count(), 100)
                    except Exception:
                        count = 0
                    for idx in range(count):
                        item = exact_locator.nth(idx)
                        try:
                            if not item.is_visible(timeout=200):
                                continue
                            item.click(timeout=1000)
                            return True
                        except Exception:
                            continue
            for scope in self._iter_scopes(page):
                try:
                    locator = scope.locator(f"xpath={xpath}")
                    count = min(locator.count(), 50)
                    for idx in range(count):
                        item = locator.nth(idx)
                        if not item.is_visible(timeout=200):
                            continue
                        if loose:
                            try:
                                candidate = self._compact_text(item.inner_text(timeout=300))
                            except Exception:
                                candidate = ""
                            if not candidate or compact_target not in candidate:
                                continue
                        item.click(timeout=1000)
                        return True
                except Exception:
                    continue
            time.sleep(0.2)
        if optional:
            return False
        raise RuntimeError(f"找不到文本选项: {text}")

    def _compact_text(self, value: str) -> str:
        text = _clean_text(value)
        if not text:
            return ""
        return re.sub(r"[\s\-_/／·—–,，.。()（）\[\]【】]+", "", text)

    def _page_has_text(self, page: Any, text: str, timeout: int = 1000) -> bool:
        return self._visible_locator(page, f"text={text}", text, timeout=timeout) is not None

    def _wait_quiet(self, page: Any, timeout_ms: int = 5000) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:
            page.wait_for_timeout(min(timeout_ms, 2000))

    def _screenshot(self, page: Any, name: str) -> Path:
        self.settings.screenshot_dir.mkdir(parents=True, exist_ok=True)
        path = self.settings.screenshot_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{_slug(name)}.png"
        try:
            page.screenshot(path=str(path), full_page=True)
        except Exception:
            pass
        return path

    def _capture_error_screenshot(self, page: Any | None, name: str) -> str:
        if page is None:
            return ""
        try:
            shot = self._screenshot(page, name)
            print(f"[猫超] 已保存错误截图: {shot}")
            return str(shot)
        except Exception as exc:
            print(f"[猫超] 截图失败: {exc}")
            return ""

    def _write_manifest(self, results: list[RunResult]) -> None:
        self.settings.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.settings.log_dir / f"maochao_download_manifest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "config": str(self.settings.config_path),
            "results": [asdict(item) for item in results],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[猫超] manifest 已写入: {path}")


def dry_run(settings: Settings, tasks: list[str], account_keys: list[str] | None = None) -> int:
    accounts = settings.accounts
    if account_keys:
        wanted = set(account_keys)
        accounts = [account for account in accounts if account.key in wanted]
    print(f"[dry-run] config: {settings.config_path}")
    print(f"[dry-run] accounts_source: {settings.accounts_source}")
    print(f"[dry-run] accounts_db: {settings.accounts_db_path}")
    print(f"[dry-run] login_url: {settings.login_url or '未配置'}")
    print(f"[dry-run] tasks: {', '.join(tasks)}")
    print(f"[dry-run] accounts: {len(accounts)}")
    seen_ports: dict[int, str] = {}
    for account in accounts:
        task_list = [task for task in tasks if task in account.tasks]
        password_status = "已填" if account.password else "未填"
        print(
            f"  - {account.key}: port={account.port}, tasks={task_list or '无'}, "
            f"profile={account.profile_dir}, password={password_status}"
        )
        if account.port in seen_ports:
            print(f"[dry-run] 端口冲突: {seen_ports[account.port]} 与 {account.key} 都使用 {account.port}")
            return 1
        seen_ports[account.port] = account.key

    missing = []
    for task in tasks:
        for selector_key in REQUIRED_SELECTORS[task]:
            for account in accounts:
                if task not in account.tasks:
                    continue
                context = account_context(
                    {
                        "key": account.key,
                        "name": account.name,
                        "username": account.username,
                        "password": account.password,
                        "port": account.port,
                        "profile_dir": str(account.profile_dir),
                        "download_dir": str(account.download_dir),
                        "supplier_names": account.supplier_names,
                        "tasks": account.tasks,
                        "note": account.note,
                        "xpath_vars": account.xpath_vars,
                        "selector_overrides": account.selector_overrides,
                        "enabled": account.enabled,
                    }
                )
                rendered = deep_merge(render_tree(settings.selectors, context), render_tree(account.selector_overrides, context))
                node: Any = rendered
                ok = True
                for part in selector_key.split("."):
                    if not isinstance(node, dict) or part not in node or not _clean_text(node.get(part)):
                        ok = False
                        break
                    node = node[part]
                if not ok:
                    missing.append(f"{task}: {account.key}: selectors.{selector_key}")
                for unresolved_key in unresolved_templates(rendered):
                    missing.append(f"{task}: {account.key}: template 未展开 -> {unresolved_key}")
    if missing:
        print("[dry-run] 缺少以下 XPath/selector：")
        for item in sorted(set(missing)):
            print(f"  - {item}")
        return 1
    print("[dry-run] 基础配置检查通过。")
    return 0


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help="配置文件路径，默认使用 config.example.json；实际运行建议复制为 config.local.json",
    )
    parser.add_argument("--task", action="append", help="任务 key/编号，可重复；默认 all")
    parser.add_argument("--account", action="append", help="账号 key，可重复；默认全部配置账号")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="猫超补货数据源下载 RPA")
    sub = parser.add_subparsers(dest="cmd")

    p_dry = sub.add_parser("dry-run", help="只检查配置和 XPath 完整性")
    add_common_args(p_dry)

    p_login = sub.add_parser("login", help="打开/接管账号浏览器，用于首次人工登录")
    add_common_args(p_login)
    p_login.add_argument("--headed", action="store_true", help="强制显示浏览器窗口")

    p_run = sub.add_parser("run", help="顺序执行猫超数据源下载")
    add_common_args(p_run)
    p_run.add_argument("--manual-login", action="store_true", help="登录后暂停，方便处理验证码/手机确认")
    p_run.add_argument("--headed", action="store_true", help="强制显示浏览器窗口")
    p_run.add_argument("--force-account-tasks", action="store_true", help="指定账号时临时执行命令行任务，忽略账号库任务归属")

    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 0

    settings = load_settings(Path(args.config))
    tasks = selected_tasks(args.task)

    if args.cmd == "dry-run":
        return dry_run(settings, tasks, args.account)

    headless = False if getattr(args, "headed", False) else None
    rpa = MaochaoRPA(
        settings,
        manual_login=getattr(args, "manual_login", False),
        headless=headless,
    )
    if args.cmd == "login":
        rpa.manual_login = True
        rpa.login_only(args.account)
        return 0
    if args.cmd == "run":
        results = rpa.run(tasks, args.account, force_account_tasks=getattr(args, "force_account_tasks", False))
        failed = [item for item in results if item.status != "ok"]
        print(f"[猫超] 执行完成: ok={len(results) - len(failed)}, failed={len(failed)}")
        return 1 if failed else 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
