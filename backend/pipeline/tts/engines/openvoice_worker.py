"""Isolated worker source; no torch imports in the API process."""

SCRIPT = r'''
import contextlib, hashlib, json, os, sys, tempfile
from pathlib import Path

protocol = sys.stdout
sys.stdout = sys.stderr
import numpy as np
import soundfile as sf
import torch
from openvoice.api import ToneColorConverter, OpenVoiceBaseClass

class Converter(ToneColorConverter):
    def __init__(self, config, device):
        # Upstream forwards enable_watermark to a base constructor that does
        # not accept it. Initialize just the converter, without wavmark.
        OpenVoiceBaseClass.__init__(self, config, device=device)
        self.watermark_model = None
        self.version = getattr(self.hps, '_version_', 'v2')

root = Path(sys.argv[1])
identity = sys.argv[2]
converter = None
device = 'cpu'
if sys.platform != 'darwin' and torch.cuda.is_available():
    try:
        torch.ones(1, device='cuda').sum().item()
        device = 'cuda'
    except RuntimeError:
        pass

def emit(**data):
    protocol.write(json.dumps(data) + '\n')
    protocol.flush()

def checked_audio(path):
    samples, rate = sf.read(str(path), dtype='float32', always_2d=True)
    if len(samples) < rate // 5 or not np.isfinite(samples).all() or np.max(np.abs(samples)) < 1e-5:
        raise ValueError('OPENVOICE_INVALID_AUDIO')
    return samples, rate

def embedding(path):
    checked_audio(path)
    digest = hashlib.sha256(identity.encode() + Path(path).read_bytes()).hexdigest()
    cache = root / 'embeddings'
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / (digest + '.pt')
    if target.is_file():
        try:
            value = torch.load(target, map_location=device, weights_only=True)
            if isinstance(value, torch.Tensor) and tuple(value.shape) == (1, converter.hps.model.gin_channels, 1) and torch.isfinite(value).all():
                return value
        except Exception:
            pass
    value = converter.extract_se(str(path))
    if not torch.isfinite(value).all():
        raise ValueError('OPENVOICE_INVALID_AUDIO')
    fd, name = tempfile.mkstemp(dir=cache, suffix='.tmp')
    os.close(fd)
    try:
        torch.save(value.detach().cpu(), name)
        os.replace(name, target)
    finally:
        Path(name).unlink(missing_ok=True)
    return value

for line in sys.stdin:
    try:
        request = json.loads(line)
        if converter is None:
            # Conversion only: no BaseSpeakerTTS, ASR, or watermark model download.
            converter = Converter(str(root / 'config.json'), device=device)
            converter.load_ckpt(str(root / 'checkpoint.pth'))
        if request.get('op') == 'probe':
            emit(ok=True, device=device)
            continue
        emit(stage='embedding', progress=0.5)
        source = embedding(request['source'])
        target = embedding(request['reference'])
        emit(stage='conversion', progress=0.7)
        converter.convert(request['source'], source, target, output_path=request['output'])
        checked_audio(request['output'])
        emit(ok=True, device=device)
    except Exception as exc:
        print(str(exc), file=sys.stderr, flush=True)
        emit(ok=False, error='OPENVOICE_INVALID_AUDIO' if 'OPENVOICE_INVALID_AUDIO' in str(exc) else 'OPENVOICE_CONVERSION_FAILED')
'''
