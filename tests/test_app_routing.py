import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient


SPA_PATHS = (
    '/text-to-speech', '/video-clone', '/live-preview', '/renders', '/download-video',
    '/video-cleaner', '/media-compose', '/subtitle-export', '/drawing', '/film',
    '/batch', '/flow-veo', '/chat', '/automation', '/license',
)


class DesktopRoutingTest(unittest.TestCase):
    def test_desktop_boot_phases_are_logged(self):
        launcher = Path(__file__).resolve().parents[1] / 'build_app' / 'launcher.py'
        source = launcher.read_text(encoding='utf-8')
        for phase in (
            'parent_spawn',
            'child_enter',
            'runtime_site',
            'import_app_begin',
            'import_app_done',
            'api_ready',
            'webview_start',
        ):
            self.assertIn(f'_boot_mark("{phase}"', source)
        self.assertIn('[desktop] boot ', source)

    def test_startup_opens_root_not_forced_tts(self):
        launcher = Path(__file__).resolve().parents[1] / 'build_app' / 'launcher.py'
        source = launcher.read_text(encoding='utf-8')
        self.assertIn('f"{base}/?v={APP_VERSION}"', source)
        self.assertNotIn('f"{base}/text-to-speech?v={APP_VERSION}"', source)
        self.assertNotIn('f"{base}/video-clone?v={APP_VERSION}"', source)
        for path in SPA_PATHS:
            self.assertIn(f'"{path}"', source)

    def test_spa_paths_serve_the_frontend(self):
        with tempfile.TemporaryDirectory() as raw:
            web_dir = Path(raw)
            (web_dir / 'index.html').write_text('<html>ZM AI TOOL</html>', encoding='utf-8')
            app = FastAPI()

            def workspace_page():
                return FileResponse(web_dir / 'index.html')

            for path in SPA_PATHS:
                app.add_api_route(path, workspace_page, methods=['GET'])
                app.add_api_route(path + '/', workspace_page, methods=['GET'])
            app.mount('/', StaticFiles(directory=web_dir, html=True), name='web')
            with TestClient(app) as client:
                for path in [
                    '/', '/text-to-speech?v=8.6.24', '/text-to-speech/', '/video-clone?v=8.6.24',
                    '/video-clone/', '/flow-veo', '/media-compose?v=1',
                ]:
                    response = client.get(path)
                    self.assertEqual(response.status_code, 200, path)
                    self.assertEqual(response.text, '<html>ZM AI TOOL</html>', path)
                    self.assertIn('text/html', response.headers['content-type'], path)


if __name__ == '__main__':
    unittest.main()
