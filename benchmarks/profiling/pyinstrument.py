"""pyinstrument adapter: in-process sampling, wall clock,
`async_mode="enabled"` so time spent awaiting is attributed correctly instead
of charged to whatever happened to be on the stack when the sampler fired.
"""

from __future__ import annotations

from pyinstrument import Profiler
from pyinstrument.renderers import SpeedscopeRenderer


class PyinstrumentProfiler:
    name = "pyinstrument"

    def __init__(self) -> None:
        self._profiler = Profiler(async_mode="enabled")

    def start(self) -> None:
        self._profiler.start()

    def stop(self) -> Profiler:
        self._profiler.stop()
        return self._profiler

    def to_speedscope(self, profiler: Profiler) -> str:
        """pyinstrument ships its own speedscope renderer — no need to route
        through `profiling.render`'s pstats converter."""
        return profiler.output(renderer=SpeedscopeRenderer())
