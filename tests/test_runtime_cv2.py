"""Run: PYTHONPATH=backend backend/.venv/bin/python -m unittest discover -s tests -p test_runtime_cv2.py"""
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest
from unittest.mock import patch


class OpenCVRuntimeTest(unittest.TestCase):
    def check_process(self, code):
        result = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(code)],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "backend")},
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_runtime_refresh_preserves_native_submodules(self):
        self.check_process('''
            import importlib
            import tempfile
            from pathlib import Path
            from pipeline.core import runtime_site

            cv2 = runtime_site.ensure_cv2()
            native_dnn = cv2.dnn
            native_version = cv2.__version__
            with tempfile.TemporaryDirectory() as directory:
                for _ in range(3):
                    runtime_site._purge_external_modules(Path(directory))
                    assert importlib.import_module("cv2.dnn") is native_dnn
                    assert importlib.reload(cv2.typing).LayerId is native_dnn.DictValue
                    assert runtime_site.ensure_cv2() is cv2
            import numpy as np
            image = np.zeros((8, 8, 3), np.uint8)
            ok, encoded = cv2.imencode(".png", image)
            assert ok and cv2.imdecode(encoded, cv2.IMREAD_COLOR).shape == image.shape
            assert cv2.__version__ == native_version
        ''')

    def test_concurrent_jobs_share_one_import(self):
        self.check_process('''
            from concurrent.futures import ThreadPoolExecutor
            from pathlib import Path
            import sys
            import tempfile
            import threading
            import textwrap
            from pipeline.core import runtime_site

            with tempfile.TemporaryDirectory() as directory:
                site = Path(directory)
                package = site / "cv2"
                package.mkdir()
                package.joinpath("__init__.py").write_text(textwrap.dedent("""
                    import sys, time
                    if hasattr(sys, 'OpenCV_LOADER'):
                        raise ImportError('recursion is detected during loading of cv2 binary extensions')
                    sys.OpenCV_LOADER = True
                    sys._cv2_test_started.set()
                    time.sleep(0.1)
                    assert sys.OpenCV_LOADER
                    VideoCapture = object()
                    del sys.OpenCV_LOADER
                """))
                sys.frozen = True
                runtime_site.runtime_site_packages = lambda: site
                sys._cv2_test_started = threading.Event()
                with ThreadPoolExecutor(max_workers=8) as pool:
                    futures = [pool.submit(runtime_site.ensure_cv2)]
                    assert sys._cv2_test_started.wait(timeout=5)
                    futures += [pool.submit(runtime_site.ensure_cv2) for _ in range(7)]
                    modules = [future.result(timeout=5) for future in futures]
                assert all(module is modules[0] for module in modules)
                assert not hasattr(sys, "OpenCV_LOADER")
                assert str(package) not in sys.path
        ''')

    def test_cached_cv2_and_ollama_requests_run_concurrently(self):
        self.check_process('''
            from concurrent.futures import ThreadPoolExecutor
            import json
            import sys
            from unittest.mock import patch
            import httpx
            import numpy as np
            from pipeline.core.runtime_site import ensure_cv2
            from pipeline.review import llm

            cv2 = ensure_cv2()
            cached = {name: sys.modules[name] for name in ('cv2', 'cv2.dnn', 'numpy')}
            real_client = httpx.Client

            def handle(request):
                body = json.loads(request.content)
                assert body['keep_alive'] == '60m'
                event = {'response': json.dumps({'index': int(body['prompt'])}), 'done': True}
                return httpx.Response(200, content=json.dumps(event) + '\\n')

            def client(**kwargs):
                return real_client(transport=httpx.MockTransport(handle), **kwargs)

            def job(index):
                assert ensure_cv2() is cv2
                image = np.full((32, 48, 3), index, np.uint8)
                gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
                ok, encoded = cv2.imencode('.png', gray)
                assert ok
                np.testing.assert_array_equal(cv2.imdecode(encoded, cv2.IMREAD_GRAYSCALE), gray)
                assert llm.generate_json(str(index), model='cached-test') == {'index': index}
                return index

            with patch.object(llm.httpx, 'Client', side_effect=client):
                with ThreadPoolExecutor(max_workers=8) as pool:
                    assert list(pool.map(job, range(32))) == list(range(32))
            assert all(sys.modules[name] is module for name, module in cached.items())
        ''')

    def test_failed_import_can_retry_without_destroying_native_modules(self):
        self.check_process('''
            from pathlib import Path
            import sys
            import tempfile
            import textwrap
            from pipeline.core import runtime_site

            with tempfile.TemporaryDirectory() as directory:
                site = Path(directory)
                package = site / "cv2"
                package.mkdir()
                package.joinpath("__init__.py").write_text(textwrap.dedent("""
                    import sys, types
                    if hasattr(sys, 'OpenCV_LOADER'):
                        raise ImportError('recursion is detected during loading of cv2 binary extensions')
                    sys.OpenCV_LOADER = True
                    if 'cv2.dnn' not in sys.modules:
                        sys.modules['cv2.dnn'] = types.ModuleType('cv2.dnn')
                        raise AttributeError("module 'cv2.dnn' has no attribute 'DictValue'")
                    VideoCapture = object()
                    dnn = sys.modules['cv2.dnn']
                    del sys.OpenCV_LOADER
                """))
                sys.frozen = True
                runtime_site.runtime_site_packages = lambda: site
                try:
                    runtime_site.ensure_cv2()
                except AttributeError:
                    pass
                else:
                    raise AssertionError("Import failure was hidden")
                assert not hasattr(sys, "OpenCV_LOADER")
                native_dnn = sys.modules["cv2.dnn"]
                assert runtime_site.ensure_cv2().dnn is native_dnn
        ''')


class OpenCVInstallTest(unittest.TestCase):
    def test_full_runtime_resolver_excludes_ocr_sdk_and_its_opencv_dependency(self):
        from pipeline.core.system_check import install

        self.assertEqual(
            install._frozen_runtime_package_specs(["rapidocr_onnxruntime", "cv2", "PIL", "av"]),
            ["av"],
        )

    def test_working_ocr_does_not_reinstall_dependencies_or_refresh_model_modules(self):
        from pipeline.core.system_check import install
        from pipeline.core import runtime_site

        for frozen in (False, True):
            with self.subTest(frozen=frozen), \
                 patch.object(sys, "frozen", frozen, create=True), \
                 patch.object(install, "_mod_ok", return_value=(True, "ready")), \
                 patch.object(install, "_runtime_mod_ok", return_value=(True, "ready")), \
                 patch.object(install, "_runtime_modules_batch_ok", side_effect=lambda names: {name: (True, "ready") for name in names}), \
                 patch.object(install, "_runtime_ort_accel", return_value="cpu"), \
                 patch.object(install, "_runtime_pip_install") as pip_install, \
                 patch.object(runtime_site, "install_runtime_meta_path") as refresh, \
                 patch.object(runtime_site, "bootstrap_ai_runtime") as preload:
                self.assertTrue(install._install_native_optional_feature("ocr")["ok"])
                pip_install.assert_not_called()
                refresh.assert_not_called()
                preload.assert_not_called()

    def test_missing_ocr_sdk_does_not_pull_another_opencv_or_replace_cached_dependencies(self):
        from pipeline.core.system_check import install
        from pipeline.core import runtime_site

        installed = False

        def probe(name):
            return (installed or name != "rapidocr_onnxruntime", "probe")

        def install_sdk(*packages, **kwargs):
            nonlocal installed
            self.assertEqual(packages, ("--no-deps", "rapidocr-onnxruntime==1.4.4"))
            installed = True

        with patch.object(install, "_mod_ok", side_effect=probe), \
             patch.object(install, "_runtime_ort_accel", return_value="cpu"), \
             patch.object(install, "_runtime_pip_install", side_effect=install_sdk) as pip_install, \
             patch.object(runtime_site, "bootstrap_ai_runtime") as preload:
            self.assertTrue(install._install_native_optional_feature("ocr")["ok"])
            pip_install.assert_called_once()
            preload.assert_not_called()

    def test_conflicting_wheels_are_removed_and_shared_cv2_is_reinstalled(self):
        import tempfile
        from pipeline.core.system_check import install

        for conflict in (None, "opencv-python", "opencv-contrib-python", "opencv-contrib-python-headless"):
            with self.subTest(conflict=conflict), tempfile.TemporaryDirectory() as directory:
                for name in ("opencv-python-headless", conflict):
                    if name is None:
                        continue
                    metadata = Path(directory) / f"{name.replace('-', '_')}-4.12.0.88.dist-info"
                    metadata.mkdir()
                    (metadata / "METADATA").write_text(f"Name: {name}\nVersion: 4.12.0.88\n")
                with patch("sysconfig.get_path", return_value=directory), \
                     patch.object(install, "_runtime_pip_uninstall_cmd", return_value=["uninstall"]) as uninstall, \
                     patch.object(install, "_pip_stream", return_value=subprocess.CompletedProcess([], 0, "", "")) as remove, \
                     patch.object(install, "_runtime_pip_install") as reinstall, \
                     patch.object(install, "_invalidate_checks_cache"):
                    install._repair_opencv_conflicts()
                    if conflict is None:
                        uninstall.assert_not_called()
                        reinstall.assert_not_called()
                    else:
                        uninstall.assert_called_once_with(conflict, "opencv-python-headless")
                        remove.assert_called_once()
                        reinstall.assert_called_once_with(
                            "--force-reinstall", "--no-deps", "opencv-python-headless==4.12.0.88",
                        )

    def test_cached_modules_are_not_uninstalled_on_any_platform(self):
        from email.message import Message
        from types import SimpleNamespace
        from pipeline.core.system_check import install

        metadata = Message()
        metadata["Name"] = "opencv-python"
        for platform in ("win32", "darwin", "linux"):
            for cached in ("cv2", "numpy", "onnxruntime", "torch", "transformers"):
                with self.subTest(platform=platform, cached=cached), \
                     patch("importlib.metadata.distributions", return_value=[SimpleNamespace(metadata=metadata)]), \
                     patch("sysconfig.get_path", return_value="unused"), \
                     patch.object(sys, "platform", platform), \
                     patch.dict(sys.modules, {cached: object()}), \
                     patch.object(install, "_pip_stream") as remove:
                    with self.assertRaisesRegex(RuntimeError, "AI_RUNTIME_BUSY"):
                        install._repair_opencv_conflicts()
                    remove.assert_not_called()


if __name__ == "__main__":
    unittest.main()
