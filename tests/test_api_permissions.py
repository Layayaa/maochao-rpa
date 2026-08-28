from __future__ import annotations

import importlib
import json
import tempfile
import unittest
from pathlib import Path

import backend_core


class ApiPermissionsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        backend_core.DB_PATH = cls.root / "backend" / "rpa.db"
        backend_core.WORK_DIR = cls.root / "backend"
        backend_core.WORKER_HEARTBEAT_PATH = cls.root / "backend" / "worker_heartbeat.json"
        config = {
            "login_url": "https://example.invalid/", "chrome_executable_path": "chrome",
            "data_root": str(cls.root / "data"), "download_root": str(cls.root / "downloads"),
            "log_dir": str(cls.root / "logs"), "screenshot_dir": str(cls.root / "screenshots"),
            "accounts_source": "json", "accounts_db_path": str(cls.root / "accounts" / "accounts.db"),
            "accounts_db_key_path": str(cls.root / "accounts" / ".key"), "accounts": [],
            "direct_urls": {}, "selectors": {}, "cleanup": {},
        }
        cls.config_path = cls.root / "config.json"
        cls.config_path.write_text(json.dumps(config), encoding="utf-8")
        backend_core.DEFAULT_CONFIG_PATH = cls.config_path
        import api_server
        cls.api_server = importlib.reload(api_server)
        cls.api_server.store.account_store.ensure_initialized()
        from fastapi.testclient import TestClient
        cls.client = TestClient(cls.api_server.app)
        cls.user_a = cls.api_server.store.create_supply_chain_user("supply-a", "供应链A", "pass-a")
        cls.user_b = cls.api_server.store.create_supply_chain_user("supply-b", "供应链B", "pass-b")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        cls.temp.cleanup()

    def login(self, username: str, password: str) -> dict[str, str]:
        response = self.client.post("/api/auth/login", json={"role": "supply_chain", "username": username, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        return {"Authorization": f"Bearer {response.json()['token']}"}

    def test_supply_chain_can_edit_only_own_account(self) -> None:
        headers_a = self.login("supply-a", "pass-a")
        headers_b = self.login("supply-b", "pass-b")
        created = self.client.post("/api/accounts", headers=headers_a, json={"username": "owned-account", "password": "secret"})
        self.assertEqual(created.status_code, 200, created.text)
        account_key = created.json()["account_key"]
        own_patch = self.client.patch(f"/api/accounts/{account_key}", headers=headers_a, json={"note": "own"})
        self.assertEqual(own_patch.status_code, 200, own_patch.text)
        other_patch = self.client.patch(f"/api/accounts/{account_key}", headers=headers_b, json={"note": "other"})
        self.assertEqual(other_patch.status_code, 403, other_patch.text)
        denied_delete = self.client.delete(f"/api/accounts/{account_key}", headers=headers_a)
        self.assertEqual(denied_delete.status_code, 403, denied_delete.text)

    def test_worker_health_is_available_to_windows_service_scripts(self) -> None:
        response = self.client.get("/api/worker")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("worker_online", response.json())

    def test_supply_chain_cannot_mutate_schedules_or_users(self) -> None:
        headers = self.login("supply-a", "pass-a")
        schedule = self.client.post(
            "/api/schedules", headers=headers,
            json={"task_keys": ["channel-goods"], "all_operators": True, "enabled": True, "time_of_day": "09:00", "weekdays": [0]},
        )
        self.assertEqual(schedule.status_code, 403, schedule.text)
        users = self.client.get("/api/supply-chain-users", headers=headers)
        self.assertEqual(users.status_code, 403, users.text)

    def test_supply_chain_can_change_own_password(self) -> None:
        headers = self.login("supply-a", "pass-a")
        changed = self.client.post(
            "/api/supply-chain-users/password/change", headers=headers,
            json={"old_password": "pass-a", "new_password": "new-pass-a"},
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertTrue(self.login("supply-a", "new-pass-a")["Authorization"].startswith("Bearer "))
        self.api_server.store.update_supply_chain_user(self.user_a["user_id"], password="pass-a")

    def test_supply_chain_can_manage_only_owned_operator(self) -> None:
        headers_a = self.login("supply-a", "pass-a")
        headers_b = self.login("supply-b", "pass-b")
        created = self.client.post("/api/operators", headers=headers_a, json={"name": "运营A"})
        self.assertEqual(created.status_code, 200, created.text)
        operator_id = created.json()["operator_id"]
        self.assertEqual(created.json()["supply_chain_user_id"], self.user_a["user_id"])
        denied = self.client.patch(f"/api/operators/{operator_id}", headers=headers_b, json={"name": "越权改名"})
        self.assertEqual(denied.status_code, 403, denied.text)
        allowed = self.client.patch(f"/api/operators/{operator_id}", headers=headers_a, json={"name": "运营A-新"})
        self.assertEqual(allowed.status_code, 200, allowed.text)


if __name__ == "__main__":
    unittest.main()
