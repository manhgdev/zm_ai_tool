"""Desktop boot must not pull orchestrate/torch while importing the API app."""
from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))


_HEAVY = (
    "torch",
    "pipeline.orchestrate.asr_translate",
    "pipeline.orchestrate.dub",
    "pipeline.orchestrate.export_job",
    "pipeline.run",
    "pipeline.flow.service",
    "pipeline.automation.service",
)


class BootImportGraphTests(unittest.TestCase):
    def test_create_app_skips_heavy_modules(self) -> None:
        for name in list(sys.modules):
            if name == "torch" or name.startswith(
                (
                    "pipeline.orchestrate",
                    "pipeline.run",
                    "pipeline.flow.service",
                    "pipeline.automation.service",
                )
            ):
                del sys.modules[name]

        # Fresh package entry points so lazy __getattr__ is exercised.
        for name in ("pipeline", "pipeline.flow", "pipeline.automation", "api.app", "main"):
            sys.modules.pop(name, None)

        importlib.invalidate_caches()
        from api.app import create_app

        create_app()
        loaded = set(sys.modules)
        for name in _HEAVY:
            self.assertNotIn(name, loaded, f"{name} should stay lazy until first use")

    def test_pipeline_package_lazy_run_export(self) -> None:
        sys.modules.pop("pipeline.run", None)
        sys.modules.pop("pipeline.orchestrate.dub", None)
        import pipeline

        self.assertNotIn("pipeline.run", sys.modules)
        from pipeline import run_dub  # noqa: F401

        self.assertIn("pipeline.run", sys.modules)

    def test_create_app_cold_import_stays_subsecond_in_dev(self) -> None:
        """Baseline for Windows app.log cold/warm compare after packaging.

        On Windows, open APP twice and diff ``[desktop] boot import_app_done`` /
        ``api_ready`` totals in user-data ``app.log``. Warm should be close to
        this dev import cost; a much larger cold gap usually means Defender.
        """
        import os
        import subprocess

        backend = str(Path(__file__).resolve().parents[1] / "backend")
        env = {**os.environ, "PYTHONPATH": backend}
        script = (
            "import time\n"
            "t0 = time.perf_counter()\n"
            "from api.app import create_app\n"
            "create_app()\n"
            "print(time.perf_counter() - t0)\n"
        )
        out = subprocess.check_output([sys.executable, "-c", script], env=env, text=True)
        elapsed = float(out.strip().splitlines()[-1])
        self.assertLess(elapsed, 2.0, f"create_app cold import too slow: {elapsed:.3f}s")


if __name__ == "__main__":
    unittest.main()
