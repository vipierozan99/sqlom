"""Per-role CPU accounting — utilization is recorded, not inferred from
throughput. Reads `/proc/<pid>/stat` utime+stime for the pids in each role
(server/generator/db), so `cpu_ms_per_request`/`cpu_utilization` are measured
per role rather than inferred from the client's own `process_time()` alone —
the client-only version is what the old suite's `run_load()` had.
"""

from __future__ import annotations

import os
import resource
from pathlib import Path

_CLOCK_TICKS = os.sysconf("SC_CLK_TCK")


def children_cpu_seconds() -> float:
    """Cumulative utime+stime of every *reaped* child process, ever, in this
    process's lifetime (`resource.getrusage(RUSAGE_CHILDREN)`). Unlike
    `read_pid_cpu_seconds`, this stays readable *after* a subprocess exits —
    `/proc/<pid>/stat` disappears the moment a process is reaped, so bracketing
    a short-lived subprocess (e.g. one `locust` invocation) with before/after
    calls to this function is the only way to get its total CPU, not a
    before/after read of its own pid. Safe to use as a delta across one
    subprocess as long as nothing else reaps a child concurrently in that
    window — true here, since callers await each subprocess (or a
    `asyncio.gather` of a known, fixed set of them) before starting the next."""
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def read_pid_cpu_seconds(pid: int) -> float:
    """utime+stime for `pid`, in seconds. Returns 0.0 if the process has
    already exited or its stat line can't be parsed — a role's process ending
    mid-sample must not crash the whole run (use `pid_alive` to tell the two
    apart)."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        # field 2 is "(comm)", which may itself contain spaces or parens; utime
        # and stime are fields 14/15 (1-indexed) counting from the *last*
        # closing paren — a comm containing ")" would misalign a first-paren
        # split.
        rest = text[text.rindex(")") + 1:].split()
        utime, stime = int(rest[11]), int(rest[12])
    except (FileNotFoundError, ProcessLookupError, OSError, ValueError, IndexError):
        return 0.0
    return (utime + stime) / _CLOCK_TICKS


def pid_alive(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


class CpuAccountant:
    """Samples one or more pids per role across a measurement window.

    Deltas are taken per pid and clamped at zero: a pid that exits mid-window
    reads 0.0 at `stop()`, and subtracting its (possibly large) start baseline
    from that used to make the whole role's utilization understated or
    negative. Its post-`start()` CPU is still lost — `/proc` offers nowhere to
    read it after the exit — but it can no longer erase other pids' work."""

    def __init__(self, role_pids: dict[str, list[int]]):
        self._role_pids = role_pids
        self._start: dict[int, float] = {}

    def start(self) -> None:
        self._start = {
            pid: read_pid_cpu_seconds(pid)
            for pids in self._role_pids.values()
            for pid in pids
        }

    def stop(self, elapsed_s: float) -> dict[str, float]:
        """cpu_utilization per role: cpu-seconds consumed / wall-clock elapsed
        over the window between `start()` and `stop()`."""
        if elapsed_s <= 0:
            return dict.fromkeys(self._role_pids, 0.0)
        utilization = {}
        for role, pids in self._role_pids.items():
            consumed = sum(
                max(0.0, read_pid_cpu_seconds(pid) - self._start.get(pid, 0.0)) for pid in pids
            )
            utilization[role] = consumed / elapsed_s
        return utilization


def cgroup_cpu_seconds(cgroup_path: str = "/sys/fs/cgroup") -> float | None:
    """cgroup v2 `cpu.stat` `usage_usec`, in seconds — an alternative to
    summing per-pid `/proc/<pid>/stat` when a role runs as short-lived
    processes under one cgroup (e.g. a docker container). `None` if
    unavailable (cgroup v1, no permission, not Linux)."""
    stat_path = Path(cgroup_path) / "cpu.stat"
    try:
        for line in stat_path.read_text().splitlines():
            if line.startswith("usage_usec"):
                return int(line.split()[1]) / 1_000_000
    except (FileNotFoundError, OSError, ValueError):
        return None
    return None
