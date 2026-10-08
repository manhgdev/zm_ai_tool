import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pipeline.core import memory_guard as guard
from pipeline.core import resources


def memory(total_mb, available_mb):
    return SimpleNamespace(total=total_mb * 1024**2, available=available_mb * 1024**2)


class MemoryGuardTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(guard.os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_large_backend_is_allowed_when_system_has_headroom(self):
        with patch.object(guard, "_system_memory_mb", return_value=(32768, 12000)), \
                patch.object(guard, "_rss_mb", return_value=10000), \
                patch.object(guard, "_release_idle") as release:
            self.assertEqual(guard.max_rss_mb(), 0)
            guard.ensure_budget()
            release.assert_not_called()

    def test_low_memory_is_soft_pressure_and_never_blocks(self):
        for total in (8192, 32768, 65536):
            reserve = 768
            boundary = total * .05 + reserve
            with self.subTest(total=total), \
                    patch.object(guard, "_system_memory_mb", return_value=(total, boundary - 1)), \
                    patch.object(guard, "_release_idle", return_value=1) as release, \
                    patch.object(guard.logger, "warning") as warning:
                guard.ensure_budget(reserve)
                release.assert_called_once_with(force=True)
                warning.assert_called_once()

    def test_rechecks_system_ram_after_releasing_idle_models(self):
        with patch.object(guard, "_system_memory_mb", side_effect=[(32768, 1000), (32768, 8000)]), \
                patch.object(guard, "_release_idle") as release:
            guard.ensure_budget()
            release.assert_called_once_with(force=True)

    def test_adaptive_workers_reduce_concurrency_without_rejecting_work(self):
        with patch.object(resources.os, "cpu_count", return_value=8), \
                patch.object(resources, "_cpu_idle_and_memory", return_value=(0.8, 0.20)):
            normal = resources.adaptive_workers(None, kind="cpu", cap=16)
        with patch.object(resources.os, "cpu_count", return_value=8), \
                patch.object(resources, "_cpu_idle_and_memory", return_value=(0.8, 0.09)):
            elevated = resources.adaptive_workers(None, kind="cpu", cap=16)
        with patch.object(resources.os, "cpu_count", return_value=8), \
                patch.object(resources, "_cpu_idle_and_memory", return_value=(0.8, 0.04)):
            critical = resources.adaptive_workers(None, kind="cpu", cap=16)
        self.assertGreater(normal, elevated)
        self.assertGreater(elevated, critical)
        self.assertGreaterEqual(critical, 1)

    def test_legacy_rss_cap_is_ignored(self):
        for value in ("", "bad", "-1", "nan", "inf", "3072"):
            with self.subTest(value=value), patch.dict(guard.os.environ, {"ZM_AI_TOOL_MAX_RSS_MB": value}):
                self.assertEqual(guard.max_rss_mb(), 0)

    def test_pressure_cleanup_does_not_release_active_model(self):
        idle, active = Mock(), Mock()
        with patch.dict(guard._entries, {}, clear=True), patch.dict(guard._active_names, {}, clear=True):
            guard.register("idle-test", idle)
            guard.register("active-test", active)
            with guard.activity("active-test"):
                guard._release_idle(force=True)
            idle.assert_called_once()
            active.assert_not_called()


if __name__ == "__main__":
    unittest.main()
