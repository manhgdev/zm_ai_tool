from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from pipeline.core import runtime_packs
from pipeline.core.runtime_active import active_runtime_dir
from scripts import build_runtime_packs


def _archive(path: Path, files: dict[str, bytes]) -> dict[str, object]:
    with zipfile.ZipFile(path, "w") as bundle:
        for name, content in files.items():
            bundle.writestr(name, content)
    return {
        "url": path.as_uri(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size": path.stat().st_size,
        "unpackedSize": sum(len(value) for value in files.values()),
    }


class RuntimePackInstallTest(unittest.TestCase):
    def test_runtime_builder_finds_uv_in_backend_build_venv(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bundled = root / "backend" / ".venv" / "Scripts" / "uv.exe"
            bundled.parent.mkdir(parents=True)
            bundled.write_bytes(b"uv")
            with patch.object(build_runtime_packs, "ROOT", root), patch(
                "scripts.build_runtime_packs.shutil.which", return_value=None
            ), patch.dict("os.environ", {"UV_EXECUTABLE": ""}):
                self.assertEqual(build_runtime_packs.find_uv(), str(bundled))

    def test_core_and_gpu_activate_only_after_probe(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            core = _archive(root / "core.zip", {"Scripts/python.exe": b"python", "core.txt": b"core"})
            gpu = _archive(root / "gpu.zip", {"Lib/site-packages/gpu.txt": b"cuda"})
            manifest = root / "runtime-manifest.json"
            manifest.write_text(json.dumps({
                "schemaVersion": 1,
                "runtimeVersion": "test-1",
                "packs": {"core-ai-win-x64": core, "gpu-nvidia-cu124": gpu},
            }), encoding="utf-8")
            updates: list[dict[str, object]] = []
            with patch.dict("os.environ", {
                "ZM_AI_TOOL_HOME": str(root / "home"),
                "ZM_AI_TOOL_RUNTIME_MANIFEST_URL": manifest.as_uri(),
            }), patch.object(runtime_packs, "_is_windows_desktop", return_value=True), patch.object(
                runtime_packs, "_hardware_pack", return_value="gpu-nvidia-cu124"
            ), patch.object(
                runtime_packs, "_probe", return_value='{"gpu":"cuda"}'
            ):
                result = runtime_packs.install_runtime_packs(
                    lambda value, message, fields: updates.append({"value": value, "message": message, **fields})
                )
                activated = active_runtime_dir()

            self.assertTrue(result["ok"])
            self.assertEqual(result["runtimePack"], "gpu-nvidia-cu124")
            self.assertTrue((activated / "core.txt").is_file())
            self.assertTrue((activated / "Lib/site-packages/gpu.txt").is_file())
            self.assertEqual(updates[-1]["stage"], "activate")

    def test_checksum_failure_never_creates_active_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            core = _archive(root / "core.zip", {"Scripts/python.exe": b"python"})
            core["sha256"] = "0" * 64
            manifest = root / "runtime-manifest.json"
            manifest.write_text(json.dumps({
                "schemaVersion": 1,
                "runtimeVersion": "test-1",
                "packs": {"core-ai-win-x64": core},
            }), encoding="utf-8")
            home = root / "home"
            with patch.dict("os.environ", {
                "ZM_AI_TOOL_HOME": str(home),
                "ZM_AI_TOOL_RUNTIME_MANIFEST_URL": manifest.as_uri(),
            }), patch.object(runtime_packs, "_is_windows_desktop", return_value=True), patch.object(
                runtime_packs, "_hardware_pack", return_value="cpu"
            ):
                with self.assertRaisesRegex(runtime_packs.RuntimePackError, "SHA-256") as caught:
                    runtime_packs.install_runtime_packs()

            self.assertEqual(caught.exception.code, "CHECKSUM_MISMATCH")
            self.assertFalse((home / "runtime/current.json").exists())

    def test_zip_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("../escape.txt", "bad")
            with self.assertRaises(runtime_packs.RuntimePackError) as caught:
                runtime_packs._safe_extract(archive, root / "target")
            self.assertEqual(caught.exception.code, "EXTRACT_FAILED")
            self.assertFalse((root / "escape.txt").exists())

    def test_complete_part_is_promoted_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source.zip"
            source.write_bytes(b"complete archive")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            pack = runtime_packs.Pack("core", "https://invalid.example/core.zip", digest, source.stat().st_size, 1)
            cache = root / "cache"
            cache.mkdir()
            final = cache / f"core-{digest[:12]}.zip"
            final.with_suffix(".zip.part").write_bytes(source.read_bytes())
            with patch("urllib.request.urlopen", side_effect=AssertionError("network must not be used")):
                result = runtime_packs._download(pack, cache, None, 0, 1, pack.size)
            self.assertEqual(result, final)
            self.assertEqual(result.read_bytes(), source.read_bytes())

    def test_nvidia_without_working_smi_is_rejected_before_download(self) -> None:
        with patch("pipeline.core.media.detect_device", return_value={"gpuKind": "nvidia", "accel": "directml"}), patch(
            "pipeline.core.accel.nvidia_smi_executable", return_value="nvidia-smi"
        ), patch("subprocess.run", return_value=SimpleNamespace(returncode=1, stdout="", stderr="driver missing")):
            with self.assertRaises(runtime_packs.RuntimePackError) as caught:
                runtime_packs._hardware_pack()
        self.assertEqual(caught.exception.code, "DRIVER_TOO_OLD")

    def test_disk_check_includes_archives_and_extracted_size(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = root / "runtime-manifest.json"
            manifest.write_text(json.dumps({
                "schemaVersion": 1,
                "runtimeVersion": "test-1",
                "packs": {"core-ai-win-x64": {
                    "url": "core.zip", "sha256": "1" * 64,
                    "size": 400, "unpackedSize": 500,
                }},
            }), encoding="utf-8")
            with patch.dict("os.environ", {
                "ZM_AI_TOOL_HOME": str(root / "home"),
                "ZM_AI_TOOL_RUNTIME_MANIFEST_URL": manifest.as_uri(),
            }), patch.object(runtime_packs, "_is_windows_desktop", return_value=True), patch.object(
                runtime_packs, "_hardware_pack", return_value="cpu"
            ), patch("shutil.disk_usage", return_value=SimpleNamespace(free=512 * 1024 * 1024 + 899)):
                with self.assertRaises(runtime_packs.RuntimePackError) as caught:
                    runtime_packs.install_runtime_packs()
            self.assertEqual(caught.exception.code, "DISK_FULL")

    def test_valid_cached_archive_is_not_counted_twice_for_disk_space(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "home"
            cache = home / "runtime" / "downloads"
            cache.mkdir(parents=True)
            content = b"cached"
            digest = hashlib.sha256(content).hexdigest()
            (cache / f"core-ai-win-x64-{digest[:12]}.zip").write_bytes(content)
            manifest = root / "runtime-manifest.json"
            manifest.write_text(json.dumps({
                "schemaVersion": 1,
                "runtimeVersion": "test-1",
                "packs": {"core-ai-win-x64": {
                    "url": "core.zip", "sha256": digest,
                    "size": len(content), "unpackedSize": 100,
                }},
            }), encoding="utf-8")
            disk_free = 512 * 1024 * 1024 + 100
            with patch.dict("os.environ", {
                "ZM_AI_TOOL_HOME": str(home),
                "ZM_AI_TOOL_RUNTIME_MANIFEST_URL": manifest.as_uri(),
            }), patch.object(runtime_packs, "_is_windows_desktop", return_value=True), patch.object(
                runtime_packs, "_hardware_pack", return_value="cpu"
            ), patch("shutil.disk_usage", return_value=SimpleNamespace(free=disk_free)), patch.object(
                runtime_packs, "_download", side_effect=runtime_packs.RuntimePackError("DOWNLOAD_FAILED", "stop", retryable=True)
            ):
                with self.assertRaises(runtime_packs.RuntimePackError) as caught:
                    runtime_packs.install_runtime_packs()
            self.assertEqual(caught.exception.code, "DOWNLOAD_FAILED")

    def test_failed_rollback_probe_keeps_current_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runtime = root / "runtime"
            current_dir = runtime / "current-runtime" / "Scripts"
            previous_dir = runtime / "previous-runtime" / "Scripts"
            current_dir.mkdir(parents=True)
            previous_dir.mkdir(parents=True)
            (current_dir / "python.exe").write_bytes(b"python")
            (previous_dir / "python.exe").write_bytes(b"python")
            current = {"path": "current-runtime", "runtimePack": "cpu"}
            previous = {"path": "previous-runtime", "runtimePack": "cpu"}
            (runtime / "current.json").write_text(json.dumps(current), encoding="utf-8")
            (runtime / "previous.json").write_text(json.dumps(previous), encoding="utf-8")
            with patch.dict("os.environ", {"ZM_AI_TOOL_HOME": str(root)}), patch.object(
                runtime_packs, "_probe", side_effect=runtime_packs.RuntimePackError(
                    "RUNTIME_PROBE_FAILED", "bad runtime", retryable=False
                )
            ):
                with self.assertRaises(runtime_packs.RuntimePackError):
                    runtime_packs.rollback_runtime()
            self.assertEqual(json.loads((runtime / "current.json").read_text()), current)


if __name__ == "__main__":
    unittest.main()
