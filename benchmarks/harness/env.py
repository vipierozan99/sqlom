"""Machine + git + package-version capture (PLAN.md §6 `env` block, §4's
closing invariant: "absolutes drift with the machine"). `docs/METHODOLOGY.md`
records a run coming back ~1.35x slower for every contender after a machine
change — recording the machine is how a later reader tells "the code changed"
from "the box changed".
"""

from __future__ import annotations

import importlib.metadata
import platform
import re
import subprocess
import sys
from pathlib import Path

from benchmarks.harness.affinity import cpu_topology

_PACKAGES = (
    "rowform", "sqlalchemy", "asyncpg", "psycopg", "orjson", "fastapi",
    "uvicorn", "uvloop", "aiosqlite", "locust", "httptools",
)


def _read(path) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _cpu_model() -> str:
    text = _read("/proc/cpuinfo") or ""
    match = re.search(r"^model name\s*:\s*(.+)$", text, re.MULTILINE)
    return match.group(1) if match else (platform.processor() or "unknown")


def _governors() -> list[str]:
    values = set()
    for p in Path("/sys/devices/system/cpu").glob("cpu*/cpufreq/scaling_governor"):
        text = _read(p)
        if text:
            values.add(text)
    return sorted(values)


def _frequencies_mhz() -> list[float]:
    values = []
    for p in sorted(Path("/sys/devices/system/cpu").glob("cpu*/cpufreq/scaling_cur_freq")):
        text = _read(p)
        if text and text.isdigit():
            values.append(int(text) / 1000)
    return values


def _boost_enabled() -> bool | None:
    # Generic (acpi-cpufreq, intel_pstate passive mode) location. Returns None
    # — "unknown", not "off" — rather than guessing on unsupported drivers
    # (e.g. intel_pstate active mode, which exposes no such file).
    text = _read("/sys/devices/system/cpu/cpufreq/boost")
    return text == "1" if text is not None else None


def _throttle_count() -> int:
    total = 0
    for p in Path("/sys/devices/system/cpu").glob("cpu*/thermal_throttle/core_throttle_count"):
        text = _read(p)
        if text and text.lstrip("-").isdigit():
            total += int(text)
    return total


def _mem_total_kb() -> int:
    text = _read("/proc/meminfo") or ""
    match = re.search(r"^MemTotal:\s*(\d+)", text, re.MULTILINE)
    return int(match.group(1)) if match else 0


def _loadavg() -> list[float]:
    text = _read("/proc/loadavg")
    return [float(x) for x in text.split()[:3]] if text else []


def _git_info() -> dict[str, object]:
    def run(*args):
        try:
            result = subprocess.run(
                ["git", *args], capture_output=True, text=True, check=True, timeout=5
            )
            return result.stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            return None

    return {
        "sha": run("rev-parse", "HEAD"),
        "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(run("status", "--porcelain")),
    }


def _package_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for name in _PACKAGES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def _distro() -> str:
    text = _read("/etc/os-release") or ""
    match = re.search(r'^PRETTY_NAME="?([^"\n]+)"?', text, re.MULTILINE)
    return match.group(1) if match else platform.platform()


def capture() -> dict:
    """One instantaneous machine+software snapshot. Callers that span a
    measurement window call this twice (before/after) and pass both to
    `merge_start_end()` — see that function for why."""
    topology = cpu_topology()
    threads = sum(len(cpus) for cpus in topology.values())
    gil_enabled = getattr(sys, "_is_gil_enabled", lambda: True)()

    return {
        "host": platform.node(),
        "kernel": platform.release(),
        "distro": _distro(),
        "cpu": {
            "model": _cpu_model(),
            "physical_cores": len(topology),
            "threads": threads,
            "smt_siblings": topology,
            "governor": _governors(),
            "boost": _boost_enabled(),
            "mhz": _frequencies_mhz(),
            "throttle_count": _throttle_count(),
        },
        "mem_total_kb": _mem_total_kb(),
        "loadavg": _loadavg(),
        "python": {
            "version": platform.python_version(),
            "impl": platform.python_implementation().lower(),
            "gil_disabled": not gil_enabled,
        },
        "packages": _package_versions(),
        "git": _git_info(),
    }


def merge_start_end(start: dict, end: dict) -> dict:
    """Combine two `capture()` snapshots into the PLAN.md §6 `env` shape:
    static facts (host/cpu identity/python/packages/git) come from `start`;
    the handful of fields that can move mid-run are recorded as both
    endpoints (`mhz_start`/`mhz_end`, `loadavg_start`/`loadavg_end`) or a
    delta (`throttle_count_delta`, a monotonic counter)."""
    merged = dict(start)
    cpu = dict(start["cpu"])
    cpu["mhz_start"] = start["cpu"]["mhz"]
    cpu["mhz_end"] = end["cpu"]["mhz"]
    cpu["throttle_count_delta"] = end["cpu"]["throttle_count"] - start["cpu"]["throttle_count"]
    del cpu["mhz"], cpu["throttle_count"]
    merged["cpu"] = cpu
    merged["loadavg_start"] = start["loadavg"]
    merged["loadavg_end"] = end["loadavg"]
    del merged["loadavg"]
    return merged


def warnings_for(env: dict) -> list[str]:
    """Audit failures worth surfacing (PLAN.md §4/§6 `warnings[]`)."""
    warnings = []
    if env["cpu"].get("boost"):
        warnings.append("cpu boost enabled — a live noise source")
    git = env.get("git") or {}
    if git.get("dirty"):
        warnings.append("git tree is dirty — this run's code may not match any commit")
    physical = env["cpu"].get("physical_cores") or 1
    loadavg = env.get("loadavg") or env.get("loadavg_start") or [0.0]
    if loadavg and loadavg[0] > physical * 0.5:
        warnings.append(
            f"1-minute loadavg {loadavg[0]:.2f} is high relative to "
            f"{physical} physical cores — other work may be competing for CPU"
        )
    return warnings
