"""Run: PYTHONPATH=backend backend/.venv/bin/python -m unittest discover -s tests -p test_runtime_cv2.py"""
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


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


if __name__ == "__main__":
    unittest.main()
