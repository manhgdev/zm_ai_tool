import sys
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pipeline.tts import zmtss_catalog


def test_download_normalization_temp_keeps_wav_extension(tmp_path):
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.side_effect = [b'raw-audio', b'']
    result = Mock(returncode=0, stderr='')

    def run(command, **kwargs):
        output = Path(command[-1])
        assert output.suffix == '.wav'
        output.write_bytes(b'normalized' * 200)
        return result

    destination = tmp_path / 'zmt-demo.wav'
    with patch.object(zmtss_catalog.urllib.request, 'urlopen', return_value=response), \
         patch.object(zmtss_catalog.subprocess, 'run', side_effect=run):
        zmtss_catalog.download_reference({'id': 'demo', 'audio': 'demo.mp3'}, destination)
    assert destination.is_file()
