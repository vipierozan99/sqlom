"""cProfile adapter (PLAN.md §10): instrumented, CPU clock
(`process_time_ns`), whole-loop — no native per-coroutine breakdown, that is
yappi's job. Keeps its CPU timer deliberately: a wall profile of an asyncio
loop is dominated by `epoll_wait`.

Known bias to preserve, never resolve by picking the flattering number:
cProfile's per-call overhead inflates call-heavy Python (PLAN.md §10).
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
