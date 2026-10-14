import asyncio
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pipeline.flow import store
from pipeline.flow.service import FlowService


def fake_logo_module(fail_names=()):
    calls = []

    def remove(path, settings):
        calls.append(Path(path).name)
        if Path(path).name in fail_names:
            raise RuntimeError('LaMa unavailable')
        return types.SimpleNamespace(mode_used='fake')

    module = types.SimpleNamespace(
        DEFAULT_LAMA_MODEL='m', LogoRemovalSettings=lambda **kw: kw, remove_logo_from_file=remove,
    )
    return module, calls


class LogoRetryTest(unittest.TestCase):
    def test_video_completion_requires_logo_marker_except_ultra(self):
        service = FlowService()
        job = {'kind': 'video', 'outputs': ['clip.mp4'], 'settings': {'removeLogo': True}}
        with patch.object(service, '_outputs_exist', return_value=True):
            self.assertFalse(service._outputs_ready_for_completion(job, account_plan='Pro'))
            self.assertTrue(service._outputs_ready_for_completion(job, account_plan='Ultra'))
            job['logoDone'] = ['clip.mp4']
            self.assertTrue(service._outputs_ready_for_completion(job, account_plan='Pro'))

    def test_failure_keeps_images_and_retry_runs_only_logo(self):
        with tempfile.TemporaryDirectory() as raw, patch.object(store, 'ROOT', Path(raw)):
            a, b = Path(raw) / 'a.jpg', Path(raw) / 'b.jpg'
            a.write_bytes(b'x'), b.write_bytes(b'x')
            store._write('jobs', [{'id': 'j', 'kind': 'image', 'status': 'processing', 'accountId': 'acc'}])
            service = FlowService()
            outputs = [str(a), str(b)]
            module, calls = fake_logo_module(fail_names={'b.jpg'})
            with patch.dict(sys.modules, {'pipeline.logo_remover': module}), patch.object(service, '_log'):
                error = asyncio.run(service._remove_flow_logos('j', outputs))
                self.assertIn('LaMa unavailable', error)
                self.assertEqual(store.get_row('jobs', 'j')['progress'], 99.5)  # 1 of 2 files cleaned in the 99.1-99.9 band
                service._fail_logo_removal('j', outputs, error)
                job = store.get_row('jobs', 'j')
                self.assertEqual((job['status'], job['outputs']), ('failed', outputs))
                self.assertTrue(job['error'].startswith('FLOW_LOGO_REMOVE_FAILED'))
                module2, calls2 = fake_logo_module()
                sys.modules['pipeline.logo_remover'] = module2
                with patch('pipeline.flow.service.threading.Thread') as thread:
                    retried = service.retry('j', {'manual': True})
                    self.assertEqual(retried['stage'], 'removing_logo')
                    thread.assert_called_once()
                asyncio.run(service._logo_only_retry('j'))
            self.assertEqual(calls, ['a.jpg', 'b.jpg'])
            self.assertEqual(calls2, ['b.jpg'])  # a.jpg was already cleaned, never inpainted twice
            self.assertEqual(store.get_row('jobs', 'j')['status'], 'done')


if __name__ == '__main__':
    unittest.main()
