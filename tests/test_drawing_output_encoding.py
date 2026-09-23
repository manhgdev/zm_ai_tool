"""Exercise the renderer's output setup without requiring OpenCV/AI packages."""
import ast
import io
from pathlib import Path
import types
import unittest


class DrawingOutputEncodingTests(unittest.TestCase):
    def test_cp1252_streams_are_reconfigured_before_unicode_output(self):
        source = Path(__file__).resolve().parents[1] / 'backend/pipeline/drawing/stream_runner.py'
        tree = ast.parse(source.read_text(encoding='utf-8'))
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                        and n.name == '_configure_text_output')
        buffers = [io.BytesIO(), io.BytesIO()]
        streams = [io.TextIOWrapper(b, encoding='cp1252') for b in buffers]
        namespace = {'sys': types.SimpleNamespace(stdout=streams[0], stderr=streams[1])}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), namespace)
        with self.assertRaises(UnicodeEncodeError):
            streams[0].write('墨流')
        namespace['_configure_text_output']()
        for stream, buffer in zip(streams, buffers):
            stream.write('墨流 · Đang vẽ ảnh\n')
            stream.flush()
            self.assertEqual(buffer.getvalue().decode('utf-8'), '墨流 · Đang vẽ ảnh\n')
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
        self.assertEqual(ast.unparse(main.body[0]), '_configure_text_output()')


if __name__ == '__main__':
    unittest.main()
