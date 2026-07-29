"""The one equivalence gate (PLAN.md §4): every contender must emit
byte-identical output before timing starts, or the comparison means nothing.
Three dialects of this existed across 8 files; this is the one implementation.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class EquivalenceResult:
    enforced: bool
    reference: str | None
    payload_sha256: str | None
    payload_bytes: int
    self_consistent: bool
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.enforced and not self.failures


async def check(
    requests: dict[str, Callable[[], Awaitable[bytes]]], *, self_consistency_runs: int = 3,
) -> EquivalenceResult:
    """Run every contender's `request()` once and compare bytes.

    Also re-runs the first contender `self_consistency_runs` times: a
    contender that is non-deterministic against itself (e.g. dict ordering, an
    unstable sort) would otherwise pass this gate by accident on a lucky single
    run and then silently violate it on every timed request.

    An empty payload from every contender does not count as agreement —
    that is the empty-result guard: it makes the fairness gate pass while
    measuring nothing.
    """
    if not requests:
        raise ValueError("check() needs at least one contender")

    outputs: dict[str, bytes] = {}
    for name, request in requests.items():
        outputs[name] = await request()

    reference_name = next(iter(outputs))
    reference = outputs[reference_name]

    failures = []
    if not reference or reference in (b"[]", b"null"):
        failures.append(
            f"{reference_name!r} produced an empty result set — equivalence would "
            f"pass vacuously; use more rows or a less restrictive filter"
        )

    for name, payload in outputs.items():
        if payload != reference:
            failures.append(
                f"{name!r} differs from {reference_name!r} "
                f"({len(payload)} vs {len(reference)} bytes)"
            )

    self_consistent = True
    reference_request = requests[reference_name]
    for _ in range(self_consistency_runs):
        if await reference_request() != reference:
            self_consistent = False
            failures.append(
                f"{reference_name!r} is not self-consistent — repeated calls with "
                f"identical inputs produced different bytes"
            )
            break

    return EquivalenceResult(
        enforced=True,
        reference=reference_name,
        payload_sha256=hashlib.sha256(reference).hexdigest() if reference else None,
        payload_bytes=len(reference),
        self_consistent=self_consistent,
        failures=failures,
    )
