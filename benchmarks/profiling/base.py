"""Profiler adapter protocols.

Two kinds, because they attach fundamentally differently:

`InProcessProfiler` runs inside the profiled process — `start()`/`stop()`
bracket the profiled region and `stop()` returns a profiler-native stats
object that `profiling.render` knows how to normalise.

`ExternalProfiler` attaches to an already-running process from outside —
`attach()` runs for the whole window and writes speedscope JSON directly to
`out_path` (both py-spy and austin's own toolchains produce it without
needing `render.py`'s generic converter).
"""

from __future__ import annotations

from typing import Any, Protocol


class InProcessProfiler(Protocol):
    name: str

    def start(self) -> None: ...
    def stop(self) -> Any: ...


class ExternalProfiler(Protocol):
    name: str

    async def attach(self, pid: int, duration: float, out_path: str) -> str: ...
