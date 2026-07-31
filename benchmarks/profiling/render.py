"""Normalise cProfile/yappi output to speedscope JSON + folded stacks
. pyinstrument, py-spy and austin all produce speedscope
directly through their own tooling (see their adapter modules) — this module
is the generic converter for the two that don't.

Both `pstats.Stats` and yappi's `YFuncStats` describe a *call graph*
(caller/callee edges with aggregate times), not literal per-sample stacks, so
there is no single "the" stack per function. The folded-stack line for each
function is reconstructed by walking its most-frequent caller chain (cProfile)
or its call tree from every root function (yappi) — a standard, if
approximate, way to turn aggregate profiler output into flame-graph input;
tools like `pyprof2calltree`/`flameprof` make the same call-graph-to-stack
approximation.
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


def yappi_to_folded(stats: Any) -> list[str]:
    """Same idea as `pstats_to_folded`, but yappi's `YFuncStats` gives a
    proper callee tree (`.children`) instead of a caller map, so this walks
    down from every root function (one with no incoming call recorded) rather
    than up from every leaf."""
    called = {child.index for func in stats for child in func.children}
    roots = [func for func in stats if func.index not in called]
    by_index = {func.index: func for func in stats}

    lines = []

    def walk(func, path, seen):
        label = f"{func.name} ({func.module}:{func.lineno})"
        new_path = [*path, label]
        if func.tsub > 0:
            weight_us = max(1, round(func.tsub * 1_000_000))
            lines.append(f"{';'.join(new_path)} {weight_us}")
        for child in func.children:
            target = by_index.get(child.index)
            if target is not None and target.index not in seen and len(new_path) < MAX_STACK_DEPTH:
                walk(target, new_path, seen | {target.index})

    for root in roots:
        walk(root, [], {root.index})
    return lines


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
