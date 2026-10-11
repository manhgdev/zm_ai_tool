import ast
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient


class DesktopRoutingTest(unittest.TestCase):
    def test_tts_startup_and_clone_urls_serve_the_frontend(self):
        launcher = Path(__file__).resolve().parents[1] / 'build_app' / 'launcher.py'
        source = launcher.read_text(encoding='utf-8')
        self.assertEqual(source.count('f"{base}/text-to-speech?v={APP_VERSION}"'), 2)
        self.assertNotIn('f"{base}/video-clone?v={APP_VERSION}"', source)
        tree = ast.parse(source)
        route = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'workspace_page')
        mount = next(node for node in tree.body if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute) and node.value.func.attr == 'mount')
        with tempfile.TemporaryDirectory() as raw:
            web_dir = Path(raw)
            (web_dir / 'index.html').write_text('<html>ZM AI TOOL</html>', encoding='utf-8')
            app = FastAPI()
            exec(compile(ast.Module(body=[route, mount], type_ignores=[]), str(launcher), 'exec'), {
                'app': app, 'web_dir': web_dir, 'FileResponse': FileResponse, 'StaticFiles': StaticFiles,
            })
            with TestClient(app) as client:
                for path in ['/', '/text-to-speech?v=8.6.23', '/text-to-speech/', '/video-clone?v=8.6.23', '/video-clone/']:
                    response = client.get(path)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.text, '<html>ZM AI TOOL</html>')
                    self.assertIn('text/html', response.headers['content-type'])
