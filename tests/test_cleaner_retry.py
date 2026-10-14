import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from api.routes import cleaner


class CleanerRetryTests(unittest.TestCase):
    def test_rerun_copies_original_and_uses_current_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / 'original.png'
            original.write_bytes(b'original image')
            source = {'status': 'done', 'filename': 'image.png', 'input_path': str(original)}
            with patch.object(cleaner, 'CLEANER_TEMP_DIR', root), \
                 patch.object(cleaner, 'get_job', return_value=source), \
                 patch.object(cleaner, 'create_job', return_value={'id': 'new'}) as create, \
                 patch.object(cleaner, 'start_cleaner_job') as start:
                self.assertEqual(cleaner.api_cleaner_retry('old', 'logo', '{"crf":24}', 'output'), {'id': 'new'})
                name, method, options, cached, output = create.call_args.args
                self.assertEqual((name, method, options, output), ('image.png', 'logo', {'crf': 24}, 'output'))
                self.assertNotEqual(Path(cached), original)
                self.assertEqual(Path(cached).read_bytes(), original.read_bytes())
                start.assert_called_once_with('new')
                source['status'] = 'processing'
                with self.assertRaises(HTTPException) as error:
                    cleaner.api_cleaner_retry('old', 'logo', '{}', '')
                self.assertEqual(error.exception.status_code, 409)
                source['status'] = 'done'
                original.unlink()
                with self.assertRaises(HTTPException) as error:
                    cleaner.api_cleaner_retry('old', 'logo', '{}', '')
                self.assertEqual(error.exception.status_code, 410)


if __name__ == '__main__':
    unittest.main()
