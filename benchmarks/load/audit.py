"""Concurrency audit: proves the load generator actually keeps N requests
in flight, three independent ways, rather than assuming it.

  1. Direct observation: count ESTABLISHED sockets on the server's port from
     `/proc/net/tcp` during a run. With N users requested there must be
     exactly N.
  2. Little's Law: in a closed loop with no think time, throughput * mean
     latency must equal N in flight. ~1 regardless of N means the generator
     is serialising; ~N means it is not.
  3. Throughput scaling: a serialising generator cannot go faster with more
     users (only ever one request outstanding); real concurrency rises to a
     knee and falls past it.

Plus the `/noop` headroom calibration: if `/noop` is not well above the
database endpoints, the *generator* saturated, not the server, and nothing
measured against it is usable.

These take plain numbers (`rps`, `mean_ms`, `concurrency`), not a
generator-specific result type — locust is the only generator `bench load`
drives, but there's no reason for this module to know that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

LITTLES_LAW_TOLERANCE = 0.10
NOOP_HEADROOM_MINIMUM = 2.0


def count_established(port: int) -> int:
    """ESTABLISHED sockets with local port == `port`, read from
    `/proc/net/tcp` — server-side accepted connections, not the listening
    socket (state `0A`). Returns -1 ("unknown") if unreadable (not Linux, or
    no permission) rather than a count a caller could mistake for zero."""
    hexport = f"{port:04X}"
    try:
        lines = Path("/proc/net/tcp").read_text().splitlines()[1:]
    except OSError:
        return -1
    count = 0
    for line in lines:
        fields = line.split()
        if len(fields) < 4:
            continue
        local, state = fields[1], fields[3]
        if state == "01" and local.endswith(f":{hexport}"):
            count += 1
    return count


@dataclass(slots=True)
class ConcurrencyCheck:
    concurrency: int
    sockets: int
    in_flight: float
    ok: bool
    notes: list[str] = field(default_factory=list)


def check_littles_law(
    rps: float, mean_ms: float, concurrency: int, sockets: int,
    tolerance: float = LITTLES_LAW_TOLERANCE,
) -> ConcurrencyCheck:
    """`rps * mean_latency` should equal `concurrency` (the requested user
    count), within `tolerance` — tighter would flag ordinary jitter, looser
    would miss a generator running at half the requested concurrency."""
    in_flight = rps * (mean_ms / 1000)
    notes = []
    ok = True
    if sockets != -1 and sockets != concurrency:
        notes.append(f"sockets ({sockets}) != concurrency ({concurrency})")
        ok = False
    deviation = abs(in_flight - concurrency) / concurrency if concurrency else 0.0
    if deviation > tolerance:
        notes.append(
            f"in-flight ({in_flight:.2f}) deviates {deviation:.0%} from concurrency "
            f"({concurrency}), tolerance {tolerance:.0%}"
        )
        ok = False
    return ConcurrencyCheck(
        concurrency=concurrency, sockets=sockets, in_flight=in_flight, ok=ok, notes=notes,
    )


KNEE_RISE_MINIMUM = 0.02


def find_scaling_knee(
    results: list[tuple[int, float]], rise_minimum: float = KNEE_RISE_MINIMUM,
) -> int | None:
    """`results` is `[(concurrency, rps), ...]`. Returns the concurrency at
    which throughput stops *meaningfully* rising — less than `rise_minimum`
    over the best so far counts as stopped, so two statistically identical
    levels read as a knee instead of ordinary run-to-run jitter reading as
    scaling. `None` if it rose throughout the levels tested (the knee is
    beyond them, not a failure of the generator)."""
    ordered = sorted(results, key=lambda r: r[0])
    best_rps = 0.0
    for concurrency, rps in ordered:
        if rps <= best_rps * (1 + rise_minimum):
            return concurrency
        best_rps = rps
    return None


def check_noop_headroom(
    noop_rps: float, db_rps: float, minimum: float = NOOP_HEADROOM_MINIMUM,
) -> tuple[bool, float]:
    """`/noop` must be at least `minimum`x the fastest database endpoint, or
    the load generator — not the server — was the bottleneck."""
    ratio = noop_rps / db_rps if db_rps else float("inf")
    return ratio >= minimum, ratio


GENERATOR_SATURATION_MAXIMUM = 0.9


def check_generator_saturation(
    utilization: float, maximum: float = GENERATOR_SATURATION_MAXIMUM,
) -> tuple[bool, float]:
    """The generator's own CPU utilization (process CPU time / wall time)
    during a run must stay below `maximum` — above it, the client is the
    bottleneck and every rps figure from that level describes the generator,
    not the server. Audit the generator; never assume its concurrency."""
    return utilization < maximum, utilization
