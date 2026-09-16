from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "build_app"))
sys.path.insert(0, str(ROOT / "backend"))

from portable_layout import (
    migrate_windows_state,
    sync_windows_portable_root,
    windows_portable_home,
)


class PortableLayoutTest(unittest.TestCase):
    def test_setup_marker_always_uses_local_app_data(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            installed = root / "Program Files" / "ZM AI TOOL"
            installed.mkdir(parents=True)
            executable = installed / "ZM AI TOOL.exe"
            executable.touch()
            (installed / ".zmaio-installed").write_text("setup\n", encoding="utf-8")
            local = root / "LocalAppData"

            home, source = windows_portable_home(
                executable, {"LOCALAPPDATA": str(local)}
            )

            self.assertEqual(home, local / "ZM_AI_TOOL")
            self.assertEqual(source, installed.absolute())

    def test_marker_free_build_remains_portable_when_writable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            portable = Path(raw) / "ZM_AI_TOOL_v7.0.4"
            portable.mkdir()
            executable = portable / "ZM AI TOOL.exe"
            executable.touch()

            home, source = windows_portable_home(
                executable, {"LOCALAPPDATA": str(Path(raw) / "LocalAppData")}
            )

            self.assertEqual(home, portable.absolute())
            self.assertIsNone(source)

    def test_inno_setup_marks_install_and_launches_as_original_user(self) -> None:
        script = (ROOT / "build_app" / "installer.iss").read_text(encoding="utf-8")

        self.assertIn('DestName: ".zmaio-installed"', script)
        self.assertIn("runasoriginaluser", script)

    def test_output_root_falls_back_when_saved_path_is_not_writable(self) -> None:
        from pipeline.core import output_paths, ui_preferences

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            data = root / "data"
            fallback = root / "fallback-output"
            data.mkdir()
            (data / "ui_preferences.json").write_text(
                json.dumps({"outputRoot": str(root / "blocked")}), encoding="utf-8"
            )
            with patch.object(ui_preferences, "_PREFERENCES_PATH", data / "ui_preferences.json"), patch.dict(
                os.environ, {"ZM_AI_TOOL_OUTPUT_ROOT": str(fallback)}
            ), patch.object(
                output_paths,
                "ensure_writable_output_root",
                side_effect=[OSError("denied"), fallback],
            ):
                self.assertEqual(output_paths.app_output_root(), fallback)

    def test_broken_runtime_is_rebuilt_as_relocatable(self) -> None:
        from pipeline.core.system_check import install

        with tempfile.TemporaryDirectory() as raw:
            venv = Path(raw) / ".venv-runtime"
            python = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
            python.parent.mkdir(parents=True)
            python.touch()
            commands: list[list[str]] = []

            def fake_stream(command: list[str], **_kwargs) -> CompletedProcess:
                commands.append(command)
                if command[1:3] == ["python", "install"]:
                    return CompletedProcess(command, 0, "", "")
                python.parent.mkdir(parents=True)
                python.touch()
                return CompletedProcess(command, 0, "", "")

            probes = [
                CompletedProcess([str(python)], 1, "", "broken"),
                CompletedProcess(["uv", "venv", "--help"], 0, "--relocatable", ""),
            ]
            with patch.object(install.subprocess, "run", side_effect=probes), patch.object(
                install, "_pip_stream", side_effect=fake_stream
            ):
                result = install._ensure_frozen_runtime_venv("uv", venv)

            self.assertEqual(result, python)
            self.assertTrue(python.is_file())
            self.assertIn("--relocatable", commands[-1])
            self.assertFalse(any(venv.parent.glob(".venv-runtime.repair-*")))

    def test_updater_swaps_only_immutable_payload_and_has_rollback(self) -> None:
        from api.routes.system import _windows_update_script

        with tempfile.TemporaryDirectory() as raw:
            script = _windows_update_script(Path(raw)).read_text(encoding="utf-8")
        launcher = (ROOT / "build_app" / "launcher.py").read_text(encoding="utf-8")

        self.assertIn("$payloadNames = @($exeName, 'app', '_internal')", script)
        self.assertIn("Restore-Backup", script)
        self.assertIn("$newProcess.HasExited", script)
        self.assertIn("ZM_AI_TOOL_UPDATE_READY_FILE", script)
        self.assertIn("$env:ZM_AI_TOOL_SUPERVISOR_CHILD = $null", script)
        self.assertNotIn("Move-Item -LiteralPath $Target -Destination", script)
        self.assertNotIn("Remove-Item -LiteralPath $OldTarget", script)
        for variable in ("ZM_AI_TOOL_HOME", "ZM_AI_TOOL_BUNDLE", "ZM_AI_TOOL_VERSION"):
            self.assertIn(f'set_desktop_path("{variable}"', launcher)

    def test_versioned_update_moves_state_and_rebases_default_output(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            old = root / "ZM_AI_TOOL_v7.0.1-windows-x64"
            current = root / "ZM_AI_TOOL_v7.0.2-windows-x64"
            for folder in (old, current):
                folder.mkdir()
                (folder / "ZM AI TOOL.exe").touch()
            (root / "update-params.json").write_text(
                json.dumps({"Target": str(current), "OldTarget": str(old)}),
                encoding="utf-8",
            )

            (old / "data").mkdir()
            (old / "data" / "ui_preferences.json").write_text(
                json.dumps({"locale": "vi", "outputRoot": str(old / "output" / "flow")}),
                encoding="utf-8",
            )
            (old / "output").mkdir()
            (old / "output" / "result.mp4").touch()
            (old / ".venv-runtime").mkdir()
            (old / ".venv-runtime" / "pyvenv.cfg").touch()
            (old / "resources").mkdir()
            (old / "resources" / "old.wav").touch()
            (current / "resources").mkdir()
            (current / "resources" / "user.wav").touch()

            used, migrated, errors = migrate_windows_state(
                current,
                current / "ZM AI TOOL.exe",
                {"LOCALAPPDATA": str(root / "unused-local")},
            )
            previous = sync_windows_portable_root(current, used)

            self.assertFalse(errors)
            self.assertTrue(migrated)
            self.assertEqual(used, [old.absolute()])
            self.assertEqual(previous, [old.absolute()])
            self.assertTrue((current / "data" / "ui_preferences.json").is_file())
            self.assertTrue((current / "output" / "result.mp4").is_file())
            self.assertTrue((current / ".venv-runtime" / "pyvenv.cfg").is_file())
            self.assertTrue((current / "resources" / "user.wav").is_file())
            self.assertTrue((old / "resources" / "old.wav").is_file())
            preferences = json.loads(
                (current / "data" / "ui_preferences.json").read_text(encoding="utf-8")
            )
            self.assertEqual(preferences["outputRoot"], str(current / "output" / "flow"))
            marker = json.loads(
                (current / "data" / ".portable-root").read_text(encoding="utf-8")
            )
            self.assertEqual(marker["current"], str(current.absolute()))
            self.assertEqual(marker["previous"], [str(old.absolute())])

    def test_read_only_portable_state_is_copied_to_fallback_home(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            portable = root / "read-only-portable"
            fallback = root / "local" / "ZM_AI_TOOL" / "portable-data"
            portable.mkdir()
            fallback.mkdir(parents=True)
            executable = portable / "ZM AI TOOL.exe"
            executable.touch()
            (portable / "data").mkdir()
            (portable / "data" / "settings.json").touch()

            used, _, errors = migrate_windows_state(
                fallback, executable, {"LOCALAPPDATA": str(root / "local")}
            )

            self.assertFalse(errors)
            self.assertEqual(used, [portable.absolute()])
            self.assertTrue((fallback / "data" / "settings.json").is_file())
            self.assertTrue((portable / "data" / "settings.json").is_file())

    def test_manually_extracted_sibling_does_not_steal_old_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            old = root / "ZM_AI_TOOL_v7.0.1-windows-x64"
            current = root / "ZM_AI_TOOL_v7.0.2-windows-x64"
            for folder in (old, current):
                folder.mkdir()
                (folder / "ZM AI TOOL.exe").touch()
            (old / "data").mkdir()
            (old / "data" / "settings.json").touch()

            _, _, errors = migrate_windows_state(
                current,
                current / "ZM AI TOOL.exe",
                {"LOCALAPPDATA": str(root / "unused-local")},
            )

            self.assertFalse(errors)
            self.assertFalse((current / "data" / "settings.json").is_file())
            self.assertTrue((old / "data" / "settings.json").is_file())

    def test_moving_fixed_folder_rebases_saved_internal_output(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            old = root / "old" / "ZM_AI_TOOL"
            old_data = old / "data"
            old_data.mkdir(parents=True)
            (old_data / "ui_preferences.json").write_text(
                json.dumps({"outputRoot": str(old / "output" / "tts")}),
                encoding="utf-8",
            )
            sync_windows_portable_root(old)
            current = root / "new" / "ZM_AI_TOOL"
            current.parent.mkdir()
            old.rename(current)

            previous = sync_windows_portable_root(current)
            preferences = json.loads(
                (current / "data" / "ui_preferences.json").read_text(encoding="utf-8")
            )

            self.assertEqual(previous, [old.absolute()])
            self.assertEqual(preferences["outputRoot"], str(current / "output" / "tts"))


if __name__ == "__main__":
    unittest.main()
