"""Regression: install while cancelling must not pretend a new download started."""
from __future__ import annotations

import unittest
from unittest.mock import patch


class UpdateInstallBusyTests(unittest.TestCase):
    def test_install_while_running_returns_busy_without_clearing_cancel(self) -> None:
        from api.routes import system as sys_routes

        with sys_routes._UPDATE_LOCK:
            sys_routes._UPDATE_CANCEL.set()
            sys_routes._UPDATE_STATE.update(
                running=True,
                phase="cancelling",
                progress=0,
                message="Đang hủy cập nhật…",
                cancelRequested=True,
            )

        with patch.object(sys_routes, "_update_supported", return_value=True):
            result = sys_routes.api_update_install()

        self.assertFalse(result.get("ok"))
        self.assertTrue(result.get("busy"))
        self.assertEqual(result.get("phase"), "cancelling")
        self.assertTrue(sys_routes._UPDATE_CANCEL.is_set())

        with sys_routes._UPDATE_LOCK:
            sys_routes._UPDATE_CANCEL.clear()
            sys_routes._UPDATE_STATE.update(
                running=False,
                phase="idle",
                message="",
                cancelRequested=False,
                cancelledAt=None,
            )


if __name__ == "__main__":
    unittest.main()
