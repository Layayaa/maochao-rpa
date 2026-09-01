from __future__ import annotations

import unittest
from pathlib import Path


class WebStatusContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")

    def test_only_health_failure_marks_service_offline(self) -> None:
        self.assertEqual(self.source.count('state.health = { status: "offline", error: error.message };'), 1)
        health_request = self.source.index('health = await request("/api/health");')
        offline_assignment = self.source.index('state.health = { status: "offline", error: error.message };')
        self.assertLess(health_request, offline_assignment)

    def test_business_failure_keeps_health_and_reports_endpoint(self) -> None:
        self.assertIn("state.dataError = {", self.source)
        self.assertIn('path: error.path || ""', self.source)
        self.assertIn("数据加载异常", self.source)
        self.assertIn("online && !state.dataError", self.source)

    def test_expired_login_does_not_render_as_offline(self) -> None:
        self.assertIn("if (error.status === 401 || !state.authToken || !state.user) return;", self.source)


if __name__ == "__main__":
    unittest.main()
