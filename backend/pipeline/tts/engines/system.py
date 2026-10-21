"""System TTS — Windows SAPI / macOS say / Linux espeak-ng."""
from __future__ import annotations

import json
import platform
import re
import subprocess
from pathlib import Path

PREFIX_WIN = "win:"


def _windows_voices() -> list[dict[str, str]]:
    """List installed SAPI voices via System.Speech (PowerShell)."""
    if platform.system() != "Windows":
        return []
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.GetInstalledVoices() | ForEach-Object { "
        "$i = $_.VoiceInfo; "
        "Write-Output ($i.Name + '|' + $i.Culture + '|' + $i.Gender) "
        "}"
    )
    try:
        raw = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", ps],
            text=True,
            timeout=12,
            stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 2:
            continue
        name, culture = parts[0].strip(), parts[1].strip()
        gender = parts[2].strip().lower() if len(parts) > 2 else ""
        if not name or name in seen:
            continue
        seen.add(name)
        gender_key = "female" if "female" in gender else "male" if "male" in gender else ""
        # short code only (en, vi) — same as zmAI / CapCut list
        lang_code = (culture or "").replace("_", "-").split("-", 1)[0].lower()
        out.append(
            {
                "id": f"{PREFIX_WIN}{name}",
                "name": f"Windows · {name}",
                "engine": "system",
                "type": "system",
                "description": f"Giọng hệ thống Windows · {culture or 'local'}",
                "language": lang_code,
                "gender": gender_key,
            }
        )
    return out


def _parse_say_voices() -> list[tuple[str, str, str]]:
    if platform.system() != "Darwin":
        return []
    try:
        raw = subprocess.check_output(["say", "-v", "?"], text=True, timeout=5)
    except (FileNotFoundError, subprocess.SubprocessError):
        return []
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        left = line.split("#", 1)[0].rstrip()
        m = re.search(r"\s([a-z]{2}_[A-Z]{2})\s*$", left)
        if not m:
            continue
        locale = m.group(1)
        say_id = left[: m.start()].strip().split(" (", 1)[0].strip()
        if not say_id or say_id in seen:
            continue
        seen.add(say_id)
        out.append((say_id, locale, f"{say_id} ({locale})"))
    return out


def list_voices(lang: str | None = None) -> list[dict[str, str]]:
    prefer = (lang or "vi").split("-")[0].lower()
    voices: list[dict[str, str]] = [
        {
            "id": "system",
            "name": "Giọng hệ thống (theo ngôn ngữ đích)",
            "engine": "system",
            "type": "system",
            "description": "Giọng mặc định của hệ điều hành theo ngôn ngữ đích.",
            "language": lang or "auto",
        }
    ]
    if platform.system() == "Windows":
        win = _windows_voices()
        win.sort(
            key=lambda v: (
                0 if str(v.get("language") or "").lower().startswith(prefer) else 1,
                v.get("name") or "",
            )
        )
        voices.extend(win)
        return voices

    if platform.system() == "Darwin":
        rows = _parse_say_voices()
        rows.sort(key=lambda x: (0 if x[1].lower().startswith(prefer) else 1, x[0]))
        for say_id, locale, label in rows:
            if locale.startswith(("vi", "en", "zh", "ja", "ko")):
                lang_code = locale.replace("_", "-").split("-", 1)[0].lower()
                voices.append(
                    {
                        "id": say_id,
                        "name": f"macOS · {label}",
                        "engine": "system",
                        "type": "system",
                        "description": f"Giọng hệ thống macOS · {locale}",
                        "language": lang_code,
                    }
                )
        return voices

    # Linux: espeak-ng voice codes commonly available
    for code, label in (
        ("vi", "Tiếng Việt"),
        ("en", "English"),
        ("zh", "Chinese"),
        ("ja", "Japanese"),
        ("ko", "Korean"),
    ):
        voices.append(
            {
                "id": f"espeak:{code}",
                "name": f"espeak · {label}",
                "engine": "system",
                "type": "system",
                "description": f"espeak-ng · {code}",
                "language": code,
            }
        )
    return voices


def _windows_synthesize(text: str, voice_name: str, out_wav: Path) -> None:
    # PowerShell also parses smart quotes as delimiters. Keep all data on stdin.
    ps = (
        "$ErrorActionPreference = 'Stop'; "
        "$request = [Console]::In.ReadToEnd() | ConvertFrom-Json; "
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "try { "
        "try { $s.SelectVoice($request.voice) } catch { }; "
        "$s.SetOutputToWaveFile($request.output); "
        "$s.Speak($request.text) "
        "} finally { $s.Dispose() }"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
        input=json.dumps({"text": text or ".", "voice": voice_name, "output": str(out_wav.resolve())}),
        text=True,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=120,
    )


def synthesize(text: str, voice: str, out_wav: Path, lang: str = "vi") -> None:
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    system = platform.system()

    if system == "Windows":
        name = voice
        if voice.startswith(PREFIX_WIN):
            name = voice[len(PREFIX_WIN) :]
        elif voice == "system" or not voice:
            # Prefer culture matching lang, else first installed
            prefer = (lang or "vi").split("-")[0].lower()
            win = _windows_voices()
            pick = next(
                (v for v in win if str(v.get("language") or "").lower().startswith(prefer)),
                win[0] if win else None,
            )
            name = (pick["id"][len(PREFIX_WIN) :] if pick else "Microsoft Zira Desktop")
        _windows_synthesize(text, name, out_wav)
        return

    if system == "Darwin":
        resolved = voice if voice and voice != "system" else "Samantha"
        tmp = out_wav.with_suffix(".aiff")
        subprocess.check_call(
            ["say", "-v", resolved, "-o", str(tmp), text or "."],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.check_call(
            ["afconvert", "-f", "WAVE", "-d", "LEI16", str(tmp), str(out_wav)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        tmp.unlink(missing_ok=True)
        return

    # Linux espeak-ng
    if voice.startswith("espeak:"):
        v = voice.split(":", 1)[1] or "en"
    elif voice and voice != "system":
        v = voice
    else:
        v = "vi" if (lang or "").startswith("vi") else "en"
    subprocess.check_call(
        ["espeak-ng", "-v", v, "-w", str(out_wav), text or "."],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
