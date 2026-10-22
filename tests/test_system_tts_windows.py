"""Run: PYTHONPATH=backend backend/.venv/bin/python -m unittest discover -s tests -p test_system_tts_windows.py"""
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from pipeline.tts.engines import system


class WindowsSystemTtsTest(unittest.TestCase):
    def test_user_data_never_changes_powershell_program(self):
        with patch.object(system.subprocess, 'run') as run:
            for text in ("Hello", "It's fine: ‘hello’, “world”.\nTiếng Việt", "hello ’); Write-Output ZM_QUOTE_PROBE; #", "long text " * 5000, ""):
                voice = "voice ‘name’ ' & $value"
                output = Path("TTS tiếng Việt ‘quote’ & (test).wav")
                system._windows_synthesize(text, voice, output)
                args, kwargs = run.call_args
                self.assertEqual(json.loads(kwargs['input']), {'text': text or '.', 'voice': voice, 'output': str(output.resolve())})
                self.assertTrue(kwargs['input'].isascii())
                self.assertTrue(kwargs['text'])
                self.assertTrue(kwargs['check'])
                self.assertEqual(kwargs['timeout'], 120)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertTrue(all(command == commands[0] for command in commands))
            self.assertNotIn('ZM_QUOTE_PROBE', commands[0][-1])

    def test_process_failure_and_timeout_remain_failures(self):
        for error in (subprocess.CalledProcessError(1, 'powershell'), subprocess.TimeoutExpired('powershell', 120)):
            with self.subTest(error=type(error).__name__), patch.object(system.subprocess, 'run', side_effect=error):
                with self.assertRaises(type(error)):
                    system._windows_synthesize('Hello', 'voice', Path('out.wav'))

    def test_public_windows_synthesis_uses_the_same_safe_launcher(self):
        with patch.object(system.platform, 'system', return_value='Windows'), \
             patch.object(system, '_windows_synthesize') as synthesize:
            system.synthesize('Tiếng Việt', 'win:installed voice', Path('out.wav'))
            synthesize.assert_called_once_with('Tiếng Việt', 'installed voice', Path('out.wav'))


if __name__ == '__main__':
    unittest.main()
