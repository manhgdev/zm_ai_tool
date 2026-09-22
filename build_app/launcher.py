"""Packaged ZM AI TOOL desktop window: local API + built web UI."""
from __future__ import annotations

import html
import json
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

APP_DISPLAY_NAME = "ZM AI TOOL"
_SUPERVISOR_ENV = "ZM_AI_TOOL_SUPERVISOR_CHILD"


def prepare_pythonnet(root: Path) -> None:
    """Load CLR without altering Windows download/security metadata."""
    if sys.platform != "win32":
        return
    py_dll = next((p for p in (root / "python312.dll", root / "python3.dll") if p.is_file()), None)
    if py_dll is not None:
        os.environ["PYTHONNET_PYDLL"] = str(py_dll)
    else:
        os.environ.pop("PYTHONNET_PYDLL", None)
    os.environ["PYTHONNET_RUNTIME"] = "netfx"
    try:
        import pythonnet

        try:
            pythonnet.load("netfx")
        except Exception:
            pythonnet.load("coreclr")
    except Exception:
        pass


def packaged_runtime_import_check(report: Path) -> int:
    """Verify the frozen stdlib and Windows GUI bridge without opening a window."""
    import importlib
    import json

    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    if sys.platform == "win32":
        prepare_pythonnet(root)
    modules = ["pdb", "bdb", "cmd", "code", "codeop", "multiprocessing.pool", "webview"]
    if sys.platform == "win32":
        modules += ["clr", "webview.platforms.edgechromium"]
    failures = {}
    for module in modules:
        try:
            importlib.import_module(module)
        except Exception as exc:
            failures[module] = f"{type(exc).__name__}: {exc}"
    report.write_text(
        json.dumps({"ok": not failures, "errors": failures}, ensure_ascii=False),
        encoding="utf-8",
    )
    return 1 if failures else 0


def packaged_ytdlp_version_check(report: Path) -> int:
    """Write the embedded yt-dlp version even when the Windows EXE has no console."""
    try:
        from yt_dlp.version import __version__

        report.write_text(str(__version__), encoding="utf-8")
        return 0
    except Exception as exc:
        report.write_text(f"{type(exc).__name__}: {exc}", encoding="utf-8")
        return 1


if len(sys.argv) == 3 and sys.argv[1] == "--runtime-import-check":
    raise SystemExit(packaged_runtime_import_check(Path(sys.argv[2])))
if len(sys.argv) == 3 and sys.argv[1] == "--yt-dlp-version-check":
    raise SystemExit(packaged_ytdlp_version_check(Path(sys.argv[2])))
if sys.argv[1:] == ["--yt-dlp-cli", "--version"]:
    from yt_dlp import main as _ytdlp_version_main

    raise SystemExit(_ytdlp_version_main(["--version"]) or 0)


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
    if sys.platform != "win32" or _single_instance_handle is not None:
        return True
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, "Local\\ZMAITool.Desktop")
    if not handle:
        return True
    if kernel32.GetLastError() == 183:
        kernel32.CloseHandle(handle)
        _activate_existing_window()
        return False
    _single_instance_handle = handle
    return True


_portable_ui_child = (
    sys.platform == "win32"
    and __name__ == "__main__"
    and not sys.argv[1:]
    and os.environ.get(_SUPERVISOR_ENV) == "1"
)
if _portable_ui_child and not acquire_single_instance():
    raise SystemExit(0)


def app_home() -> Path:
    """Return the root directory where app data lives.

    Windows Setup: always use LocalAppData. Windows Portable: use the folder
    containing the EXE when writable, with a LocalAppData fallback for a
    read-only extraction location.
    macOS: ~/Library/Application Support/ZM_AI_TOOL (standard convention).
    """
    if sys.platform == "win32":
        from portable_layout import windows_portable_home

        return windows_portable_home(Path(sys.executable))[0]
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ZM_AI_TOOL"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "ZM_AI_TOOL"


_portable_root = Path(sys.executable).absolute().parent if sys.platform == "win32" else None
_portable_fallback_from: Path | None = None
_migration_lines: list[str] = []
_migration_errors: list[str] = []
_previous_homes: list[Path] = []
if sys.platform == "win32":
    from portable_layout import (
        migrate_windows_state,
        sync_windows_portable_root,
        windows_portable_home,
    )

    home, _portable_fallback_from = windows_portable_home(Path(sys.executable))
    if getattr(sys, "frozen", False) and _portable_ui_child:
        _migrated_from, _migration_lines, _migration_errors = migrate_windows_state(
            home, Path(sys.executable)
        )
        _previous_homes = sync_windows_portable_root(home, _migrated_from)
        if _migration_lines or _migration_errors:
            try:
                with (home / "app.log").open("a", encoding="utf-8") as _bootstrap_log:
                    for _line in _migration_lines:
                        _bootstrap_log.write(f"[portable] migrated: {_line}\n")
                    for _line in _migration_errors:
                        _bootstrap_log.write(f"[portable] migration failed: {_line}\n")
            except OSError:
                pass
else:
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


def set_desktop_path(name: str, value: Path | str) -> None:
    """Windows updaters inherit stale paths; other platforms retain explicit overrides."""
    if sys.platform == "win32":
        os.environ[name] = str(value)
    else:
        os.environ.setdefault(name, str(value))


os.environ["ZM_AI_TOOL_DESKTOP"] = "1"
set_desktop_path("ZM_AI_TOOL_HOME", home)
if _portable_root is not None:
    os.environ["ZM_AI_TOOL_PORTABLE_ROOT"] = str(_portable_root)
else:
    os.environ.pop("ZM_AI_TOOL_PORTABLE_ROOT", None)
if _portable_fallback_from is not None:
    os.environ["ZM_AI_TOOL_PORTABLE_FALLBACK_FROM"] = str(_portable_fallback_from)
else:
    os.environ.pop("ZM_AI_TOOL_PORTABLE_FALLBACK_FROM", None)
if _previous_homes:
    os.environ["ZM_AI_TOOL_PREVIOUS_HOME"] = os.pathsep.join(map(str, _previous_homes))
else:
    os.environ.pop("ZM_AI_TOOL_PREVIOUS_HOME", None)
set_desktop_path("ZM_AI_TOOL_DATA", home / "data")
# PUBLIC_DATA (project temp files) sits inside data/ — one tree, easy backup.
set_desktop_path("ZM_AI_TOOL_PUBLIC_DATA", home / "data" / "public")
set_desktop_path("CAPCUT_DEVICE_JSON", home / "data" / "capcut_device.json")
set_desktop_path("UV_PYTHON_INSTALL_DIR", home / "data" / ".python-runtime")
if sys.platform == "win32":
    # Keep large installers/models on the same portable drive and across app updates.
    os.environ["UV_CACHE_DIR"] = str(home / "data" / "cache" / "uv")
    os.environ["HF_HOME"] = str(home / "data" / "cache" / "hf")
    os.environ["TORCH_HOME"] = str(home / "data" / "cache" / "torch")
# OUTPUT_ROOT: Windows portable defaults to PORTABLE_ROOT/output; macOS to ~/Downloads/ZM_AI_TOOL.
# ui_preferences.json can override this once via /api/config/output-root.
_default_output = (
    str(home / "output") if sys.platform == "win32"
    else str(Path.home() / "Downloads" / "ZM_AI_TOOL")
)
set_desktop_path("ZM_AI_TOOL_OUTPUT_ROOT", _default_output)
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

# Runtime pack được kích hoạt qua runtime/current.json; .venv-runtime chỉ là
# fallback tương thích cho bản cũ trong lúc migration.
try:
    from pipeline.core.runtime_active import active_runtime_dir, runtime_site as _active_runtime_site

    runtime_venv = active_runtime_dir()
    runtime_site = _active_runtime_site()
except Exception:
    runtime_venv = home / ".venv-runtime"
    runtime_site = (
        runtime_venv / "Lib" / "site-packages"
        if sys.platform == "win32"
        else runtime_venv / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    )

# Mở log ngay — trước mọi import nặng để crash lúc khởi động luôn được ghi.
if getattr(sys, "frozen", False):
    _log_path = home / "app.log"
    _runtime_log = _log_path.open("a", encoding="utf-8", buffering=1)
    if _portable_fallback_from is not None:
        _layout_reason = (
            "installed app"
            if (_portable_fallback_from / ".zmaio-installed").is_file()
            else "read-only portable folder"
        )
        _runtime_log.write(
            f"[windows] Dùng dữ liệu người dùng ({_layout_reason}) / "
            f"Using per-user data ({_layout_reason}): {home}\n"
        )
    if sys.stdout is None:
        sys.stdout = _runtime_log
    if sys.stderr is None:
        sys.stderr = _runtime_log
    # Luôn tee stderr vào log kể cả khi stdout/stderr đã có (child process)
    import io as _io

    class _Tee(_io.TextIOBase):
        def __init__(self, *streams: object) -> None:
            self._s = streams

        def write(self, s: str) -> int:  # type: ignore[override]
            for st in self._s:
                try:
                    st.write(s)  # type: ignore[union-attr]
                except Exception:
                    pass
            return len(s)

        def flush(self) -> None:
            for st in self._s:
                try:
                    st.flush()  # type: ignore[union-attr]
                except Exception:
                    pass

    sys.stderr = _Tee(sys.stderr, _runtime_log)  # type: ignore[assignment]

if runtime_site.is_dir():
    sys.path.insert(0, str(runtime_site))
    # Không nhét nvidia/torch CUDA vào PATH của ZM AI TOOL.exe (WebView2).
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
set_desktop_path("ZM_AI_TOOL_BUNDLE", bundle)
try:
    from pipeline.core.runtime_site import prepend_windows_path, sanitize_process_environment

    # Sanitize the inherited Explorer/shell PATH before adding bundle tools.
    # A bloated PATH makes CreateProcess and torch's DLL bootstrap fail with
    # WinError 206 even when the torch/lib directory itself is short.
    sanitize_process_environment()
    prepend_windows_path(bundle)
except Exception:
    os.environ["PATH"] = os.pathsep.join((str(bundle), os.environ.get("PATH", "")))

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
        bundle / "package.json",
        Path(__file__).resolve().parents[1] / "package.json",
    ):
        try:
            v = json.loads(candidate.read_text(encoding="utf-8")).get("version")
            if isinstance(v, str) and v:
                return v
        except (OSError, ValueError, AttributeError):
            pass
    raise RuntimeError("Missing or invalid application package.json version")


APP_VERSION = app_version()
set_desktop_path("ZM_AI_TOOL_VERSION", APP_VERSION)

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
            return '"app":"zm_ai_tool"' in body.replace(" ", "")
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


def mark_update_ready(*_args: object) -> None:
    """Tell the detached updater that API + native webview initialization succeeded."""
    raw = os.environ.get("ZM_AI_TOOL_UPDATE_READY_FILE", "").strip()
    if not raw:
        return
    # ponytail: env var is set by launcher/CI only — no untrusted input, no path guard needed.
    try:
        Path(raw).write_text(APP_VERSION, encoding="utf-8")
    except OSError:
        traceback.print_exc()


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
    os.environ["ZM_AI_TOOL_PORT"] = str(port)
    base = api_base(port)

    config = uvicorn.Config(app, host=API_HOST, port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [api_socket]},
        name="zm_ai_tool-api",
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
                        f"<h2>Không mở được API / API failed to start</h2><p>{base}</p>"
                        f"<p>Log: {html.escape(str(home / 'app.log'))}</p>"
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
            window = webview.create_window(
                f"{APP_DISPLAY_NAME} v{APP_VERSION}",
                f"{base}/?v={APP_VERSION}",
                **win_kw,
            )
        except TypeError:
            # pywebview cũ có thể không hỗ trợ icon= hoặc text_select=.
            win_kw.pop("icon", None)
            win_kw.pop("text_select", None)
            window = webview.create_window(
                f"{APP_DISPLAY_NAME} v{APP_VERSION}",
                f"{base}/?v={APP_VERSION}",
                **win_kw,
            )
        try:
            from pipeline.core.desktop_window import register_restore_callback

            def _restore_desktop_window() -> None:
                # Auth completion must not unmaximize or unminimize the app.
                for method in ("focus",):
                    fn = getattr(window, method, None)
                    if callable(fn):
                        try:
                            fn()
                        except Exception:
                            pass

            register_restore_callback(_restore_desktop_window)
        except Exception:
            traceback.print_exc()
        # webview.start() chặn đến khi user đóng cửa sổ — không thoát vì lỗi job nền
        try:
            webview.start(mark_update_ready, gui="edgechromium", debug=False)
        except Exception:
            try:
                webview.start(mark_update_ready, debug=False)
            except Exception:
                traceback.print_exc()
                msg = (
                    "Không mở được cửa sổ: Windows đang chặn file tải xuống (MOTW) "
                    "hoặc thiếu .NET/WebView2. Hãy giải nén toàn bộ ZIP vào thư mục có quyền ghi "
                    "và chọn Properties → Unblock cho file ZIP. / Window startup failed: Windows "
                    "blocked downloaded files (MOTW), or .NET/WebView2 is missing. Extract the full "
                    "ZIP to a writable folder and select Properties → Unblock on the ZIP file."
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
