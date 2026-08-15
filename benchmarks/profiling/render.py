"""Normalise cProfile output to speedscope JSON + folded stacks.

pyinstrument and py-spy emit speedscope themselves, and austin's own tooling
converts to it (`mojo2austin` then `austin2speedscope` — see `austin.py`), so
this module is the generic converter for the one that has no such path.

`pstats.Stats` describes a *call graph* (caller/callee edges with aggregate
times), not literal per-sample stacks, so there is no single "the" stack per
function. The folded-stack line for each function is reconstructed by walking
its most-frequent caller chain — a standard, if approximate, way to turn
aggregate profiler output into flame-graph input; tools like
`pyprof2calltree`/`flameprof` make the same call-graph-to-stack approximation.
"""

from __future__ import annotations

from typing import Any

MAX_STACK_DEPTH = 64


def pstats_to_folded(stats: Any) -> list[str]:
    """One folded-stack line per function with nonzero self time:
    `"root;...;caller;func weight_us"`."""
    lines = []
    for key, (_cc, _nc, tottime, _ct, _callers) in stats.stats.items():
        if tottime <= 0:
            continue
        stack = _pstats_chain(stats, key)
        weight_us = max(1, round(tottime * 1_000_000))
        lines.append(f"{';'.join(stack)} {weight_us}")
    return lines


def _pstats_chain(stats: Any, key: tuple[str, int, str]) -> list[str]:
    chain = [_pstats_label(key)]
    seen = {key}
    current = key
    for _ in range(MAX_STACK_DEPTH):
        callers = stats.stats[current][4]
        if not callers:
            break
        best = max(callers.items(), key=lambda kv: kv[1][1])[0]  # highest call count
        if best in seen:
            break
        chain.append(_pstats_label(best))
        seen.add(best)
        current = best
    chain.reverse()
    return chain


def _pstats_label(key: tuple[str, int, str]) -> str:
    filename, lineno, funcname = key
    short = filename.rsplit("/", 1)[-1]
    return f"{funcname} ({short}:{lineno})"


def folded_to_speedscope(folded_lines: list[str], profile_name: str) -> dict[str, Any]:
    """Minimal valid speedscope JSON (https://www.speedscope.app/file-format-schema.json)
    from folded-stack lines (`"frame;frame;frame weight"`, `flamegraph.pl`'s
    input format) — one "sampled" profile with one sample per line."""
    frame_index: dict[str, int] = {}
    frames: list[dict[str, str]] = []

    def frame_id(name: str) -> int:
        if name not in frame_index:
            frame_index[name] = len(frames)
            frames.append({"name": name})
        return frame_index[name]

    samples: list[list[int]] = []
    weights: list[int] = []
    for line in folded_lines:
        stack_part, _, weight_part = line.rpartition(" ")
        if not stack_part:
            continue
        samples.append([frame_id(name) for name in stack_part.split(";")])
        weights.append(int(weight_part))

    return {
        "$schema": "https://www.speedscope.app/file-format-schema.json",
        "shared": {"frames": frames},
        "profiles": [
            {
                "type": "sampled",
                "name": profile_name,
                "unit": "microseconds",
                "startValue": 0,
                "endValue": sum(weights),
                "samples": samples,
                "weights": weights,
            }
        ],
        "name": profile_name,
        "exporter": "rowform bench profile",
    }
