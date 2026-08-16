"""`CorePlan`: physical-core aware CPU pinning.

The finding this exists to prevent: on the reference machine, `cpu0`/`cpu1` are
SMT siblings of *one* physical core, not two independent ones, and the old
suite's shell scripts pinned server and generator to adjacent indices assuming
otherwise. Reasoning in physical cores
rather than raw indices is the fix; `cpu_topology()` is what makes that
possible without hardcoding a machine's layout (the `bench_row_access.py`
lesson — a number that came from one box must not be assumed on another).

Best-effort on any core count: prefers disjoint whole physical cores per role,
degrades to sharing with a recorded warning, and never refuses.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

_SYS_CPU = Path("/sys/devices/system/cpu")


def cpu_topology() -> dict[str, list[int]]:
    """Map "package:core" -> sorted logical CPU (SMT sibling) ids.

    Falls back to one logical CPU per "core" (i.e. no SMT-awareness) if
    `/sys/devices/system/cpu/*/topology` isn't readable — containers and some
    kernels restrict it. Best-effort, not a hard requirement.
    """
    cores: dict[tuple[int, int], list[int]] = {}
    for cpu_dir in sorted(_SYS_CPU.glob("cpu[0-9]*"), key=lambda p: int(p.name[3:])):
        idx = int(cpu_dir.name[3:])
        topo = cpu_dir / "topology"
        try:
            pkg = int((topo / "physical_package_id").read_text().strip())
            core = int((topo / "core_id").read_text().strip())
        except (FileNotFoundError, ValueError, OSError):
            continue
        cores.setdefault((pkg, core), []).append(idx)
    if cores:
        return {f"{pkg}:{core}": sorted(cpus) for (pkg, core), cpus in sorted(cores.items())}
    # Fallback: treat every online logical CPU as its own physical core.
    return {str(i): [i] for i in sorted(os.sched_getaffinity(0))}


@dataclass(frozen=True, slots=True)
class CorePlan:
    roles: dict[str, list[int]]
    smt_shared: bool
    degraded: bool
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            **{role: cpus for role, cpus in self.roles.items()},
            "smt_shared": self.smt_shared,
            "degraded": self.degraded,
        }


def plan(role_core_counts: dict[str, int], topology: dict[str, list[int]] | None = None) -> CorePlan:
    """Assign `role_core_counts[role]` whole physical cores to each role, in
    the order given, preferring cores no earlier role has touched.

    Once distinct physical cores run out, later roles reuse cores from the
    start of the list (`degraded=True`, one warning per reused core) rather
    than raising — a 2-core CI box must still produce a plan, just an honestly
    labelled one.
    """
    topology = topology if topology is not None else cpu_topology()
    physical = list(topology.values()) or [[i] for i in range(os.cpu_count() or 1)]

    assigned: dict[str, list[int]] = {}
    warnings: list[str] = []
    degraded = False
    cursor = 0
    for role, count in role_core_counts.items():
        cpus: list[int] = []
        for _ in range(max(count, 0)):
            core = physical[cursor % len(physical)]
            if cursor >= len(physical):
                degraded = True
                warnings.append(
                    f"role {role!r} reuses physical core {core} — not enough distinct "
                    f"physical cores on this machine for the requested plan"
                )
            cpus.extend(core)
            cursor += 1
        assigned[role] = sorted(set(cpus))

    seen: set[int] = set()
    smt_shared = False
    for cpus in assigned.values():
        if seen & set(cpus):
            smt_shared = True
        seen |= set(cpus)

    return CorePlan(roles=assigned, smt_shared=smt_shared, degraded=degraded, warnings=warnings)


def set_affinity(pid: int, cpus: list[int]) -> None:
    os.sched_setaffinity(pid, cpus)


def resolve_pin(pin: str | None) -> tuple[list[int], list[str]]:
    """Turn a `--pin` option value into concrete cpu ids: `"auto"` plans two
    whole physical cores from the part of this machine's topology the process is
    allowed on (never hardcoded indices — see the module docstring), `""`/None
    disables pinning, anything else is comma-separated logical ids taken
    literally. Returns the ids plus any planner warnings (e.g. a small machine,
    or a small cpuset, having to share cores)."""
    if pin == "auto":
        # Plan only over CPUs this process may actually run on: sysfs lists every
        # host CPU even inside a restricted cpuset, so an unfiltered plan hands
        # `sched_setaffinity` ids the kernel will reject (EINVAL) or quietly
        # narrow — either way the pinning is not the one that was planned.
        allowed = os.sched_getaffinity(0)
        visible: dict[str, list[int]] = {}
        for core, cpus in cpu_topology().items():
            siblings = [c for c in cpus if c in allowed]
            if siblings:
                visible[core] = siblings
        auto = plan({"bench": 2}, visible or {str(i): [i] for i in sorted(allowed)})
        return auto.roles["bench"], list(auto.warnings)
    return ([int(c) for c in pin.split(",")] if pin else []), []


def read_back(pid: int) -> list[int]:
    """Read back the mask the kernel actually has for `pid`, rather than
    trusting the one that was requested — only the former is evidence."""
    return sorted(os.sched_getaffinity(pid))


@contextmanager
def pin_current_process(cpus: list[int]):
    """Restrict the calling process to `cpus` for the block, restoring the
    prior affinity mask on exit (even on exception) — same guarantee
    `timing.gc_control` gives GC state. `cpus=[]` is a no-op (nothing
    requested to pin to).

    Yields the actual mask read back after setting it (`None` if `cpus` was
    empty) rather than `cpus` itself, since the kernel is free to reject or
    adjust the request.
    """
    if not cpus:
        yield None
        return
    prior = read_back(0)
    set_affinity(0, cpus)
    try:
        yield read_back(0)
    finally:
        set_affinity(0, prior)
