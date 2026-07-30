"""Compare recorded micro runs from two commits and fail on a regression.

    python scripts/compare_bench.py --base base/*/run.json --head head/*/run.json

Reads `median_ms` per contender out of the run.json files written by
`bench micro run --record`, takes the **minimum** across each side's runs, and
exits non-zero if any contender got slower by more than `--tolerance`.

Minimum, not median-of-medians: on a shared CI runner the distribution has a hard
floor and a long tail of interference, so the fastest observed run is the closest
estimate of what the code can do, and repeating both sides makes a one-off stall
on either side harmless.

**What this can and cannot see.** GitHub-hosted runners are shared and unpinned,
so the tolerance has to be loose enough to absorb that — this catches a
gross regression (an accidental per-row `setattr`, a lost hydrator cache), not a
few percent. Fine-grained work needs the pinned local harness; see
docs/METHODOLOGY.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

METRIC = "median_ms"


def medians(paths: list[Path]) -> dict[str, float]:
    """`{contender: fastest median_ms seen}` across every run given."""
    best: dict[str, float] = {}
    for path in paths:
        run = json.loads(path.read_text())
        for cell in run["cells"]:
            for trial in cell["trials"]:
                value = trial["metrics"].get(METRIC)
                if value is None:
                    continue
                name = cell["contender"]
                if name not in best or value < best[name]:
                    best[name] = value
    return best


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", nargs="+", type=Path, required=True)
    parser.add_argument("--head", nargs="+", type=Path, required=True)
    parser.add_argument(
        "--tolerance",
        type=float,
        default=1.25,
        help="fail when head/base exceeds this ratio (default 1.25)",
    )
    args = parser.parse_args()

    base, head = medians(args.base), medians(args.head)
    shared = sorted(set(base) & set(head))
    if not shared:
        print(f"no contender appears on both sides: base={sorted(base)} head={sorted(head)}")
        return 1

    print(f"{'contender':<42}{'base ms':>10}{'head ms':>10}{'ratio':>8}  verdict")
    regressions = []
    for name in shared:
        ratio = head[name] / base[name]
        bad = ratio > args.tolerance
        if bad:
            regressions.append((name, ratio))
        print(
            f"{name:<42}{base[name]:>10.4f}{head[name]:>10.4f}{ratio:>8.2f}x"
            f"  {'REGRESSION' if bad else 'ok'}"
        )

    # A contender that exists on only one side is reported rather than ignored: it
    # usually means a renamed or dropped benchmark, which silently empties the gate.
    for name in sorted(set(base) ^ set(head)):
        print(f"note: {name!r} recorded on only one side, not compared")

    if regressions:
        print()
        for name, ratio in regressions:
            print(f"FAIL {name} is {ratio:.2f}x slower than base (tolerance {args.tolerance}x)")
        return 1

    print(f"\nOK — nothing beyond {args.tolerance}x of base.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
