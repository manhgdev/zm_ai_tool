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
    def test_updater_early_exit_is_reported_before_app_shutdown(self):
        from api.routes.system import _spawn_windows_updater
        from unittest.mock import Mock
        process = Mock()
        process.poll.return_value = 1
        with tempfile.TemporaryDirectory() as raw, patch('api.routes.system.subprocess.Popen', return_value=process):
            with self.assertRaisesRegex(RuntimeError, 'App kept open'):
                _spawn_windows_updater(['powershell.exe'], started=Path(raw) / 'started', log_path=Path(raw) / 'log')

    def test_updater_acknowledges_before_waiting_for_app(self):
        from api.routes.system import _windows_setup_update_script, _windows_update_script, _spawn_windows_updater
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for factory in (_windows_setup_update_script, _windows_update_script):
                source = factory(root).read_text(encoding='utf-8-sig')
                self.assertLess(source.index('Set-Content -LiteralPath $StartedFile'), source.index('Wait-Process'))
                self.assertIn("$env:PYINSTALLER_RESET_ENVIRONMENT = '1'", source)
            process = Mock()
            process.poll.return_value = None
            started = root / 'started'
            def spawn(*args, **kwargs):
                started.write_text('ready')
                self.assertEqual(kwargs['env']['PYINSTALLER_RESET_ENVIRONMENT'], '1')
                return process
            with patch('api.routes.system.subprocess.Popen', side_effect=spawn):
                _spawn_windows_updater(['powershell.exe'], started=started, log_path=root / 'log')
            process.terminate.assert_not_called()
            self.assertEqual(Path(str(started) + '.commit').read_text(), 'proceed')

    def test_setup_data_follows_selected_install_directory(self) -> None:
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

            self.assertEqual(home, installed.absolute() / 'user-data')
            self.assertFalse(local.exists())
            self.assertEqual(source, installed.absolute())

    def test_setup_does_not_fallback_to_c_when_selected_folder_is_unwritable(self):
        with tempfile.TemporaryDirectory() as raw:
            installed = Path(raw) / 'Selected Drive' / 'ZM AI TOOL'
            installed.mkdir(parents=True)
            (installed / '.zmaio-installed').touch()
            with patch('portable_layout.ensure_writable_directory', side_effect=PermissionError('denied')) as ensure:
                with self.assertRaises(PermissionError):
                    windows_portable_home(installed / 'ZM AI TOOL.exe', {'LOCALAPPDATA': str(Path(raw) / 'C')})
                ensure.assert_called_once_with(installed.absolute() / 'user-data')

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
        self.assertIn(r"DefaultDirName={localappdata}\Programs\{#MyAppName}", script)
        self.assertIn("UsePreviousAppDir=no", script)
        self.assertIn("PrivilegesRequired=lowest", script)
        self.assertNotIn("PrivilegesRequiredOverridesAllowed=dialog", script)
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

    def test_output_root_hard_fallback_is_documents_on_windows(self) -> None:
        from pipeline.core import output_paths

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            docs = root / "Documents" / "ZM_AI_TOOL"
            with patch(
                "pipeline.core.ui_preferences.load_output_root", return_value=None
            ), patch.dict(os.environ, {}, clear=False), patch.object(
                output_paths.sys, "platform", "win32"
            ), patch.object(output_paths.Path, "home", return_value=root), patch.object(
                output_paths,
                "ensure_writable_output_root",
                side_effect=lambda folder: folder,
            ):
                os.environ.pop("ZM_AI_TOOL_OUTPUT_ROOT", None)
                self.assertEqual(output_paths.app_output_root(), docs)

    def test_output_root_hard_fallback_is_downloads_off_windows(self) -> None:
        from pipeline.core import output_paths

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            downloads = root / "Downloads" / "ZM_AI_TOOL"
            with patch(
                "pipeline.core.ui_preferences.load_output_root", return_value=None
            ), patch.object(output_paths.sys, "platform", "darwin"), patch.object(
                output_paths.Path, "home", return_value=root
            ), patch.object(
                output_paths,
                "ensure_writable_output_root",
                side_effect=lambda folder: folder,
            ):
                os.environ.pop("ZM_AI_TOOL_OUTPUT_ROOT", None)
                self.assertEqual(output_paths.app_output_root(), downloads)

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

    def test_installed_windows_updater_relaunches_without_elevation(self) -> None:
        from api.routes.system import _windows_setup_update_script

        with tempfile.TemporaryDirectory() as raw:
            script = _windows_setup_update_script(Path(raw)).read_text(encoding="utf-8-sig")

        self.assertIn("PrivilegesRequired=lowest", script)
        self.assertIn("Start-Process -FilePath $newExe", script)
        self.assertIn("/DIR=\"", script)
        self.assertIn("ZM_AI_TOOL_UPDATE_READY_FILE", script)
        self.assertIn("Ban moi khong bao san sang sau 90 giay", script)
        self.assertNotIn("-Verb RunAs", script)

    def test_macos_updater_uses_pkg_or_zip_relaunch_and_rollback(self) -> None:
        from api.routes.system import _macos_update_script, _macos_update_script_body

        body = _macos_update_script_body()
        with tempfile.TemporaryDirectory() as raw:
            script = _macos_update_script(Path(raw))
            self.assertEqual(script.name, "apply-macos-update")
            self.assertFalse((Path(raw) / "apply-macos-update.sh").exists())
            self.assertEqual(script.read_text(encoding="utf-8"), body)

        self.assertIn("pkgutil --expand-full", body)
        self.assertIn("ditto -x -k", body)
        self.assertIn("launch_and_wait", body)
        self.assertIn("restore_backup", body)
        self.assertIn("remove_duplicate_apps", body)
        self.assertIn("sudo -n /usr/sbin/installer -pkg", body)
        self.assertIn('$HOME/Applications/ZM AI TOOL.app', body)
        self.assertIn("Da go ban trung (doi ten)", body)
        self.assertIn(".$name.removed.", body)
        self.assertIn("with administrator privileges", body)
        self.assertNotIn("display notification", body)
        self.assertIn("Bo qua mo duong dan khong phai .app", body)

    def test_macos_removes_older_duplicate_app_bundle(self) -> None:
        import plistlib

        from portable_layout import remove_stale_macos_app_duplicates

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            keep_root = root / "keep-apps"
            stale_root = root / "stale-apps"
            keep = keep_root / "ZM AI TOOL.app"
            stale = stale_root / "ZM AI TOOL.app"
            newer = root / "newer-apps" / "ZM AI TOOL.app"
            for app, version in (
                (keep, "8.5.9"),
                (stale, "8.5.8"),
                (newer, "8.5.10"),
            ):
                info = app / "Contents" / "Info.plist"
                info.parent.mkdir(parents=True)
                info.write_bytes(plistlib.dumps({"CFBundleShortVersionString": version}))

            removed = remove_stale_macos_app_duplicates(
                keep, roots=[keep_root, stale_root, newer.parent]
            )
            self.assertEqual(removed, [str(stale.resolve())])
            self.assertFalse(stale.exists())
            self.assertTrue(keep.is_dir())
            self.assertTrue(newer.is_dir())

    def test_release_assets_match_unelevated_update_packages(self) -> None:
        from api.routes import system

        release = {
            "tag_name": "v8.0.6",
            "assets": [
                {"name": "ZM_AI_TOOL_v8.0.6-windows-x64-Setup.exe"},
                {"name": "ZM_AI_TOOL_v8.0.6-windows-x64-Portable.zip"},
                {"name": "ZM_AI_TOOL_v8.0.6-macos-arm64.pkg"},
                {"name": "ZM_AI_TOOL_v8.0.6-macos-arm64.zip"},
            ],
        }
        with patch.object(system.sys, "platform", "win32"), patch.object(
            system, "_is_windows_installed_build", return_value=True
        ):
            self.assertEqual(
                system._release_asset(release)["name"],
                "ZM_AI_TOOL_v8.0.6-windows-x64-Setup.exe",
            )
        with patch.object(system.sys, "platform", "darwin"), patch(
            "platform.machine", return_value="arm64"
        ):
            self.assertEqual(
                system._release_asset(release)["name"],
                "ZM_AI_TOOL_v8.0.6-macos-arm64.pkg",
            )
        with patch.object(system.sys, "platform", "darwin"), patch(
            "platform.machine", return_value="x86_64"
        ):
            release_intel = {
                "tag_name": "v8.0.6",
                "assets": [{"name": "ZM_AI_TOOL_v8.0.6-macos-x86_64.pkg"}],
            }
            self.assertEqual(
                system._release_asset(release_intel)["name"],
                "ZM_AI_TOOL_v8.0.6-macos-x86_64.pkg",
            )
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
