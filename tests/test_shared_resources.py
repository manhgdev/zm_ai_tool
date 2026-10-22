"""PYTHONPATH=backend backend/.venv/bin/python -m pytest -q tests/test_shared_resources.py"""
import json
import os
from pathlib import Path
import subprocess
import sys

from web import Workers, browser_route_allowed


def test_accounts_share_installed_models_but_not_voice_data(tmp_path, monkeypatch):
    from pipeline.core import config
    from pipeline.tts.engines import moss_tts_nano as moss

    shared = tmp_path / "server-data"
    monkeypatch.setattr(config, "RESOURCE_DATA", shared)
    monkeypatch.setenv("ZM_AI_TOOL_RESOURCE_DATA", str(shared))
    for key in ("MOSS_TTS_NANO_RUNTIME_DIR", "MOSS_TTS_NANO_MODEL_DIR", "MOSS_TTS_NANO_VOICES_DIR", "MOSS_TTS_NANO_PYTHON"):
        monkeypatch.delenv(key, raising=False)
    source, model, _ = moss._paths()
    source.mkdir(parents=True)
    model.mkdir(parents=True)
    (source / "infer_onnx.py").touch()
    python = moss._python()
    python.parent.mkdir(parents=True)
    python.touch()
    (model / "model.onnx").write_bytes(b"model")
    (model / "zm-ready.json").write_text(json.dumps({
        "revision": moss.SOURCE_REVISION, "python": str(python), "source": str(source),
        "files": {"model.onnx": 5},
    }))
    backend = Path(__file__).resolve().parents[1] / "backend"
    workers = Workers(tmp_path / "web")
    code = """
import json, os
from pipeline.core.config import DATA, RESOURCE_DATA
from pipeline.tts.engines import moss_tts_nano as moss
from pipeline.tts import voice_store
from pipeline.core.system_check.install import _runtime_pip_cmd
from pipeline.export.stem import _demucs_install_root
def unexpected_download(*args, **kwargs):
    raise AssertionError('Already installed: must not download again')
moss._run = unexpected_download
assert moss.available()
assert moss.install()['needsRestart'] is False
print(json.dumps({
    'data': str(DATA), 'resources': str(RESOURCE_DATA),
    'source': str(moss._paths()[0]), 'model': str(moss._paths()[1]),
    'refs': str(moss._paths()[2]), 'python': str(moss._python()),
    'voices': str(voice_store.VIENEU_ROOT),
    'pip': _runtime_pip_cmd(), 'demucs': str(_demucs_install_root()),
    'hub': os.environ['HF_HUB_CACHE'], 'torch': os.environ['TORCH_HOME'],
}))
"""
    results = []
    for account in ("a", "b"):
        private = tmp_path / "web" / "users" / account
        env = {**os.environ, **workers.model_cache_env, "PYTHONPATH": str(backend),
               "ZM_AI_TOOL_WEB_WORKER": "1", "ZM_AI_TOOL_DATA": str(private / "data"),
               "ZM_AI_TOOL_RESOURCE_DATA": str(workers.resource_data),
               "XDG_CACHE_HOME": str(private / "cache")}
        result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30, check=True)
        results.append(json.loads(result.stdout))
    a, b = results
    for key in ("resources", "source", "model", "python", "pip", "demucs", "hub", "torch"):
        assert a[key] == b[key], key
    for key in ("data", "refs", "voices"):
        assert a[key] != b[key], key


def test_shared_install_lock_excludes_other_processes(tmp_path, monkeypatch):
    from pipeline.core import config
    from pipeline.core.resource_lock import shared_resource_lock

    monkeypatch.setenv("ZM_AI_TOOL_WEB_WORKER", "1")
    monkeypatch.setattr(config, "RESOURCE_DATA", tmp_path)
    code = """
import fcntl, sys
with open(sys.argv[1], 'a') as handle:
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print('available')
    except BlockingIOError:
        print('busy')
"""
    def probe():
        return subprocess.check_output([sys.executable, "-c", code, str(tmp_path / ".install-locks" / "packages.lock")], text=True, timeout=5).strip()
    with shared_resource_lock("packages"):
        assert probe() == "busy"
    assert probe() == "available"


def test_diarization_installer_and_resource_listing_use_shared_models(tmp_path, monkeypatch):
    import importlib.util
    from pipeline.core import config, accel, runtime_site
    from pipeline.core.system_check import install, probe
    from pipeline.asr import speaker
    from api.routes import system

    monkeypatch.setattr(config, "DATA", tmp_path / "private")
    monkeypatch.setattr(config, "RESOURCE_DATA", tmp_path / "shared")
    monkeypatch.setattr(install, "_runtime_pip_install", lambda *args, **kwargs: None)
    monkeypatch.setattr(install, "_runtime_ort_accel", lambda: "cpu")
    monkeypatch.setattr(install, "_mod_ok", lambda *args, **kwargs: (True, ""))
    monkeypatch.setattr(runtime_site, "install_runtime_meta_path", lambda: None)
    monkeypatch.setattr(runtime_site, "bootstrap_ai_runtime", lambda: None)
    monkeypatch.setattr(install, "_invalidate_checks_cache", lambda: None)

    def download(model_dir, **kwargs):
        assert model_dir == config.RESOURCE_DATA / "models" / "pyannote"
        model_dir.mkdir(parents=True)
        for name in ("model.int8.onnx", speaker.EMBEDDING_NAME):
            (model_dir / name).write_bytes(b"model")

    monkeypatch.setattr(speaker, "ensure_diarization_models", download)
    assert install.install_diarization_runtime()["ok"]
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(accel, "local_ai_runtime_profile", lambda: {"label": "CPU"})
    monkeypatch.setattr(probe, "_demucs_venv_fast", lambda: (False, ""))
    resource = next(item for item in system.api_resources()["items"] if item["id"] == "diarization")
    assert resource["installed"] is True
    assert not config.DATA.exists()


def test_web_allows_package_installers_but_not_host_controls():
    for package in ("ai_runtime", "ocr_cuda", "demucs_cuda", "nvm", "cancel"):
        path = f"/api/system/install/{package}"
        assert browser_route_allowed(path, "POST")
        assert not browser_route_allowed(path, "GET")
    for path in ("/api/system/restart", "/api/system/install/nvidia_driver", "/api/system/install/cpu_runtime", "/api/system/install/rollback"):
        assert not browser_route_allowed(path, "POST")


def test_desktop_resource_layout_is_unchanged(tmp_path):
    env = {**os.environ, "ZM_AI_TOOL_DATA": str(tmp_path), "PYTHONPATH": "backend"}
    env.pop("ZM_AI_TOOL_RESOURCE_DATA", None)
    result = subprocess.check_output([sys.executable, "-c", "from pipeline.core.config import DATA, RESOURCE_DATA; assert DATA == RESOURCE_DATA"], env=env, text=True, timeout=5)
    assert result == ""
