"""cProfile adapter: instrumented, CPU clock
(`process_time_ns`), whole-loop — no per-coroutine breakdown. Keeps its CPU
timer deliberately: a wall profile of an asyncio
loop is dominated by `epoll_wait`.

Known bias to preserve, never resolve by picking the flattering number:
cProfile's per-call overhead inflates call-heavy Python.
"""

from __future__ import annotations

import cProfile
import pstats


class CProfileProfiler:
    name = "cprofile"

    def __init__(self) -> None:
        self._profiler = cProfile.Profile()

    def start(self) -> None:
        self._profiler.enable()

    def stop(self) -> pstats.Stats:
        self._profiler.disable()
        return pstats.Stats(self._profiler)
