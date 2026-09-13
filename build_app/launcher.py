"""Packaged ZM AIO TOOL desktop window: local API + built web UI."""
from __future__ import annotations

import multiprocessing
import os
import shutil
import socket
import sys
import threading
import time
import traceback
import urllib.request
from pathlib import Path

APP_DISPLAY_NAME = "ZM AIO TOOL"


if len(sys.argv) == 3 and sys.argv[1] == "--runtime-import-check":
    # Test the *packaged* stdlib before GUI/AI startup. No browser, models or
    # user profiles are needed; windowed EXEs report through the given file.
    import importlib
    import json

    failures = {}
    for module in ("pdb", "bdb", "cmd", "code", "codeop", "multiprocessing.pool"):
        try:
            importlib.import_module(module)
        except Exception as exc:
            failures[module] = f"{type(exc).__name__}: {exc}"
    Path(sys.argv[2]).write_text(json.dumps({"ok": not failures, "errors": failures}), encoding="utf-8")
    raise SystemExit(1 if failures else 0)


def _unblock_zone_identifier(path: Path) -> bool:
    """Xóa MOTW (Zone.Identifier). Zip tải từ internet làm netfx không LoadLibrary DLL."""
    if sys.platform != "win32":
        return False
    ads = str(path) + ":Zone.Identifier"
    try:
        os.remove(ads)
        return True
    except OSError:
        pass
    try:
        import ctypes

        if ctypes.windll.kernel32.DeleteFileW(ads):
            return True
    except Exception:
        pass
    return False


def unblock_windows_motw(root: Path) -> int:
    """Remove MOTW from every native library before Python.NET/WebView2 loads.

    Explorer can propagate the download zone from a GitHub ZIP to every DLL
    inside ``_internal``.  Unblocking only Python.Runtime.dll is insufficient:
    WebView2's Core/WinForms DLLs fail with the same 0x80131515 error.
    """
    if sys.platform != "win32" or not root.is_dir():
        return 0
    n = 0
    # PyInstaller's one-dir bundle contains native libraries in package
    # folders such as webview/lib, pythonnet/runtime and clr_loader.  .pyd
    # modules are native DLLs too and can carry Zone.Identifier.
    for pattern in ("*.dll", "*.pyd"):
        try:
            libraries = root.rglob(pattern)
            for library in libraries:
                if _unblock_zone_identifier(library):
                    n += 1
        except OSError:
            # A broken optional package must not prevent the desktop launcher
            # from continuing to its normal dependency/fallback diagnostics.
            continue
    return n


def prepare_pythonnet(root: Path) -> None:
    """Load CLR sau khi gỡ MOTW; PYTHONNET_PYDLL trỏ python312.dll trong bundle."""
    if sys.platform != "win32":
        return
    unblock_windows_motw(root)
    py_dll = next((p for p in (root / "python312.dll", root / "python3.dll") if p.is_file()), None)
    if py_dll is not None:
        os.environ.setdefault("PYTHONNET_PYDLL", str(py_dll))
    os.environ.setdefault("PYTHONNET_RUNTIME", "netfx")
    try:
        import pythonnet

        try:
            pythonnet.load("netfx")
        except Exception:
            pythonnet.load("coreclr")
    except Exception:
        pass


def app_home() -> Path:
    """Return the root directory where app data lives.

    Windows (portable): APP_ROOT = folder containing the .exe — moves with
    the app to any drive the user places it on.
    macOS: ~/Library/Application Support/ZM_AIO_TOOL (standard convention).
    """
    if sys.platform == "win32":
        # Portable layout: data lives next to the executable, not in LOCALAPPDATA.
        # sys.executable is the .exe inside the onedir bundle root.
        return Path(sys.executable).resolve().parent
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ZM_AIO_TOOL"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "ZM_AIO_TOOL"


home = app_home()
home.mkdir(parents=True, exist_ok=True)


def configure_stable_temp_directory(app_data: Path) -> Path:
    """Keep desktop work files outside a transient macOS Installer sandbox.

    ``postinstall`` can launch the app while macOS still exports a TMPDIR such
    as ``/private/tmp/PKInstallSandbox...``.  That directory disappears as
    soon as Installer exits; Playwright then fails before opening Chrome while
    creating its ``playwright-artifacts-*`` directory.  A private, persistent
    app temp directory is safe for both the first launch and normal launches.
    """
    temp_dir = app_data / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    for name in ("TMPDIR", "TMP", "TEMP"):
        os.environ[name] = str(temp_dir)
    return temp_dir


configure_stable_temp_directory(home)
os.environ["VIDEO_CLONE_DESKTOP"] = "1"
os.environ.setdefault("VIDEO_CLONE_HOME", str(home))
os.environ.setdefault("VIDEO_CLONE_DATA", str(home / "data"))
# PUBLIC_DATA (project temp files) sits inside data/ — one tree, easy backup.
os.environ.setdefault("VIDEO_CLONE_PUBLIC_DATA", str(home / "data" / "public"))
os.environ.setdefault("CAPCUT_DEVICE_JSON", str(home / "data" / "capcut_device.json"))
os.environ.setdefault("UV_PYTHON_INSTALL_DIR", str(home / "data" / ".python-runtime"))
# OUTPUT_ROOT: Windows portable defaults to APP_ROOT/output; macOS to ~/Downloads/ZM_AIO_TOOL.
# ui_preferences.json can override this once via /api/config/output-root.
_default_output = (
    str(home / "output") if sys.platform == "win32"
    else str(Path.home() / "Downloads" / "ZM_AIO_TOOL")
)
os.environ.setdefault("VIDEO_CLONE_OUTPUT_ROOT", _default_output)
# httpx parse NO_PROXY IPv6 trần ``::1`` thành port ``:1`` → Whisper/HF crash.
_broken_np = {"::1", "::1/128", "[::1]", "[::1]/128"}
for _np in ("NO_PROXY", "no_proxy"):
    _raw = os.environ.get(_np)
    if _raw:
        os.environ[_np] = ",".join(
            p.strip() for p in _raw.split(",") if p.strip() not in _broken_np
        )
# Ưu tiên GPU (CUDA/MPS); giới hạn thread CPU phụ — tránh đơ máy
os.environ.setdefault("VIENEU_BACKEND", "auto")
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("MKL_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "2")
# Tắt async FFmpeg frame decoding — tránh "Assertion fctx->async_lock failed"
# (libavcodec/pthread_frame.c:173) khi VideoCapture mở video với multi-thread decoder.
# Không có env var này: cv2 dùng thread_type=FRAME theo mặc định → assertion abort().
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "threads;1")
# Windows: KHÔNG dùng MSMF vì MSMF tự động chèn viền đen (letterboxing) hoặc bóp méo 
# khung hình video dọc làm lệch tọa độ Bbox của OCR.
# Sử dụng FFmpeg với threads=1 đã khắc phục được lỗi pthread_frame.c.
if sys.platform == "win32":
    os.environ.setdefault("OPENCV_VIDEOIO_PRIORITY_MSMF", "0")
    os.environ.setdefault("OPENCV_VIDEOIO_PRIORITY_FFMPEG", "100")

# Process giám sát: nạp CUDA/cv2 chỉ trong process con. Native crash (0xC0000409)
# giết con — cha hiện popup + log để copy, không im lặng tắt.
_SUPERVISOR_ENV = "VIDEO_CLONE_SUPERVISOR_CHILD"


def _unsigned_exit(code: int) -> int:
    return code & 0xFFFFFFFF if code < 0 else int(code)


def _crash_report(exit_code: int) -> str:
    u = _unsigned_exit(exit_code)
    lines: list[str] = [
        f"{APP_DISPLAY_NAME} đã thoát bất thường.",
        f"Mã: {exit_code} (0x{u:08X})",
        f"Log: {home / 'app.log'}",
        "",
    ]
    log = home / "app.log"
    try:
        if log.is_file():
            tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]
            lines.extend(tail if tail else ["(app.log trống)"])
        else:
            lines.append("(chưa có app.log)")
    except OSError as e:
        lines.append(f"(không đọc được app.log: {e})")
    return "\n".join(lines)


def show_copyable_crash(exit_code: int) -> None:
    """Popup lỗi copy được — không để APP tắt im lặng sau native crash."""
    body = _crash_report(exit_code)
    crash_file = home / "last_crash.txt"
    try:
        crash_file.write_text(body, encoding="utf-8")
    except OSError:
        crash_file = None
    try:
        import tkinter as tk
        from tkinter.scrolledtext import ScrolledText

        root = tk.Tk()
        root.title(f"{APP_DISPLAY_NAME} — lỗi (copy gửi để sửa)")
        root.geometry("720x480")
        root.attributes("-topmost", True)
        hint = tk.Label(
            root,
            text="APP đã thoát bất thường. Copy toàn bộ nội dung dưới gửi để sửa.",
            wraplength=680,
            justify="left",
        )
        hint.pack(fill="x", padx=10, pady=(10, 4))
        box = ScrolledText(root, wrap="word", font=("Consolas", 10))
        box.pack(fill="both", expand=True, padx=10, pady=4)
        box.insert("1.0", body)
        box.focus_set()
        box.tag_add("sel", "1.0", "end")

        def copy_all() -> None:
            text = box.get("1.0", "end-1c")
            root.clipboard_clear()
            root.clipboard_append(text)
            btn.configure(text="Đã chép")

        bar = tk.Frame(root)
        bar.pack(fill="x", padx=10, pady=(0, 10))
        btn = tk.Button(bar, text="Chép lỗi", command=copy_all)
        btn.pack(side="left")
        tk.Button(bar, text="Đóng", command=root.destroy).pack(side="right")
        root.mainloop()
        return
    except Exception:
        pass
    if sys.platform == "win32" and crash_file is not None:
        try:
            import subprocess as _sp

            _sp.Popen(["notepad.exe", str(crash_file)])
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                0,
                f"APP đã thoát bất thường (0x{_unsigned_exit(exit_code):08X}).\n"
                f"Notepad đang mở log để copy:\n{crash_file}",
                f"{APP_DISPLAY_NAME} — lỗi",
                0x10 | 0x40000,
            )
            return
        except Exception:
            pass


def _supervise_child() -> int:
    import subprocess

    try:
        from pipeline.core.runtime_site import subprocess_environment

        env = subprocess_environment()
    except Exception:
        env = os.environ.copy()
    env[_SUPERVISOR_ENV] = "1"
    proc = subprocess.Popen([sys.executable, *sys.argv[1:]], env=env)
    return int(proc.wait())


if __name__ == "__main__" and os.environ.get(_SUPERVISOR_ENV) != "1":
    multiprocessing.freeze_support()
    rc = 1
    try:
        rc = _supervise_child()
    except Exception:
        traceback.print_exc()
        rc = 1
    if rc != 0:
        show_copyable_crash(rc)
    raise SystemExit(rc)

try:
    from pipeline.core.accel import apply_gpu_process_env

    apply_gpu_process_env()
except Exception:
    pass

# Các gói AI nặng được cài ở lần chạy đầu, ngoài thư mục app để nâng cấp không cần build lại EXE.
runtime_venv = home / ".venv-runtime"
runtime_site = (
    runtime_venv / "Lib" / "site-packages"
    if sys.platform == "win32"
    else runtime_venv / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
)

if getattr(sys, "frozen", False) and sys.stdout is None:
    runtime_log = (home / "app.log").open("a", encoding="utf-8", buffering=1)
    sys.stdout = runtime_log
    sys.stderr = runtime_log

if runtime_site.is_dir():
    sys.path.insert(0, str(runtime_site))
    # Không nhét nvidia/torch CUDA vào PATH của VideoClone.exe (WebView2).
    # GPU chạy trong worker .venv-runtime — python.exe đó tự load CUDA DLL.
    if getattr(sys, "frozen", False):
        try:
            from pipeline.core.runtime_site import (
                install_runtime_meta_path,
                prepare_cv2_import_path,
            )

            install_runtime_meta_path(runtime_site, gpu_in_process=False)
            prepare_cv2_import_path(runtime_site)
        except Exception:
            traceback.print_exc()

ocr_venv = home / ".venv-ocr"
ocr_site = (
    ocr_venv / "Lib" / "site-packages"
    if sys.platform == "win32"
    else ocr_venv / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
)
if ocr_site.is_dir():
    # Không nạp CUDA/ORT vào process cửa sổ. GPU OCR/TTS/Whisper = worker .venv-runtime.
    def _path_ok(p: str) -> bool:
        n = p.replace("\\", "/").rstrip("/").lower()
        if n.endswith("/cv2"):
            return False
        if "/.venv-ocr/" in f"/{n}/" or n.endswith("/.venv-ocr/lib/site-packages"):
            return False
        return True

    sys.path[:] = [p for p in sys.path if _path_ok(p)]
    if runtime_site.is_dir():
        try:
            from pipeline.core.runtime_site import prepare_cv2_import_path

            prepare_cv2_import_path(runtime_site)
        except Exception:
            _rt = str(runtime_site)
            while _rt in sys.path:
                sys.path.remove(_rt)
            sys.path.insert(0, _rt)
            sys.path[:] = [p for p in sys.path if _path_ok(p)]

bundle = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
os.environ.setdefault("VIDEO_CLONE_BUNDLE", str(bundle))
try:
    from pipeline.core.runtime_site import prepend_windows_path, sanitize_process_environment

    # Sanitize the inherited Explorer/shell PATH before adding bundle tools.
    # A bloated PATH makes CreateProcess and torch's DLL bootstrap fail with
    # WinError 206 even when the torch/lib directory itself is short.
    sanitize_process_environment()
    prepend_windows_path(bundle)
except Exception:
    os.environ["PATH"] = os.pathsep.join((str(bundle), os.environ.get("PATH", "")))
if sys.platform == "win32":
    _motw_removed = unblock_windows_motw(bundle)
    if _motw_removed:
        print(f"[desktop] removed MOTW from {_motw_removed} native library file(s)", flush=True)

# Chocolatey ShimGen copy vào _internal trỏ `..\lib\ffmpeg\...` → exit 4294967295.
# Đưa thư mục ffmpeg thật (ngoài bundle) lên trước để bare `ffmpeg` không dính shim.
if sys.platform == "win32":
    _ff_bundled = bundle / "ffmpeg.exe"
    _ff_ok = False
    try:
        _ff_ok = _ff_bundled.is_file() and _ff_bundled.stat().st_size >= 2_000_000
    except OSError:
        pass
    if not _ff_ok:
        try:
            _bundle_res = bundle.resolve()
        except OSError:
            _bundle_res = bundle
        for _d in os.environ.get("PATH", "").split(os.pathsep):
            if not _d:
                continue
            _p = Path(_d)
            try:
                if _p.resolve() == _bundle_res:
                    continue
            except OSError:
                pass
            _cand = _p / "ffmpeg.exe"
            if _cand.is_file():
                try:
                    prepend_windows_path(_cand.parent)
                except Exception:
                    os.environ["PATH"] = os.pathsep.join((str(_cand.parent), os.environ["PATH"]))
                break

# Seed giọng zmAI đi kèm; không ghi đè giọng hoặc metadata người dùng đã sửa.
# ponytail: kiểm tra mtime — không copy nếu target mới hơn source (tránh chậm startup mỗi lần).
bundled_voice_refs = bundle / "resources" / "voice-ref"
user_voice_refs = home / "resources" / "voice-ref"
if bundled_voice_refs.is_dir():
    user_voice_refs.mkdir(parents=True, exist_ok=True)
    for source in bundled_voice_refs.iterdir():
        if not source.is_file():
            continue
        target = user_voice_refs / source.name
        # ONEFILE giải nén lại mỗi lần chạy → mtime nguồn LUÔN mới hơn, so mtime
        # sẽ ghi đè file người dùng đã sửa. Chỉ seed khi target chưa có, hoặc
        # nội dung khác và target chưa từng bị sửa (cùng size = bản seed cũ).
        if not target.exists():
            shutil.copy2(source, target)
            (user_voice_refs / f".{source.name}.seeded").touch()
            continue
        try:
            if source.stat().st_size != target.stat().st_size:
                # Bản bundle đổi nội dung: chỉ ghi đè khi user chưa sửa gì
                # (đánh dấu bằng file .seeded cạnh bên).
                marker = user_voice_refs / f".{source.name}.seeded"
                if marker.is_file():
                    shutil.copy2(source, target)
                    marker.touch()
        except OSError:
            pass

# Ẩn cửa sổ console đen khi app GUI spawn ffmpeg / demucs / nvidia-smi
if sys.platform == "win32":
    try:
        import subprocess as _sp

        _no_win = int(getattr(_sp, "CREATE_NO_WINDOW", 0x08000000))
        _OrigPopen = _sp.Popen

        # Resolve ffmpeg/ffprobe → absolute path trong bundle để tránh
        # trailing-space PATH trên Windows ('ffprobe ' → exit 4294967295).
        _BUNDLED_BINS: dict[str, str] = {}
        _meipass = getattr(sys, "_MEIPASS", None)
        if _meipass:
            for _name in ("ffmpeg", "ffprobe", "uv"):
                _cand = os.path.join(_meipass, f"{_name}.exe")
                if not os.path.isfile(_cand):
                    continue
                # Bỏ Chocolatey shim (~400KB) — copy vào _internal thì gãy.
                if _name in ("ffmpeg", "ffprobe"):
                    try:
                        if os.path.getsize(_cand) < 2_000_000:
                            continue
                    except OSError:
                        continue
                _BUNDLED_BINS[_name] = _cand
                _BUNDLED_BINS[f"{_name}.exe"] = _cand

        class _PopenNoWindow(_OrigPopen):  # type: ignore[misc, valid-type]
            def __init__(self, *a, **kw):
                if kw.get("creationflags") is None and not kw.get("shell"):
                    kw["creationflags"] = _no_win
                # Fix trailing-space executables & resolve bundled binaries
                args = a[0] if a else kw.get("args")
                if args is not None and isinstance(args, (list, tuple)) and args:
                    exe = str(args[0]).strip()
                    # Nếu chỉ là bare name (không có path separator), resolve từ bundle
                    if os.sep not in exe and "/" not in exe:
                        resolved = _BUNDLED_BINS.get(exe) or _BUNDLED_BINS.get(exe.lower())
                        if resolved:
                            exe = resolved
                    if exe != args[0]:
                        args = list(args)
                        args[0] = exe
                        if a:
                            a = (args, *a[1:])
                        else:
                            kw["args"] = args
                from pipeline.core.runtime_site import external_process_dll_search

                executable = str(args[0]) if isinstance(args, (list, tuple)) and args else ""
                with external_process_dll_search(executable):
                    super().__init__(*a, **kw)

        _sp.Popen = _PopenNoWindow  # type: ignore[misc, assignment]
    except Exception:
        traceback.print_exc()


def app_version() -> str:
    for candidate in (
        bundle / "VERSION",
        Path(__file__).resolve().parent / "VERSION",
    ):
        try:
            v = candidate.read_text(encoding="utf-8").strip()
            if v:
                return v
        except OSError:
            pass
    return "1.0.0"


APP_VERSION = app_version()
os.environ.setdefault("VIDEO_CLONE_VERSION", APP_VERSION)

from fastapi.staticfiles import StaticFiles  # noqa: E402
from main import app  # noqa: E402
import uvicorn  # noqa: E402
import webview  # noqa: E402

web_dir = bundle / "dist"
app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")


API_HOST = "127.0.0.1"


def api_base(port: int) -> str:
    return f"http://{API_HOST}:{port}"


def server_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"{api_base(port)}/api/health", timeout=1) as response:
            if response.status != 200:
                return False
            body = response.read(512).decode("utf-8", errors="replace")
            return '"app":"videoclone"' in body.replace(" ", "")
    except Exception:
        return False


def wait_for_server(port: int, timeout: float = 120.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server_running(port):
            return True
        time.sleep(0.1)
    return False


def wait_for_parent_exit(pid: int) -> None:
    """Let a replacement desktop process wait until the old API releases its port."""
    if sys.platform == "win32":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)
        if handle:
            ctypes.windll.kernel32.WaitForSingleObject(handle, 30_000)
            ctypes.windll.kernel32.CloseHandle(handle)
        return
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.1)


def centered_xy(width: int, height: int) -> tuple[int, int]:
    """Góc trên-trái để cửa sổ nằm giữa màn hình chính."""
    sw, sh = 1920, 1080
    try:
        if sys.platform == "win32":
            import ctypes

            user32 = ctypes.windll.user32
            sw = int(user32.GetSystemMetrics(0))
            sh = int(user32.GetSystemMetrics(1))
        elif sys.platform == "darwin":
            try:
                from AppKit import NSScreen  # type: ignore

                frame = NSScreen.mainScreen().frame()
                sw, sh = int(frame.size.width), int(frame.size.height)
            except Exception:
                pass
        else:
            try:
                import subprocess

                out = subprocess.check_output(
                    ["xrandr"], text=True, stderr=subprocess.DEVNULL, timeout=2
                )
                for line in out.splitlines():
                    if " connected" in line and " primary " in line:
                        # e.g. "eDP-1 connected primary 1920x1080+0+0"
                        for part in line.split():
                            if "x" in part and "+" in part:
                                res = part.split("+")[0]
                                w_s, h_s = res.split("x", 1)
                                sw, sh = int(w_s), int(h_s)
                                break
                        break
            except Exception:
                pass
    except Exception:
        pass
    x = max(0, (sw - width) // 2)
    y = max(0, (sh - height) // 2)
    return x, y


_single_instance_handle = None


def _activate_existing_window() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def visit(hwnd, _lparam):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                title = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, title, length + 1)
                if title.value.startswith(f"{APP_DISPLAY_NAME} v"):
                    user32.ShowWindow(hwnd, 9)
                    user32.SetForegroundWindow(hwnd)
                    return False
            return True

        user32.EnumWindows(callback_type(visit), 0)
    except Exception:
        pass


def acquire_single_instance() -> bool:
    """Only one desktop window; a second launch focuses the existing instance."""
    global _single_instance_handle
    if sys.platform != "win32":
        return True
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, "Local\\ZMAIOTool.Desktop")
    if not handle:
        return True
    if kernel32.GetLastError() == 183:
        kernel32.CloseHandle(handle)
        _activate_existing_window()
        return False
    _single_instance_handle = handle
    return True


def run_desktop() -> int:
    if not acquire_single_instance():
        return 0
    prepare_pythonnet(bundle)
    try:
        from pipeline.core.app_log import append_log, install_process_hooks

        install_process_hooks()
        append_log(f"[desktop] start v{APP_VERSION}")
    except Exception:
        traceback.print_exc()
    # The desktop app only needs a private loopback endpoint; let the OS assign it.
    # Keep this socket open until Uvicorn adopts it so another process cannot claim it.
    api_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    api_socket.bind((API_HOST, 0))
    port = int(api_socket.getsockname()[1])
    os.environ["VIDEO_CLONE_PORT"] = str(port)
    base = api_base(port)

    config = uvicorn.Config(app, host=API_HOST, port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [api_socket]},
        name="videoclone-api",
        daemon=True,
    )
    thread.start()
    _t0 = time.monotonic()
    print(f"{APP_DISPLAY_NAME} v{APP_VERSION} — chờ API...", flush=True)
    try:
        if not wait_for_server(port):
            print(f"[desktop] API không khởi được sau {time.monotonic()-_t0:.1f}s", flush=True)
            # Giữ cửa sổ thông báo thay vì im lặng exit
            try:
                webview.create_window(
                    f"{APP_DISPLAY_NAME} v{APP_VERSION}",
                    html=(
                        "<html><body style='font-family:sans-serif;padding:2rem'>"
                        f"<h2>Không mở được API</h2><p>{base}</p>"
                        "<p>Xem log: %LOCALAPPDATA%\\VideoClone\\app.log</p>"
                        "</body></html>"
                    ),
                    width=520,
                    height=280,
                    text_select=True,
                )
                webview.start()
            except Exception:
                traceback.print_exc()
            return 1
        print(f"[desktop] API sẵn sàng sau {time.monotonic()-_t0:.1f}s tại {base}", flush=True)
        win_w, win_h = 1440, 900
        x, y = centered_xy(win_w, win_h)
        icon = None
        for cand in (bundle / "app.ico", Path(__file__).resolve().parent / "app.ico"):
            if cand.is_file():
                icon = str(cand)
                break
        win_kw: dict = dict(
            width=win_w,
            height=win_h,
            x=x,
            y=y,
            min_size=(960, 640),
            maximized=True,
            text_select=True,
        )
        if icon:
            win_kw["icon"] = icon
        try:
            webview.create_window(
                f"{APP_DISPLAY_NAME} v{APP_VERSION}",
                f"{base}/?v={APP_VERSION}",
                **win_kw,
            )
        except TypeError:
            # pywebview cũ có thể không hỗ trợ icon= hoặc text_select=.
            win_kw.pop("icon", None)
            win_kw.pop("text_select", None)
            webview.create_window(
                f"{APP_DISPLAY_NAME} v{APP_VERSION}",
                f"{base}/?v={APP_VERSION}",
                **win_kw,
            )
        # webview.start() chặn đến khi user đóng cửa sổ — không thoát vì lỗi job nền
        try:
            webview.start(gui="edgechromium", debug=False)
        except Exception:
            try:
                webview.start(debug=False)
            except Exception:
                traceback.print_exc()
                msg = (
                    "webview.start failed: Python.Runtime.dll bị Windows chặn (MOTW) "
                    "hoặc thiếu .NET/WebView2. Copy thư mục app ra ngoài Downloads, "
                    "hoặc Properties → Unblock trên Python.Runtime.dll."
                )
                print(f"[desktop] {msg}", flush=True)
                try:
                    from pipeline.core.app_log import append_log

                    append_log(f"[desktop] {msg}")
                except Exception:
                    pass
                return 1
        return 0
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        api_socket.close()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "--yt-dlp-cli":
            from yt_dlp import main as ytdlp_main

            raise SystemExit(ytdlp_main(sys.argv[2:]) or 0)
        if len(sys.argv) == 3 and sys.argv[1] == "--restart-after":
            wait_for_parent_exit(int(sys.argv[2]))
        elif len(sys.argv) > 1:
            raise SystemExit(2)
        raise SystemExit(run_desktop())
    except SystemExit:
        raise
    except BaseException:
        # Mọi lỗi khởi động: ghi log, không silent die
        traceback.print_exc()
        try:
            (home / "app.log").open("a", encoding="utf-8").write(
                f"\n[fatal] {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            )
            traceback.print_exc(file=(home / "app.log").open("a", encoding="utf-8"))
        except Exception:
            pass
        raise SystemExit(1)
