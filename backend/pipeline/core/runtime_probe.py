"""Model-free smoke test source, available in frozen apps without source files."""
PROBE_SOURCE = r'''
from __future__ import annotations

import json
import sys


def probe(profile: str, demucs: bool = False) -> dict[str, str]:
    import numpy as np
    import torch
    import torchaudio
    import cv2
    import ctranslate2
    import faster_whisper
    import soundfile
    import cffi
    from transformers import PreTrainedModel, Qwen3Model
    from vieneu.v3turbo import V3TurboVieNeuTTS
    from vieneu._v3_turbo_engine.onnx_runtime_lite import OnnxV3LiteEngine
    from vieneu._v3_turbo_engine.speaker.fbank import extract_speaker_fbank
    import sherpa_onnx
    import onnxruntime as ort
    from rapidocr_onnxruntime import RapidOCR

    assert sys.version_info[:2] == (3, 12), sys.version
    assert 'float32' in ctranslate2.get_supported_compute_types('cpu')
    assert cv2.resize(np.zeros((8, 8, 3), np.uint8), (4, 4)).shape == (4, 4, 3)
    features = extract_speaker_fbank(torch.zeros(16000), sample_rate=16000)
    assert features.shape[-1] == 80
    cuda = profile.startswith('nvidia-')
    import importlib.metadata
    installed = []
    for component in ('onnxruntime', 'onnxruntime-gpu', 'onnxruntime-directml'):
        try:
            importlib.metadata.version(component)
            installed.append(component)
        except importlib.metadata.PackageNotFoundError:
            pass
    assert len(installed) == 1, f'Conflicting ORT distributions: {installed}'
    if cuda:
        assert torch.cuda.is_available(), 'CUDA driver/device unavailable'
        assert (torch.ones(1, device='cuda') + 1).cpu().item() == 2
        torch.cuda.synchronize()
        assert ctranslate2.get_supported_compute_types('cuda'), 'Whisper CUDA unavailable'
    else:
        assert torch.version.cuda is None, 'CPU/DirectML profile must not contain CUDA Torch'
    provider = 'CUDAExecutionProvider' if cuda else (
        'DmlExecutionProvider' if profile == 'directml' else 'CPUExecutionProvider'
    )
    assert provider in ort.get_available_providers(), f'Missing {provider}'
    # ONNX Identity(float[1] -> float[1]), IR 8 / opset 13. No model download.
    model = bytes.fromhex('08083a3b0a100a017812017922084964656e74697479120570726f62655a0f0a0178120a0a08080112040a020801620f0a0179120a0a08080112040a0208014202100d')
    options = ort.SessionOptions()
    options.enable_mem_pattern = False
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    session = ort.InferenceSession(model, options, providers=[provider])
    session.disable_fallback()
    assert session.get_providers()[0] == provider, f'Provider fell back: {session.get_providers()}'
    np.testing.assert_array_equal(session.run(None, {'x': np.array([2], np.float32)})[0], [2])
    ocr = RapidOCR(det_use_cuda=cuda, cls_use_cuda=cuda, rec_use_cuda=cuda,
                   det_use_dml=profile == 'directml', cls_use_dml=profile == 'directml',
                   rec_use_dml=profile == 'directml')
    ocr(np.zeros((64, 64, 3), np.uint8))
    if demucs:
        import demucs.separate
        from demucs.demucs import Demucs
        net = Demucs(sources=['voice', 'other'], audio_channels=1, channels=4,
                     depth=1, resample=False, normalize=False)
        with torch.no_grad():
            assert net(torch.zeros(1, 1, 4096)).shape[-1] == 4096
    return {'profile': profile, 'provider': provider, 'python': sys.version, 'result': 'ok'}


if __name__ == '__main__':
    print(json.dumps(probe(sys.argv[1], '--demucs' in sys.argv[2:])))
'''

if __name__ == '__main__':
    exec(PROBE_SOURCE)
