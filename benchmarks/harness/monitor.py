"""Live per-process CPU monitor. Samples every tracked pid once a second,
prints one line per sample, and accumulates a time series a caller can dump
to JSON — the always-on visibility `bench load` didn't have: CPU utilization
was previously computed once, over the whole run, at the end (`load/audit.py`
still does that for its Little's Law gate). This is the finer-grained,
running version of the same `/proc/<pid>/stat` read PLAN.md §4 asks for
("record utilization, not just throughput").
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from benchmarks.harness.cpuacct import read_pid_cpu_seconds

DEFAULT_INTERVAL_S = 1.0


@dataclass(slots=True)
class ProcessMonitor:
    """`track()` every pid as you spawn it, `start()` once, `stop()` when the
    run ends. Safe to track/untrack while running — a role whose process has
    already exited just reads 0% (via `read_pid_cpu_seconds`'s own
    exited-process guard) rather than crashing the monitor."""

    interval: float = DEFAULT_INTERVAL_S
    print_fn: Callable[[str], None] = print
    pids: dict[str, int] = field(default_factory=dict)
    samples: list[dict[str, Any]] = field(default_factory=list)
    _task: asyncio.Task[None] | None = field(default=None, repr=False)
    _start_wall: float = field(default=0.0, repr=False)
    _last_wall: float = field(default=0.0, repr=False)
    _last_cpu: dict[str, float] = field(default_factory=dict, repr=False)

    def track(self, role: str, pid: int) -> None:
        self.pids[role] = pid
        self._last_cpu[role] = read_pid_cpu_seconds(pid)

    def untrack(self, role: str) -> None:
        self.pids.pop(role, None)
        self._last_cpu.pop(role, None)

    def start(self) -> None:
        self._start_wall = time.monotonic()
        self._last_wall = self._start_wall
        self._task = asyncio.ensure_future(self._loop())

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            self._sample()

    def _sample(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_wall
        row: dict[str, Any] = {"t": round(now - self._start_wall, 2)}
        parts = []
        for role, pid in self.pids.items():
            cpu_now = read_pid_cpu_seconds(pid)
            previous = self._last_cpu.get(role, cpu_now)
            utilization = max(0.0, (cpu_now - previous) / elapsed) if elapsed > 0 else 0.0
            row[role] = round(utilization, 4)
            self._last_cpu[role] = cpu_now
            parts.append(f"{role}(pid={pid}) {utilization * 100:5.1f}%")
        self._last_wall = now
        self.samples.append(row)
        self.print_fn("  " + "  ".join(parts))

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    def to_dict(self) -> dict[str, Any]:
        return {"pids": dict(self.pids), "interval_s": self.interval, "samples": self.samples}
