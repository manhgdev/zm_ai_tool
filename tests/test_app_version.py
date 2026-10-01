import ast
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class AppVersionTest(unittest.TestCase):
    def test_launcher_replaces_inherited_version_on_every_platform(self):
        tree = ast.parse((ROOT / 'build_app/launcher.py').read_text())
        start = next(i for i, node in enumerate(tree.body)
                     if isinstance(node, ast.Assign)
                     and any(isinstance(target, ast.Name) and target.id == 'APP_VERSION'
                             for target in node.targets))
        setter = next(node for node in tree.body
                      if isinstance(node, ast.FunctionDef) and node.name == 'set_desktop_path')
        code = compile(ast.Module(body=[setter, *tree.body[start:start + 2]], type_ignores=[]),
                       '<version-env-test>', 'exec')
        for platform in ('darwin', 'win32'):
            with self.subTest(platform=platform), patch.object(sys, 'platform', platform), \
                    patch.dict(os.environ, {'ZM_AI_TOOL_VERSION': '8.6.2'}):
                namespace = {'os': os, 'sys': sys, 'Path': Path, 'app_version': lambda: '8.6.3'}
                exec(code, namespace)
                self.assertEqual(os.environ['ZM_AI_TOOL_VERSION'], '8.6.3')

    def test_launcher_reads_bundled_package_without_starting_gui(self):
        tree = ast.parse((ROOT / 'build_app/launcher.py').read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'app_version')
        with tempfile.TemporaryDirectory() as raw:
            bundle = Path(raw)
            (bundle / 'package.json').write_text(json.dumps({'version': '9.8.7'}))
            namespace = {'bundle': bundle, 'Path': Path, 'json': json, '__file__': str(bundle / 'launcher.py')}
            exec(compile(ast.Module(body=[function], type_ignores=[]), '<version-test>', 'exec'), namespace)
            self.assertEqual(namespace['app_version'](), '9.8.7')

    def test_build_has_no_second_version_source(self):
        self.assertFalse((ROOT / 'build_app/VERSION').exists())
        for name in ['build_app/build.mjs', 'build_app/build_installer.mjs', 'scripts/release.sh', '.github/workflows/release-windows.yml']:
            source = (ROOT / name).read_text()
            self.assertNotIn('build_app/VERSION', source, name)
            self.assertNotIn('releaseVersionFilePath', source, name)
        self.assertIn('`${packageJsonPath}${dataSep}.`', (ROOT / 'build_app/build.mjs').read_text())


if __name__ == '__main__':
    unittest.main()
