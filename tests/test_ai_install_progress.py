from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from pipeline.core.system_check import install


class AiInstallProgressTest(unittest.TestCase):
    def setUp(self) -> None:
        self.old_log = install._install_log_fn
        self.old_progress = install._install_progress_fn

    def tearDown(self) -> None:
        install._install_log_fn = self.old_log
        install._install_progress_fn = self.old_progress

    def test_stream_reports_activity_and_finishes_progress_range(self) -> None:
        logs: list[str] = []
        updates: list[tuple[int, str]] = []
        install._install_log_fn = logs.append
        install._install_progress_fn = lambda value, message: updates.append(
            (int(value), str(message))
        )
        script = (
            "import sys,time; "
            "sys.stdout.write('Downloading torch\\r'); sys.stdout.flush(); "
            "time.sleep(0.05); print('Downloaded torch'); "
            "print('Installed 2 packages')"
        )

        with patch.object(install, "_runtime_subprocess_env", return_value=os.environ.copy()):
            result = install._pip_stream(
                [sys.executable, "-u", "-c", script],
                timeout=5,
                idle_timeout=2,
                progress=(10, 40, "PyTorch"),
            )

        self.assertEqual(result.returncode, 0)
        self.assertIn("Downloading torch", "".join(logs))
        self.assertEqual(updates[-1][0], 40)
        self.assertTrue(any(10 < value < 40 for value, _ in updates))

    def test_silent_process_is_stopped_at_idle_timeout(self) -> None:
        started = time.monotonic()
        with patch.object(install, "_runtime_subprocess_env", return_value=os.environ.copy()):
            with self.assertRaisesRegex(RuntimeError, "INSTALL_IDLE_TIMEOUT"):
                install._pip_stream(
                    [sys.executable, "-c", "import time; time.sleep(5)"],
                    timeout=5,
                    idle_timeout=0.25,
                )
        self.assertLess(time.monotonic() - started, 3)

    def test_chatty_spinner_cannot_bypass_hard_timeout(self) -> None:
        script = (
            "import sys,time\n"
            "while True:\n"
            " sys.stdout.write('spinner\\r')\n"
            " sys.stdout.flush()\n"
            " time.sleep(.01)\n"
        )
        started = time.monotonic()
        with patch.object(install, "_runtime_subprocess_env", return_value=os.environ.copy()):
            with self.assertRaisesRegex(RuntimeError, "INSTALL_TIMEOUT"):
                install._pip_stream(
                    [sys.executable, "-u", "-c", script],
                    timeout=0.25,
                    idle_timeout=None,
                )
        self.assertLess(time.monotonic() - started, 3)

    def test_stream_maps_real_download_bytes_into_stage_progress(self) -> None:
        updates: list[tuple[int, str]] = []
        install._install_progress_fn = lambda value, message: updates.append(
            (int(value), str(message))
        )
        script = (
            "import sys,time; "
            "sys.stdout.write('50/100 MiB\\r'); sys.stdout.flush(); "
            "time.sleep(0.15)"
        )

        with patch.object(install, "_runtime_subprocess_env", return_value=os.environ.copy()):
            result = install._pip_stream(
                [sys.executable, "-u", "-c", script],
                timeout=5,
                idle_timeout=2,
                progress=(10, 40, "PyTorch"),
            )

        self.assertEqual(result.returncode, 0)
        self.assertTrue(any("50%" in message for _, message in updates))
        self.assertEqual(updates[-1][0], 40)

    def test_backend_progress_is_monotonic_and_log_lines_do_not_merge(self) -> None:
        from api.routes import system

        with system._install_lock:
            previous = dict(system._install_state)
            system._install_state.update(
                running=True,
                progress=20,
                message="",
                log="",
                updatedAt=0.0,
            )
        try:
            system._append_install_log("first\n")
            system._append_install_log("second\n")
            system._set_install_progress(35, "Downloading")
            system._set_install_progress(30, "Older update")
            with system._install_lock:
                self.assertEqual(system._install_state["log"], "first\nsecond")
                self.assertEqual(system._install_state["progress"], 35)
                self.assertEqual(system._install_state["message"], "Downloading")
        finally:
            with system._install_lock:
                system._install_state.clear()
                system._install_state.update(previous)

    def test_sherpa_cuda_replacement_does_not_reinstall_cpu_onnxruntime(self) -> None:
        commands: list[list[str]] = []

        def fake_stream(command: list[str], **_kwargs) -> CompletedProcess:
            commands.append(command)
            return CompletedProcess(command, 0, "", "")

        with patch.object(install, "_runtime_ort_accel", return_value="cuda"), patch.object(
            install, "_sherpa_cuda_ready", return_value=False
        ), patch.object(install, "_pip_stream", side_effect=fake_stream):
            install._install_sherpa_cuda("python.exe", "uv.exe")

        self.assertEqual(len(commands), 1)
        self.assertIn("--no-deps", commands[0])

    def test_frozen_ocr_install_streams_and_verifies_provider(self) -> None:
        commands: list[list[str]] = []

        def fake_stream(command: list[str], **_kwargs) -> CompletedProcess:
            commands.append(command)
            return CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            python = root / ".venv-runtime" / (
                "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
            )
            python.parent.mkdir(parents=True)
            python.touch()
            with patch.object(install.sys, "frozen", True, create=True), patch.dict(
                os.environ, {"ZM_AI_TOOL_HOME": str(root)}
            ), patch.object(install, "_ocr_cuda_check", return_value=(False, "CPU")), patch.object(
                install, "_find_uv", return_value="uv.exe"
            ), patch.object(
                install, "_ensure_frozen_runtime_venv", return_value=python
            ), patch.object(
                install, "_pip_stream", side_effect=fake_stream
            ), patch.object(
                install, "_ocr_cuda_check_fresh", return_value=(True, "CUDAExecutionProvider")
            ), patch.object(install, "_invalidate_checks_cache"):
                result = install.install_ocr_cuda()

        self.assertTrue(result["ok"])
        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[0][1:3], ["pip", "uninstall"])
        self.assertIn("--force-reinstall", commands[1])

    def test_frozen_find_uv_uses_bundled_binary_when_path_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = Path(raw)
            uv = bundle / ("uv.exe" if sys.platform == "win32" else "uv")
            uv.touch()
            with patch.dict(os.environ, {"ZM_AI_TOOL_BUNDLE": str(bundle), "PATH": ""}), patch.object(
                install.sys, "frozen", True, create=True
            ):
                self.assertEqual(install._find_uv(), str(uv))

    def test_frozen_torch_check_uses_runtime_probe_not_parent_import(self) -> None:
        with patch.object(install.sys, "frozen", True, create=True), patch.object(
            install, "_runtime_mod_ok", return_value=(True, "ok")
        ) as runtime_mod_ok, patch.object(
            install, "_nvidia_present", return_value=False
        ), patch.object(
            install, "_mod_ok", return_value=(False, "parent missing")
        ):
            self.assertFalse(install._runtime_torch_needs_install())

        self.assertEqual(
            [call.args[0] for call in runtime_mod_ok.call_args_list],
            ["torch", "torchaudio"],
        )


if __name__ == "__main__":
    unittest.main()
