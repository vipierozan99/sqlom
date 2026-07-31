"""yappi adapter: instrumented, **both** CPU and wall clock,
with native per-coroutine aggregation — the one adapter here that attributes
time to individual async tasks rather than charging it all to the event loop.
"""

from __future__ import annotations

import yappi as _yappi


class YappiProfiler:
    name = "yappi"

    def __init__(self, clock: str = "cpu") -> None:
        if clock not in ("cpu", "wall"):
            raise ValueError(f"clock must be 'cpu' or 'wall', got {clock!r}")
        self.clock = clock

    def start(self) -> None:
        _yappi.set_clock_type(self.clock)
        _yappi.start()

    def stop(self) -> _yappi.YFuncStats:
        _yappi.stop()
        stats = _yappi.get_func_stats()
        _yappi.clear_stats()
        return stats
