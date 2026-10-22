"""Read-only support diagnostics; never include environment dumps or credentials."""
from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import struct
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import DATA, PUBLIC_DATA, REPO_ROOT, SERVER_ROOT
from .media import _ff_bin, detect_device
from .runtime_active import active_runtime_dir, runtime_site


def _probe(read):
    # A missing driver, permission or optional package must not hide the rest.
    try:
        return read()
    except Exception:
        return None


def _command(args: list[str], timeout: float = 3) -> str | None:
    return _probe(lambda: subprocess.check_output(
        args, text=True, encoding="utf-8", errors="replace",
        stderr=subprocess.DEVNULL, timeout=timeout,
    ).strip())


def _machine() -> dict:
    system = platform.system()
    if system == "Windows":
        script = (
            "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
            "$c=Get-CimInstance Win32_ComputerSystem; "
            "$b=Get-CimInstance Win32_BIOS; "
            "[PSCustomObject]@{manufacturer=$c.Manufacturer;model=$c.Model;"
            "bios=$b.SMBIOSBIOSVersion;cpu=(@(Get-CimInstance Win32_Processor | "
            "Select-Object -ExpandProperty Name) -join ' / ')} | ConvertTo-Json -Compress"
        )
        return _probe(lambda: json.loads(_command(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], 6,
        ) or "{}")) or {}
    if system == "Darwin":
        return {
            "manufacturer": "Apple", "model": _command(["sysctl", "-n", "hw.model"]),
            "cpu": _command(["sysctl", "-n", "machdep.cpu.brand_string"]),
        }
    cpuinfo = _probe(lambda: Path("/proc/cpuinfo").read_text()) or ""
    return {
        **{key: _probe(lambda name=name: Path(f"/sys/class/dmi/id/{name}").read_text().strip())
           for key, name in (("manufacturer", "sys_vendor"), ("model", "product_name"), ("bios", "bios_version"))},
        "cpu": next((line.split(":", 1)[1].strip() for line in cpuinfo.splitlines()
                     if line.startswith(("model name", "Hardware")) and ":" in line), None),
    }


def system_info() -> dict:
    import psutil

    sections = []

    def section(id, rows, name=None):
        sections.append({"id": id, "name": name, "rows": [
            {"id": key, "value": value, **({"unit": unit[0]} if unit else {})}
            for key, value, *unit in rows
        ]})

    now = datetime.now(timezone.utc).isoformat()
    version = os.environ.get("ZM_AI_TOOL_VERSION") or _probe(
        lambda: json.loads((REPO_ROOT / "package.json").read_text())["version"]
    )
    section("app", [("version", version), ("packaged", bool(getattr(sys, "frozen", False))),
                    ("python", platform.python_version()), ("pythonBits", struct.calcsize("P") * 8),
                    ("executable", sys.executable)])
    machine = _machine()
    system = platform.system()
    os_name = f"macOS {platform.mac_ver()[0]}" if system == "Darwin" else platform.platform()
    if system == "Linux":
        os_name = (_probe(platform.freedesktop_os_release) or {}).get("PRETTY_NAME", os_name)
    boot = _probe(psutil.boot_time)
    section("system", [("os", os_name), ("kernel", platform.version()), ("arch", platform.machine()),
                       ("hostname", platform.node()), ("manufacturer", machine.get("manufacturer")),
                       ("model", machine.get("model")), ("bios", machine.get("bios")),
                       ("timezone", str(datetime.now().astimezone().tzinfo)),
                       ("uptime", max(0, time.time() - boot) if boot else None, "seconds")])
    freq = _probe(psutil.cpu_freq)
    section("cpu", [("model", machine.get("cpu") or platform.processor()),
                    ("physicalCores", _probe(lambda: psutil.cpu_count(logical=False))),
                    ("logicalCores", os.cpu_count()), ("usage", _probe(lambda: psutil.cpu_percent(interval=0.1)), "percent"),
                    ("frequency", freq.current if freq else None, "mhz"),
                    ("maxFrequency", freq.max if freq and freq.max > 0 else None, "mhz")])
    memory, swap = _probe(psutil.virtual_memory), _probe(psutil.swap_memory)
    section("memory", [("total", memory.total if memory else None, "bytes"),
                       ("available", memory.available if memory else None, "bytes"),
                       ("usage", memory.percent if memory else None, "percent"),
                       ("swapTotal", swap.total if swap else None, "bytes"),
                       ("swapUsed", swap.used if swap else None, "bytes")])
    device = _probe(detect_device)
    section("graphics", [("adapterCount", len(device.get("gpus") or []) if device else None),
                         ("acceleration", device.get("accel") if device else None)])
    device = device or {}
    for gpu in device.get("gpus") or []:
        section("gpu", [("model", gpu.get("name")), ("vendor", gpu.get("kind")),
                        ("vram", gpu["vramMb"] * 1024**2 if gpu.get("vramMb") is not None else None, "bytes"),
                        ("driver", gpu.get("driver")), ("acceleration", gpu.get("accel"))], gpu.get("index"))
    partitions = _probe(lambda: psutil.disk_partitions(all=False)) or []
    mounts = set()
    for partition in partitions:
        if partition.mountpoint in mounts:
            continue
        mounts.add(partition.mountpoint)
        usage = _probe(lambda: psutil.disk_usage(partition.mountpoint))
        section("disk", [("device", partition.device), ("mountpoint", partition.mountpoint), ("filesystem", partition.fstype),
                         ("total", usage.total if usage else None, "bytes"),
                         ("used", usage.used if usage else None, "bytes"),
                         ("free", usage.free if usage else None, "bytes"),
                         ("usage", usage.percent if usage else None, "percent")], partition.mountpoint)
    if not partitions:
        section("disk", [("total", None, "bytes")])
    section("paths", [("appFolder", str(SERVER_ROOT)), ("dataFolder", str(DATA)),
                      ("publicFolder", str(PUBLIC_DATA)), ("runtimeFolder", str(active_runtime_dir()))])
    section("tools", [])
    # Probe only version flags, without importing or loading any AI model.
    for name in ("ffmpeg", "ffprobe"):
        output = _command([_ff_bin(name), "-version"])
        sections[-1]["rows"].append({"id": name, "value": output.splitlines()[0] if output else None})
    for id, search in (("pythonPackages", {}), ("aiPackages", {"path": [str(runtime_site())]})):
        packages = _probe(lambda: sorted(
            {dist.metadata["Name"]: dist.version for dist in importlib.metadata.distributions(**search)
             if dist.metadata["Name"]}.items(), key=lambda item: item[0].casefold(),
        )) or []
        section(id, [("packageCount", len(packages))])
        sections[-1]["rows"].extend({"id": "package", "name": name, "value": value} for name, value in packages)
    return {"collectedAt": now, "sections": sections}
