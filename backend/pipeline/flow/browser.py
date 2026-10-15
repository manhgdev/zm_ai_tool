"""Desktop browser adapter for Flow.

Flow's upstream helper defaults to Playwright's bundled Chromium.  Desktop
packages do not ship that 200+ MB runtime, so use the user's installed Google
Chrome instead and keep the persistent account profile inside ZM AI TOOL.
"""
from __future__ import annotations

import os
import asyncio
import re
import socket
import subprocess
import threading
import sys
import signal
import shutil
import time
from pathlib import Path
from typing import Any

from pipeline.core.network import network_available


def _capture_active_window() -> Any:
    """Return an opaque token representing the current foreground window.

    macOS   -> {name, bundle} dict via System Events
    Windows -> HWND integer via ctypes user32
    Other   -> None (no-op)
    """
    # ponytail: stdlib-only on both platforms; no extra dependency.
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["osascript", "-e",
                 "tell application \"System Events\"\n"
                 "  set p to first process whose frontmost is true\n"
                 "  return (name of p) & \"||\" & (bundle identifier of p)\n"
                 "end tell"],
                capture_output=True, text=True, timeout=3,
            )
            raw = result.stdout.strip()
            if "||" in raw:
                pname, bundle = raw.split("||", 1)
                return {"name": pname.strip(), "bundle": bundle.strip()}
            return None
        except Exception:
            return None
    elif sys.platform == "win32":
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            return int(hwnd) if hwnd else None
        except Exception:
            return None
    return None


def _restore_active_window(token: Any) -> None:
    """Bring the previously captured window back to the foreground (best-effort)."""
    if not token:
        return
    if sys.platform == "darwin":
        name = token.get("name", "") if isinstance(token, dict) else ""
        bundle = token.get("bundle", "") if isinstance(token, dict) else str(token)
        # Primary: set frontmost directly via System Events — works for Electron
        # and any app that does not implement the AppleScript activate handler.
        restored = False
        if name:
            try:
                subprocess.run(
                    ["osascript", "-e",
                     f"tell application \"System Events\"\n"
                     f"  set frontmost of first process whose name is \"{name}\" to true\n"
                     f"end tell"],
                    capture_output=True, timeout=5,
                )
                restored = True
            except Exception:
                pass
        # Fallback: classic activate by bundle id
        if not restored and bundle:
            try:
                subprocess.run(
                    ["osascript", "-e", f'tell application id "{bundle}" to activate'],
                    capture_output=True, timeout=5,
                )
            except Exception:
                pass
    elif sys.platform == "win32":
        try:
            import ctypes
            hwnd = token
            # SW_RESTORE = 9: unminimise if needed, then bring to front.
            ctypes.windll.user32.ShowWindow(hwnd, 9)
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception:
            pass

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

FLOW_BASE_URL = "https://flow.google.com"
_profile_guard = threading.Lock()
_profile_locks: dict[str, Any] = {}
_visible_window_guard = threading.Lock()
_visible_launch_guard = threading.Lock()
_visible_window_index = 0
_profile_slots: dict[str, int] = {}


def _visible_window_geometry(
    index: int,
    screen_width: int,
    screen_height: int,
) -> tuple[int, int, int, int]:
    """Split screen into 2 columns, allow horizontal overlap, full height."""

    columns = 2
    width = min(screen_width, max(1080, screen_width // columns))
    height = screen_height

    # Even index: right; odd index: left
    column = index % columns

    if column == 0:
        x = screen_width - width
    else:
        x = 0

    y = 0

    return x, y, width, height



def _display_available() -> bool:
    """False on a Linux VPS with no window server; desktop always has one."""
    if sys.platform.startswith("linux"):
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return True


def _visible_window_args(*, fullscreen: bool = False) -> list[str]:
    """Start compactly; the page then supplies the real monitor work area."""
    # Do not create Tk/AppKit from a Flow worker thread.  macOS aborts the
    # process when Tk initializes Cocoa off the main thread (SIGABRT/-6).
    return [
        "--window-position=0,0",
        *(["--start-maximized"] if fullscreen else ["--window-size=1080,800"]),
    ]


def _chrome_major_version(executable: Path) -> str:
    """Major Chrome version without launching a browser process.

    Spawning the branded Chrome binary while the user's Chrome is already
    open prints "Opening in existing browser session" / "Mở trong phiên
    trình duyệt hiện tại" and steals focus to the real profile picker.
    Read version metadata from disk only.
    """
    if sys.platform == "darwin":
        # .../Google Chrome.app/Contents/MacOS/Google Chrome → Contents/Info.plist
        try:
            import plistlib
            plist_path = executable.resolve().parents[1] / "Info.plist"
            with plist_path.open("rb") as handle:
                info = plistlib.load(handle)
            match = re.search(
                r"(\d+)\.",
                str(info.get("CFBundleShortVersionString") or info.get("KSVersion") or ""),
            )
            if match:
                return f"{match.group(1)}.0.0.0"
        except Exception:
            pass
    if sys.platform == "win32":
        try:
            import winreg
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(hive, r"Software\Google\Chrome\BLBeacon") as key:
                        value, _ = winreg.QueryValueEx(key, "version")
                except OSError:
                    continue
                match = re.search(r"(\d+)\.", str(value or ""))
                if match:
                    return f"{match.group(1)}.0.0.0"
        except ImportError:
            pass
    # Never spawn the Chrome binary here: branded Chrome hands off to the
    # user's open session ("Opening in existing browser session").
    return "155.0.0.0"


def profile_lock(path: Path):
    key = os.path.normcase(str(path.resolve()))
    with _profile_guard:
        return _profile_locks.setdefault(key, threading.Lock())


def chrome_profile_running(profile_dir: Path) -> bool:
    """Check the exact owned profile, including Chrome left after a window closes."""
    import psutil

    argument = f"--user-data-dir={Path(profile_dir).resolve()}"
    for process in psutil.process_iter(["pid"]):
        try:
            if argument in (process.cmdline() or []):
                return True
        except (psutil.Error, OSError, SystemError):
            continue
    return False


def _windows_chrome_user_data_dir_policy() -> str | None:
    """Return Google Chrome's enterprise UserDataDir policy when set.

    That policy overrides ``--user-data-dir``, so launching branded
    ``chrome.exe`` would open the user's real Chrome profiles / picker.
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\Policies\Google\Chrome") as key:
                value, _ = winreg.QueryValueEx(key, "UserDataDir")
        except OSError:
            continue
        text = str(value or "").strip()
        if text:
            return text
    return None


def _default_chrome_user_data_dir() -> Path | None:
    """OS default Google Chrome user-data directory (parent of Default)."""
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Google/Chrome"
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData/Local")
        return Path(local) / "Google/Chrome/User Data"
    return Path.home() / ".config/google-chrome"


def _is_google_chrome_binary(path: Path) -> bool:
    """True for branded Google Chrome installs (subject to Chrome policies)."""
    text = os.path.normcase(str(path)).casefold()
    if "chrome for testing" in text or "chromium" in text or "ms-playwright" in text:
        return False
    return "google" in text and "chrome" in text


def _playwright_chrome_candidates() -> list[Path]:
    """Automation Chromium builds that ignore Google Chrome UserDataDir policy."""
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
        cache = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or local / "ms-playwright")
        found: list[Path] = []
        found.extend(sorted(cache.glob("chromium-*/chrome-win64/chrome.exe"), reverse=True))
        found.extend(sorted(cache.glob("chromium-*/chrome-win/chrome.exe"), reverse=True))
        cft = local / "Google/Chrome for Testing/Application/chrome.exe"
        if cft.is_file():
            found.insert(0, cft)
        chromium = local / "Chromium/Application/chrome.exe"
        if chromium.is_file():
            found.insert(0, chromium)
        return found
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    puppeteer_cache = Path(os.environ.get("PUPPETEER_CACHE_DIR") or cache / "puppeteer")
    playwright_cache = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or cache / "ms-playwright")
    found = list(sorted(puppeteer_cache.glob("chrome/linux-*/chrome-linux64/chrome"), reverse=True))
    found.extend(sorted(playwright_cache.glob("chromium-*/chrome-linux/chrome"), reverse=True))
    found.extend(sorted(playwright_cache.glob("chromium-*/chrome-linux64/chrome"), reverse=True))
    return found


def chrome_executable() -> Path | None:
    """Locate Chrome from system installs, PATH, or known automation caches."""
    candidates: list[Path]
    if sys.platform == "darwin":
        candidates = [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")]
    elif sys.platform == "win32":
        roots = [Path(os.environ.get("PROGRAMFILES", "")), Path(os.environ.get("PROGRAMFILES(X86)", "")), Path(os.environ.get("LOCALAPPDATA", ""))]
        google = [root / "Google/Chrome/Application/chrome.exe" for root in roots if str(root)]
        # Branded Chrome honors enterprise UserDataDir and would open the
        # user's real profiles; prefer Chromium / Chrome for Testing then.
        automation = _playwright_chrome_candidates()
        if _windows_chrome_user_data_dir_policy():
            candidates = [*automation, *google]
        else:
            candidates = [*google, *automation]
    else:
        candidates = [Path("/usr/bin/google-chrome"), Path("/usr/bin/google-chrome-stable"), Path("/opt/google/chrome/chrome"), Path("/usr/bin/chromium"), Path("/usr/bin/chromium-browser")]
        candidates.extend(Path(found) for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser") if (found := shutil.which(name)))
        candidates.extend(_playwright_chrome_candidates())
    return next((path for path in candidates if path.is_file() and os.access(path, os.X_OK)), None)


def free_local_port() -> int:
    """Reserve a localhost port number for a visible manual session."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _cdp_endpoint_ready(cdp_url: str) -> bool:
    """True when Chrome's remote-debugging HTTP endpoint answers."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"{cdp_url.rstrip('/')}/json/version", timeout=1.0) as resp:
            return int(getattr(resp, "status", 200) or 200) < 400
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


async def _wait_cdp_endpoint(cdp_url: str, *, timeout_s: float = 30.0) -> None:
    """Block until a spawned Chrome exposes its CDP HTTP endpoint."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await asyncio.to_thread(_cdp_endpoint_ready, cdp_url):
            return
        await asyncio.sleep(0.15)
    raise RuntimeError(
        f"FLOW_CDP_TIMEOUT: Chrome remote debugging did not open at {cdp_url}"
    )


async def _goto_tolerant(page, url: str) -> None:
    """goto() that accepts being superseded by a parallel navigation to the same page."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=15_000)
    except Exception as exc:
        # ponytail: another job navigating the shared tab is harmless; anything else re-raises.
        if "interrupted by another navigation" not in str(exc) and "ERR_ABORTED" not in str(exc):
            raise
        await asyncio.sleep(1.5)

class BrowserManager:
    """Subset of flow-py's browser contract backed by installed Google Chrome."""

    def __init__(self, *, headless: bool = True, profile_dir: Path | None = None, slow_mo: int = 0, google_login_compat: bool = False, fullscreen: bool = False, page_zoom: float = 1.0, cdp_url: str | None = None, remote_debugging_port: int | None = None, restore_last_session: bool = False) -> None:
        self.cdp_url = cdp_url  # flow-py checks this optional upstream attribute.
        # ``headless`` / ``_chromeless``: keep the user's selected mode. Linux
        # without a display necessarily falls back to true headless Chrome.
        no_display = not _display_available()
        self.headless = bool(headless) or no_display
        self._chromeless = self.headless
        # Absolute path: relative dirs resolve against chrome.exe cwd on Windows
        # and can fall through to the user's real Chrome User Data.
        self.profile_dir = Path(profile_dir or Path.cwd() / ".flow-profile").expanduser()
        try:
            self.profile_dir = self.profile_dir.resolve()
        except OSError:
            self.profile_dir = self.profile_dir.absolute()
        self.slow_mo = slow_mo
        self.google_login_compat = google_login_compat
        self.fullscreen = fullscreen
        self.page_zoom = page_zoom
        self.remote_debugging_port = remote_debugging_port
        self.restore_last_session = restore_last_session
        self._pw: Playwright | None = None
        self._ctx: BrowserContext | None = None
        self._cdp_browser: Any = None
        self._page: Page | None = None
        self._owns_page = False
        self._zoom_applied = False
        self._window_placed = False
        self._profile_lock = profile_lock(self.profile_dir)
        self._owns_profile = False
        # True when this manager spawned Chrome and must reap it on stop/kill.
        # External from_cdp() attach leaves this False so user Chrome survives.
        self._owns_chrome_process = False
        self._chrome_proc: subprocess.Popen[Any] | None = None
        # ponytail: captured only for on-screen windows; restored in stop().
        self._prior_app: Any = None

    @classmethod
    def from_cdp(cls, cdp_url: str, *, new_page: bool = False) -> "BrowserManager":
        # CDP attaches to the already visible Chrome profile used by Flow.
        # Keep this distinct from headless CDP consumers so page zoom/layout
        # adjustments run on the real account tab.
        manager = cls(headless=False, cdp_url=cdp_url, page_zoom=1.0)
        manager._owns_page = new_page
        manager._window_placed = True  # owner already placed it; re-placing makes it jump
        return manager

    def _spawn_chrome_argv(self, executable: Path, port: int) -> list[str]:
        """Minimal real-Chrome argv (no Playwright launch wrapper)."""
        args = [
            str(executable),
            f"--user-data-dir={self.profile_dir}",
            "--profile-directory=Default",
            f"--remote-debugging-port={int(port)}",
            "--remote-debugging-address=127.0.0.1",
            "--lang=en-US",
            "--disable-extensions",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if self.restore_last_session:
            args.append("--restore-last-session")
        if sys.platform.startswith("linux") and getattr(os, "geteuid", lambda: -1)() == 0:
            args.append("--no-sandbox")
        if self._chromeless:
            args.extend(["--headless=new", "--window-size=1080,800"])
        else:
            args.extend(_visible_window_args(fullscreen=self.fullscreen))
        return args

    async def _attach_cdp_context(self, cdp_url: str) -> None:
        self._pw = await asyncio.wait_for(async_playwright().start(), timeout=30)
        self._cdp_browser = await self._pw.chromium.connect_over_cdp(cdp_url)
        contexts = self._cdp_browser.contexts
        if not contexts:
            raise RuntimeError("FLOW_CDP_NO_CONTEXT: Existing Chrome has no browser context")
        self._ctx = contexts[0]

    async def _start_spawned_chrome(self, executable: Path) -> None:
        """Spawn branded Chrome ourselves, then attach Playwright over CDP.

        Avoids Playwright ``launch_persistent_context`` (heavy automation
        fingerprint). Connect / cookie login / manual sessions use this path.
        """
        port = int(self.remote_debugging_port or free_local_port())
        self.remote_debugging_port = port
        self.cdp_url = f"http://127.0.0.1:{port}"
        argv = self._spawn_chrome_argv(executable, port)
        popen_kwargs: dict[str, Any] = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            # New process group so kill_now can signal the tree; keep the
            # window visible for headed cookie/connect sessions.
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        self._chrome_proc = subprocess.Popen(argv, **popen_kwargs)
        self._owns_chrome_process = True
        try:
            await _wait_cdp_endpoint(self.cdp_url, timeout_s=45.0)
            await self._attach_cdp_context(self.cdp_url)
        except BaseException:
            self.kill_now()
            self._owns_chrome_process = False
            self._chrome_proc = None
            raise

    async def start(self) -> "BrowserManager":
        if self.cdp_url and not self.remote_debugging_port:
            # Pure attach to an already-running Chrome (from_cdp / shared session).
            if not await asyncio.to_thread(network_available):
                raise RuntimeError('FLOW_NETWORK_UNAVAILABLE: Internet connection is unavailable')
            try:
                await self._attach_cdp_context(self.cdp_url)
                return self
            except BaseException:
                await self.stop()
                raise
        executable = chrome_executable()
        if executable is None:
            raise RuntimeError("FLOW_CHROME_REQUIRED: Google Chrome was not found. Install Google Chrome, then connect again.")
        policy_dir = _windows_chrome_user_data_dir_policy()
        if policy_dir and _is_google_chrome_binary(executable):
            # Policy wins over --user-data-dir → real Chrome profiles / picker.
            raise RuntimeError(
                "FLOW_CHROME_POLICY_USER_DATA_DIR: Windows Google Chrome policy "
                "UserDataDir is set, so Flow cannot isolate its profile. Install "
                "Chromium or Chrome for Testing, or clear the policy, then retry."
            )
        chrome_home = _default_chrome_user_data_dir()
        try:
            profile_resolved = self.profile_dir.resolve()
        except OSError:
            profile_resolved = self.profile_dir
        if chrome_home is not None:
            try:
                chrome_home = chrome_home.resolve()
            except OSError:
                pass
            if profile_resolved == chrome_home or chrome_home in profile_resolved.parents:
                raise RuntimeError(
                    "FLOW_CHROME_PROFILE_FORBIDDEN: Refusing to use the system "
                    "Google Chrome User Data directory as a Flow profile"
                )
        # All callers (jobs, login, credit sync, project recovery) pass here.
        # Workers own the cancellable wait; synchronous API calls fail promptly.
        if not await asyncio.to_thread(network_available):
            raise RuntimeError('FLOW_NETWORK_UNAVAILABLE: Internet connection is unavailable')
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        while not self._profile_lock.acquire(blocking=False):
            await asyncio.sleep(0.1)
        self._owns_profile = True
        visible_launch_owned = False
        show_window = not self.headless
        if show_window:
            self._prior_app = _capture_active_window()
            while not _visible_launch_guard.acquire(blocking=False):
                await asyncio.sleep(0.05)
            visible_launch_owned = True
        try:
            # Connect, manual sessions and workers use native Chrome + CDP.
            if self.remote_debugging_port:
                await asyncio.wait_for(self._start_spawned_chrome(executable), timeout=60)
                if visible_launch_owned:
                    _visible_launch_guard.release()
                    visible_launch_owned = False
                return self

            self._pw = await asyncio.wait_for(async_playwright().start(), timeout=30)
            window_args = _visible_window_args(fullscreen=self.fullscreen) if show_window else []
            # Force Default inside Flow's user-data-dir so Windows never shows
            # the system "Who's using Chrome?" picker for a multi-profile store.
            launch_args = [
                "--lang=en-US",
                "--disable-extensions",
                "--no-first-run",
                "--no-default-browser-check",
                "--profile-directory=Default",
            ]
            # Imported Cookie headers have no expiry metadata. Restore the
            # native session so Chrome keeps these session cookies on reopen.
            if self.restore_last_session:
                launch_args.append("--restore-last-session")
            launch_args.extend(window_args)
            # Linux root cannot use Chromium's sandbox. Keep it enabled for all
            # non-root sessions; do not retry arbitrary failures unsandboxed.
            sandbox = not (sys.platform.startswith("linux") and getattr(os, "geteuid", lambda: -1)() == 0)
            launch_kwargs: dict[str, Any] = {
                "executable_path": str(executable),
                # Login and CDP reopen the same profile. Playwright's mock
                # keychain/basic password store cannot decrypt native Chrome
                # cookies (or vice versa), so keep the platform store in both.
                "ignore_default_args": ["--use-mock-keychain", "--password-store=basic"],
                "headless": self._chromeless,
                "slow_mo": self.slow_mo,
                "accept_downloads": True,
                "locale": "en-US",
                "extra_http_headers": {"Accept-Language": "en-US,en;q=0.9"},
                "chromium_sandbox": sandbox,
                "args": launch_args,
            }
            if self._chromeless:
                # Keep a stable viewport for request/layout calculations. The
                # installed Chrome's native headless UA is left untouched.
                launch_kwargs["viewport"] = {"width": 1080, "height": 800}
            else:
                launch_kwargs["viewport"] = None
                launch_kwargs["no_viewport"] = True
            self._ctx = await asyncio.wait_for(
                self._pw.chromium.launch_persistent_context(
                    str(self.profile_dir),
                    **launch_kwargs,
                ),
                timeout=60,
            )
            if window_args and visible_launch_owned:
                _visible_launch_guard.release()
                visible_launch_owned = False
            # Observe requests through UIInterceptor; do not replace page APIs.
        except asyncio.TimeoutError as exc:
            await self.stop()
            raise RuntimeError(
                "FLOW_BROWSER_START_TIMEOUT: Google Chrome did not start within 60 seconds"
            ) from exc
        except BaseException:
            await self.stop()
            raise
        finally:
            if visible_launch_owned:
                _visible_launch_guard.release()
        return self

    async def stop(self) -> None:
        # External CDP attach: disconnect Playwright only — never close the
        # user's Chrome. Spawned Chrome (connect/login) is reaped below.
        if self._owns_page and self._page:
            try:
                await self._page.close()
            except Exception:
                pass
        owned = self._owns_chrome_process
        proc = self._chrome_proc
        if owned and self._cdp_browser is not None:
            # Ask Chrome to quit so Cookie DB / Login Data flush before kill.
            try:
                await asyncio.wait_for(self._cdp_browser.close(), timeout=5.0)
            except Exception:
                pass
            self._cdp_browser = None
            if proc is not None and proc.poll() is None:
                try:
                    await asyncio.to_thread(proc.wait, 5.0)
                except Exception:
                    pass
        elif self.cdp_url and self._cdp_browser:
            self._cdp_browser = None
        elif self._ctx:
            try:
                await self._ctx.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
        self._ctx = None
        self._cdp_browser = None
        self._pw = None
        self._page = None
        if owned:
            try:
                await asyncio.to_thread(self.kill_now)
            except Exception:
                pass
            self._owns_chrome_process = False
            self._chrome_proc = None
        if self._owns_profile:
            self._owns_profile = False
            self._profile_lock.release()
        if not self.headless and self._prior_app:
            _restore_active_window(self._prior_app)
            self._prior_app = None

    def kill_now(self) -> None:
        """Synchronously terminate Chrome processes owned by this profile.

        Cancellation can arrive from the API thread while Playwright is
        blocked in another event loop, so awaiting ``stop()`` is not enough.
        The runtime profile is unique per job, making the command-line match
        safe and preventing unrelated Chrome accounts from being killed.
        """
        proc = self._chrome_proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except OSError:
                pass
        self._chrome_proc = None
        profile = str(self.profile_dir.resolve())
        matched = False
        try:
            import psutil  # type: ignore
            # Request only pid metadata during iteration. On macOS, asking
            # psutil for cmdline in process_iter can raise PermissionError or
            # SystemError from KERN_PROCARGS2 for unrelated protected PIDs.
            for candidate in psutil.process_iter(["pid"]):
                try:
                    if candidate.pid == os.getpid():
                        continue
                    if f"--user-data-dir={profile}" not in (candidate.cmdline() or []):
                        continue
                    matched = True
                    candidate.send_signal(signal.SIGTERM)
                    try:
                        candidate.wait(timeout=0.5)
                    except psutil.TimeoutExpired:
                        candidate.kill()
                except (psutil.Error, OSError, PermissionError, SystemError):
                    continue
        except (ImportError, psutil.Error, OSError, PermissionError, SystemError):
            pass
        # macOS can deny cmdline inspection for Chrome helper processes. The
        # profile path is unique per job, so pkill's exact command-line match
        # is a safe fallback for those otherwise orphaned helpers.
        if sys.platform != "win32" and not matched:
            try:
                pattern = rf"--user-data-dir={re.escape(profile)}(\s|$)"
                subprocess.run(["pkill", "-TERM", "-f", "--", pattern], check=False, timeout=2)
                time.sleep(0.2)
                subprocess.run(["pkill", "-KILL", "-f", "--", pattern], check=False, timeout=2)
            except (OSError, subprocess.SubprocessError):
                pass

    @property
    def context(self) -> BrowserContext:
        if not self._ctx:
            raise RuntimeError("BrowserManager not started")
        return self._ctx

    async def _finish_page(self, page: Page, *, bring_front: bool = False) -> Page:
        """Place/zoom after the tab is ready (visible jobs only)."""
        if not self.headless:
            if not self._window_placed:
                await self._place_visible_window(page)
            if bring_front:
                await page.bring_to_front()
            await self._apply_visible_zoom(page)
        return page

    async def page(self) -> Page:
        if self._page and not self._page.is_closed():
            if not self.headless:
                await self._apply_visible_zoom(self._page)
            return self._page
        pages = self.context.pages
        # Reuse one initial blank page and close only extra blank tabs created
        # by the profile. Never leave about:blank tabs visible to the user.
        blank_pages = [candidate for candidate in pages if candidate.url in {"", "about:blank"}]
        if len(blank_pages) > 1:
            for extra in blank_pages[1:]:
                try:
                    await extra.close()
                except Exception:
                    pass
            pages = self.context.pages
        if self.cdp_url:
            if self._owns_page:
                self._page = next((candidate for candidate in pages if candidate.url in {"", "about:blank"}), None) or await self.context.new_page()
                if self._page.url in {"", "about:blank"}:
                    await _goto_tolerant(self._page, FLOW_BASE_URL)
                return await self._finish_page(self._page)
            for candidate in pages:
                if "flow.google.com" in candidate.url:
                    self._page = candidate
                    for extra in list(self.context.pages):
                        if extra is candidate or extra.url not in {"", "about:blank"}:
                            continue
                        try:
                            await extra.close()
                        except Exception:
                            pass
                    return await self._finish_page(candidate, bring_front=True)
        # Prefer an existing Flow tab. Chrome profiles often start with a
        # blank tab beside it; close those blanks instead of exposing them.
        self._page = next((candidate for candidate in pages
                           if "flow.google.com" in candidate.url), None)
        if self._page is None:
            self._page = pages[0] if pages else await self.context.new_page()
        for extra in list(self.context.pages):
            if extra is self._page or extra.url not in {"", "about:blank"}:
                continue
            try:
                await extra.close()
            except Exception:
                pass
        if self._page.url in {"", "about:blank"}:
            await _goto_tolerant(self._page, FLOW_BASE_URL)
        for extra in list(self.context.pages):
            if extra is not self._page and extra.url in {"", "about:blank"}:
                try:
                    await extra.close()
                except Exception:
                    pass
        return await self._finish_page(self._page, bring_front=True)

    async def _place_visible_window(self, page: Page) -> None:
        if self.fullscreen:
            session = await self.context.new_cdp_session(page)
            try:
                window_id = (await session.send('Browser.getWindowForTarget'))['windowId']
                await session.send('Browser.setWindowBounds', {'windowId': window_id, 'bounds': {'windowState': 'normal'}})
                try:
                    area = await page.evaluate("() => ({l: screen.availLeft || 0, t: screen.availTop || 0, w: screen.availWidth, h: screen.availHeight})")
                    await session.send('Browser.setWindowBounds', {'windowId': window_id, 'bounds': {
                        'left': int(area['l']), 'top': int(area['t']), 'width': int(area['w']), 'height': int(area['h'])}})
                except Exception:
                    await session.send('Browser.setWindowBounds', {'windowId': window_id, 'bounds': {'windowState': 'maximized'}})
                self._window_placed = True
            finally:
                await session.detach()
            return
        global _visible_window_index
        area = await page.evaluate("""() => ({
            left: screen.availLeft || 0, top: screen.availTop || 0,
            // Use the physical screen height. macOS can report a reduced
            // availHeight when another app owns the menu/dock area, which
            // made Flow windows appear as short floating rectangles.
            width: screen.width, height: screen.height * (window.devicePixelRatio || 1)
        })""")
        with _visible_window_guard:
            key = str(self.profile_dir)
            if key not in _profile_slots:
                _profile_slots[key] = _visible_window_index
                _visible_window_index += 1
            index = _profile_slots[key]
        x, y, width, height = _visible_window_geometry(index, area['width'], area['height'])
        new_cdp_session = getattr(self.context, "new_cdp_session", None)
        if new_cdp_session is None:
            # Lightweight test doubles and non Chromium contexts may not
            # expose CDP; keep the visible fallback local to the document.
            await page.evaluate("(scale) => { document.documentElement.style.zoom = String(scale); }", self.page_zoom)
            self._zoom_applied = True
            return
        session = await new_cdp_session(page)
        try:
            window = await session.send('Browser.getWindowForTarget')
            window_id = window['windowId']
            await session.send('Browser.setWindowBounds', {
                'windowId': window_id, 'bounds': {'windowState': 'normal'},
            })
            await session.send('Browser.setWindowBounds', {
                'windowId': window_id, 'bounds': {
                    'left': area['left'] + x, 'top': area['top'] + y,
                    'width': width, 'height': height,
                },
            })
            # Chrome may enforce a larger native minimum; clamp its actual frame.
            actual = (await session.send('Browser.getWindowBounds', {'windowId': window_id}))['bounds']
            await session.send('Browser.setWindowBounds', {
                'windowId': window_id, 'bounds': {
                    'left': area['left'] + max(0, min(x, area['width'] - actual['width'])),
                    'top': area['top'] + max(0, min(y, area['height'] - actual['height'])),
                },
            })
            self._window_placed = True
        finally:
            await session.detach()

    async def _apply_visible_zoom(self, page: Page) -> None:
        if self.page_zoom == 1.0:
            return
        # Keep the profile and Chrome settings untouched.  Applying the scale
        # only to the active tab avoids opening chrome://settings, which can
        # interfere with Flow's anti-automation checks.
        new_cdp_session = getattr(self.context, "new_cdp_session", None)
        if new_cdp_session is None:
            evaluate = getattr(page, "evaluate", None)
            if evaluate is not None:
                await evaluate("(scale) => { document.documentElement.style.zoom = String(scale); }", self.page_zoom)
            self._zoom_applied = True
            return
        # CSS zoom changes the actual desktop layout and is visible in Flow's
        # narrow/wide responsive surfaces; pageScaleFactor only simulates pinch
        # zoom and leaves the page layout effectively at 100%.
        evaluate = getattr(page, "evaluate", None)
        if evaluate is not None:
            await evaluate("(scale) => { document.documentElement.style.zoom = String(scale); }", self.page_zoom)
        self._zoom_applied = True
