"""Build immutable Windows runtime ZIP layers and their signed-by-hash manifest."""
from __future__ import annotations

import argparse
import email.parser
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPECS = ROOT / "runtime_packs"
PACKS = (
    "core-ai-win-x64",
    "gpu-directml",
    "gpu-nvidia-cu124",
    "gpu-nvidia-cu128",
    "gpu-torch-cpu",
    "feature-demucs",
)
SPEC_NAME = {
    "core-ai-win-x64": "core-win-x64.requirements.txt",
    "gpu-directml": "gpu-directml.requirements.txt",
    "gpu-nvidia-cu124": "gpu-nvidia-cu124.requirements.txt",
    "gpu-nvidia-cu128": "gpu-nvidia-cu128.requirements.txt",
    "gpu-torch-cpu": "gpu-torch-cpu.requirements.txt",
    "feature-demucs": "feature-demucs.requirements.txt",
}
TORCH_INDEX = {
    "gpu-nvidia-cu124": "https://download.pytorch.org/whl/cu124",
    "gpu-nvidia-cu128": "https://download.pytorch.org/whl/cu128",
    "gpu-torch-cpu": "https://download.pytorch.org/whl/cpu",
}
PYTHON_VERSION = "3.12.10"


def run(command: list[str], **kwargs) -> None:
    print("+", subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, check=True, **kwargs)


def find_uv() -> str | None:
    """Find uv both on PATH and in the repository build virtualenv."""
    configured = (os.environ.get("UV_EXECUTABLE") or "").strip()
    candidates = (
        configured,
        shutil.which("uv") or "",
        str(ROOT / "backend" / ".venv" / "Scripts" / "uv.exe"),
        str(ROOT / "backend" / ".venv" / "bin" / "uv"),
    )
    return next((item for item in candidates if item and Path(item).is_file()), None)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def zip_tree(source: Path, destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for item in sorted(source.rglob("*")):
            if item.is_file():
                bundle.write(item, item.relative_to(source))


def freeze(python: Path) -> list[str]:
    proc = subprocess.run(
        [str(python), "-m", "pip", "freeze", "--all"],
        check=True, capture_output=True, text=True,
    )
    return sorted(line.strip() for line in proc.stdout.splitlines() if line.strip())


def wheel_lock(wheelhouse: Path) -> list[dict[str, object]]:
    return [
        {"file": wheel.name, "sha256": sha256(wheel), "size": wheel.stat().st_size}
        for wheel in sorted(wheelhouse.glob("*.whl"))
    ]


def installed_lock(site: Path) -> list[str]:
    packages: list[str] = []
    for metadata in sorted(site.glob("*.dist-info/METADATA")):
        parsed = email.parser.Parser().parsestr(metadata.read_text(encoding="utf-8", errors="replace"))
        name, version = parsed.get("Name"), parsed.get("Version")
        if name and version:
            packages.append(f"{name}=={version}")
    return sorted(packages, key=str.casefold)


def build_core(work: Path) -> tuple[Path, list[str], list[dict[str, object]]]:
    target = work / "core"
    uv = find_uv()
    if not uv:
        raise RuntimeError("uv is required to build a relocatable runtime")
    run([uv, "python", "install", PYTHON_VERSION])
    run([
        uv, "venv", "--python", PYTHON_VERSION, "--managed-python",
        "--seed", "--relocatable", str(target),
    ])
    python = target / "Scripts/python.exe"
    run([str(python), "-m", "pip", "install", "--upgrade", "pip==25.2"])
    wheels = work / "wheels-core"
    wheels.mkdir()
    run([str(python), "-m", "pip", "download", "--dest", str(wheels), "-r", str(SPECS / SPEC_NAME["core-ai-win-x64"])])
    run([str(python), "-m", "pip", "download", "--dest", str(wheels), "--no-deps", "vieneu==3.2.0"])
    run([
        str(python), "-m", "pip", "install", "--no-index", "--find-links", str(wheels),
        "-r", str(SPECS / SPEC_NAME["core-ai-win-x64"]),
    ])
    # Avoid the upstream Gradio/UI dependency graph. Runtime dependencies used
    # by ZM AI TOOL are pinned explicitly in the core spec.
    run([str(python), "-m", "pip", "install", "--no-index", "--find-links", str(wheels), "--no-deps", "vieneu==3.2.0"])
    probe = "import faster_whisper,rapidocr_onnxruntime,PIL,cv2,transformers,vieneu,soundfile,sherpa_onnx,cffi"
    run([str(python), "-I", "-c", probe])
    return target, freeze(python), wheel_lock(wheels)


def build_layer(pack_id: str, work: Path) -> tuple[Path, list[str], list[dict[str, object]]]:
    target = work / pack_id
    site = target / "Lib/site-packages"
    site.mkdir(parents=True)
    wheels = work / f"wheels-{pack_id}"
    wheels.mkdir()
    if pack_id == "feature-demucs":
        requirements = [
            line.strip() for line in (SPECS / SPEC_NAME[pack_id]).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        run([sys.executable, "-m", "pip", "download", "--dest", str(wheels), "--no-deps", *requirements])
        run([
            sys.executable, "-m", "pip", "install", "--target", str(site),
            "--no-index", "--find-links", str(wheels), "--no-deps", *requirements,
        ])
    elif pack_id in TORCH_INDEX:
        requirements = [
            line.strip() for line in (SPECS / SPEC_NAME[pack_id]).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        torch_specs = [item for item in requirements if item.startswith(("torch==", "torchaudio=="))]
        sherpa_specs = [item for item in requirements if item.startswith("sherpa-onnx==")]
        other_specs = [item for item in requirements if item not in torch_specs and item not in sherpa_specs]
        run([sys.executable, "-m", "pip", "download", "--dest", str(wheels), "--index-url", TORCH_INDEX[pack_id], *torch_specs])
        if other_specs:
            run([sys.executable, "-m", "pip", "download", "--dest", str(wheels), *other_specs])
        if sherpa_specs:
            run([
                sys.executable, "-m", "pip", "download", "--dest", str(wheels),
                "--no-deps", "-f", "https://k2-fsa.github.io/sherpa/onnx/cuda.html",
                *sherpa_specs,
            ])
        run([
            sys.executable, "-m", "pip", "install", "--target", str(site),
            "--no-index", "--find-links", str(wheels), *requirements,
        ])
    else:
        run([sys.executable, "-m", "pip", "download", "--dest", str(wheels), "-r", str(SPECS / SPEC_NAME[pack_id])])
        run([sys.executable, "-m", "pip", "install", "--target", str(site), "--no-index", "--find-links", str(wheels), "-r", str(SPECS / SPEC_NAME[pack_id])])
    # Layer lock is derived from installed dist-info, independent of host pip.
    locked = installed_lock(site)
    return target, locked, wheel_lock(wheels)


def validate_compositions(work: Path, trees: dict[str, Path]) -> None:
    """Probe the exact layer combinations shipped to users."""
    core_probe = "import cv2,faster_whisper,rapidocr_onnxruntime,transformers,vieneu,soundfile,sherpa_onnx,cffi; "
    combinations = {
        "cpu": ((), core_probe),
        "directml": (("gpu-directml",), core_probe + "import onnxruntime as o; assert 'DmlExecutionProvider' in o.get_available_providers()"),
        "cu124": (("gpu-nvidia-cu124",), core_probe + "import torch,torchaudio,onnxruntime; x=torch.ones(1); assert x.item()==1"),
        "cu128": (("gpu-nvidia-cu128",), core_probe + "import torch,torchaudio,onnxruntime; x=torch.ones(1); assert x.item()==1"),
        "demucs-cpu": (("gpu-torch-cpu", "feature-demucs"), core_probe + "import torch,torchaudio,demucs.separate,omegaconf,antlr4"),
    }
    for name, (layers, probe) in combinations.items():
        target = work / f"validate-{name}"
        shutil.copytree(trees["core-ai-win-x64"], target)
        for layer in layers:
            shutil.copytree(trees[layer], target, dirs_exist_ok=True)
        python = target / "Scripts/python.exe"
        run([str(python), "-I", "-c", probe])


def write_manifest_from_archives(output: Path, runtime_version: str, base_url: str) -> dict[str, object]:
    packs: dict[str, object] = {}
    for pack_id in PACKS:
        archive = output / f"{pack_id}-{runtime_version}.zip"
        if not archive.is_file():
            raise FileNotFoundError(f"runtime archive missing: {archive}")
        with zipfile.ZipFile(archive) as bundle:
            unpacked = sum(info.file_size for info in bundle.infolist() if not info.is_dir())
        packs[pack_id] = {
            "url": f"{base_url}{archive.name}",
            "sha256": sha256(archive),
            "size": archive.stat().st_size,
            "unpackedSize": unpacked,
        }
    manifest: dict[str, object] = {
        "schemaVersion": 1,
        "runtimeVersion": runtime_version,
        "appVersion": runtime_version,
        "pythonVersion": PYTHON_VERSION,
        "architecture": "win-x64",
        "packs": packs,
    }
    (output / "runtime-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-version", required=True)
    parser.add_argument("--base-url", default="")
    parser.add_argument("--pack", choices=PACKS, action="append")
    parser.add_argument("--manifest-only", action="store_true")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("runtime packs must be built on Windows")
    args.output.mkdir(parents=True, exist_ok=True)
    if args.manifest_only:
        manifest = write_manifest_from_archives(args.output, args.runtime_version, args.base_url)
        print(json.dumps(manifest, indent=2))
        return 0
    selected = tuple(args.pack or PACKS)
    manifest: dict[str, object] = {
        "schemaVersion": 1,
        "runtimeVersion": args.runtime_version,
        "appVersion": args.runtime_version,
        "pythonVersion": PYTHON_VERSION,
        "architecture": "win-x64",
        "packs": {},
    }
    with tempfile.TemporaryDirectory(prefix="zmaio-runtime-build-") as raw:
        work = Path(raw)
        trees: dict[str, Path] = {}
        for pack_id in selected:
            tree, locked, wheels = build_core(work) if pack_id == "core-ai-win-x64" else build_layer(pack_id, work)
            trees[pack_id] = tree
            lock_path = tree / "runtime-lock.json"
            lock_path.write_text(json.dumps({"pack": pack_id, "packages": locked, "wheels": wheels}, indent=2), encoding="utf-8")
            archive = args.output / f"{pack_id}-{args.runtime_version}.zip"
            unpacked = tree_size(tree)
            zip_tree(tree, archive)
            manifest["packs"][pack_id] = {  # type: ignore[index]
                "url": f"{args.base_url}{archive.name}",
                "sha256": sha256(archive),
                "size": archive.stat().st_size,
                "unpackedSize": unpacked,
            }
        if set(selected) == set(PACKS):
            validate_compositions(work, trees)
    (args.output / "runtime-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
