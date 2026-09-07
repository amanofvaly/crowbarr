"""Cheap host/cgroup telemetry; inference is admitted only when headroom exists."""

from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path


def snapshot() -> dict:
    result = {
        "ram_available_mb": None,
        "vram_free_mb": None,
        "vram_total_mb": None,
        "cpu_load": os.getloadavg()[0] / max(1, os.cpu_count() or 1),
        "gpu": None,
    }
    try:
        memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        available = int(memory["MemAvailable"].split()[0]) / 1024
        result["ram_available_without_arc_mb"] = round(available)
        try:
            arc = {
                parts[0]: int(parts[2])
                for line in Path("/proc/spl/kstat/zfs/arcstats").read_text().splitlines()[2:]
                if len(parts := line.split()) >= 3
            }
            reclaimable = max(0, arc.get("size", 0) - arc.get("c_min", 0)) / 1024**2
            result["zfs_arc_reclaimable_mb"] = round(reclaimable)
            available += reclaimable
        except (OSError, ValueError):
            result["zfs_arc_reclaimable_mb"] = 0
        limit = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        if limit != "max":
            used = int(Path("/sys/fs/cgroup/memory.current").read_text())
            available = min(available, (int(limit) - used) / 1024**2)
        result["ram_available_mb"] = round(available)
    except (OSError, ValueError, KeyError):
        pass
    try:
        output = (
            subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.free,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=3,
                check=True,
            )
            .stdout.splitlines()[0]
            .split(",")
        )
        result.update(gpu=output[0].strip(), vram_free_mb=int(output[1]), vram_total_mb=int(output[2]))
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass
    return result


def gate(settings, metrics: dict) -> str:
    ram = metrics["ram_available_mb"]
    if ram is not None and ram < settings.min_free_ram_mb:
        return f"Waiting for {settings.min_free_ram_mb} MB available RAM ({ram} MB available)"
    if (
        settings.device == "cuda"
        and metrics["vram_free_mb"] is not None
        and metrics["vram_free_mb"] < settings.min_free_vram_mb
    ):
        return f"Waiting for {settings.min_free_vram_mb} MB free GPU memory"
    return ""


def quiet(settings) -> bool:
    start, end = settings.quiet_hour_start, settings.quiet_hour_end
    hour = datetime.now().hour
    if start == end:
        return False
    return start <= hour < end if start < end else hour >= start or hour < end
