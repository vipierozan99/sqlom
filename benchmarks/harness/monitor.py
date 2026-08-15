"""Per-process CPU monitor. Samples every tracked pid once a second and
accumulates a time series a caller can dump to JSON or summarise — silently;
nothing is printed per sample, only `print_averages()` at the end of a run
prints anything, so a `bench load` run's own progress output isn't
interleaved with a line every second. This is the finer-grained, continuously
*recorded* version of the same `/proc/<pid>/stat` read that makes utilization a
recorded metric rather than throughput alone — `load/audit.py` still computes
its own once-per-level utilization for the Little's Law gate; this is the
whole-run companion to that.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from benchmarks.harness.cpuacct import pid_alive, read_pid_cpu_seconds

DEFAULT_INTERVAL_S = 1.0


@dataclass(slots=True)
class ProcessMonitor:
    """`track()` every pid as you spawn it, `start()` once, `stop()` when the
    run ends. Safe to track/untrack while running — a role whose process has
    exited simply stops being sampled (its history stays in `samples`), rather
    than crashing the monitor or diluting its average with 0% readings from
    after its death."""

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
        # Re-baseline every tracked pid: CPU accrued between track() and
        # start() must not be charged to the first wall interval.
        for role, pid in self.pids.items():
            self._last_cpu[role] = read_pid_cpu_seconds(pid)
        self._task = asyncio.ensure_future(self._loop())

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            self._sample()

    def _sample(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_wall
        row: dict[str, Any] = {"t": round(now - self._start_wall, 2)}
        for role, pid in self.pids.items():
            if not pid_alive(pid):
                continue  # exited: keep its history, stop writing 0% rows
            cpu_now = read_pid_cpu_seconds(pid)
            previous = self._last_cpu.get(role, cpu_now)
            utilization = max(0.0, (cpu_now - previous) / elapsed) if elapsed > 0 else 0.0
            row[role] = round(utilization, 4)
            self._last_cpu[role] = cpu_now
        self._last_wall = now
        self.samples.append(row)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    def averages(self) -> dict[str, float]:
        """Mean utilization per role across every sample that included it —
        a role only alive for part of the run (e.g. a locust subprocess) is
        averaged only over the samples where it was actually measured, not
        diluted by samples from before it existed or after it exited. Roles
        come from the recorded samples plus current tracking, so an
        `untrack()`ed or exited role keeps its history."""
        totals: dict[str, float] = {}
        counts: dict[str, int] = {}
        for row in self.samples:
            for role, value in row.items():
                if role == "t":
                    continue
                totals[role] = totals.get(role, 0.0) + value
                counts[role] = counts.get(role, 0) + 1
        roles = set(totals) | set(self.pids)
        return {role: (totals[role] / counts[role] if counts.get(role) else 0.0) for role in roles}

    def print_averages(self) -> None:
        """The one thing this monitor prints — call after `stop()`."""
        if not self.pids:
            return
        averages = self.averages()
        parts = [
            f"{role}(pid={pid}) {averages.get(role, 0.0) * 100:5.1f}% avg cpu"
            for role, pid in self.pids.items()
        ]
        self.print_fn("  " + "  ".join(parts))

    def to_dict(self) -> dict[str, Any]:
        return {
            "pids": dict(self.pids), "interval_s": self.interval, "samples": self.samples,
            "averages": self.averages(),
        }
