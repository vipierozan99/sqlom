"""`CorePlan`: physical-core aware CPU pinning (PLAN.md §4, §13 D13).

PLAN.md §3 records the finding this exists to prevent: on the reference
machine, `cpu0`/`cpu1` are SMT siblings of *one* physical core, not two
independent ones, and the old suite's shell scripts pinned server and
generator to adjacent indices assuming otherwise. Reasoning in physical cores
rather than raw indices is the fix; `cpu_topology()` is what makes that
possible without hardcoding a machine's layout (the `bench_row_access.py`
lesson — a number that came from one box must not be assumed on another).

D13: best-effort on any core count. Prefers disjoint whole physical cores per
role; degrades to sharing with a recorded warning; never refuses.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_SYS_CPU = Path("/sys/devices/system/cpu")


def cpu_topology() -> dict[str, list[int]]:
    """Map "package:core" -> sorted logical CPU (SMT sibling) ids.

    Falls back to one logical CPU per "core" (i.e. no SMT-awareness) if
    `/sys/devices/system/cpu/*/topology` isn't readable — containers and some
    kernels restrict it. Best-effort per D13, not a hard requirement.
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


def read_back(pid: int) -> list[int]:
    """The actual mask the kernel has for `pid`, read back rather than
    trusted — PLAN.md §4: "records actual masks.\""""
    return sorted(os.sched_getaffinity(pid))


def verify(pid: int, expected_cpus: list[int]) -> bool:
    return read_back(pid) == sorted(set(expected_cpus))
