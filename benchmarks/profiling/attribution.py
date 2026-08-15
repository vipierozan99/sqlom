"""Ported from the old `benchmarks/profkit.py`, plus the impossible-row
tripwire: an impossible-by-construction row must read zero, asserted rather than
eyeballed.

Extracted so the sqlite and Postgres profilers cannot drift apart on how they
attribute frames — one of them already did, silently: `profkit.py:1-7`
documented private copies of `rollup`/`top_functions` shadowing the shared
ones.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from pathlib import Path

import rowform as rf

# Resolved once. Substring matching on "rowform" is a trap: this repo's own
# directory is named rowform, so a naive r"/rowform/" pattern also matches
# /home/user/rowform/benchmarks/... and credits harness work to the library.
# Compare against real package directories instead.
ROWFORM_DIR = str(Path(rf.__file__).resolve().parent) + os.sep
BENCH_DIR = str(Path(__file__).resolve().parent.parent) + os.sep

# Functions rowform generates via exec(). Their code object filename is
# "<string>", which SQLAlchemy's own codegen also uses, so these are matched
# on name instead.
ROWFORM_GENERATED = {"_hydrate", "_hydrate_all", "_default", "_rows_to_dicts"}

# SQLAlchemy categories come before the drivers, and the driver patterns are
# anchored to package directories: SQLAlchemy's own dialect adapters live in
# files *named after* the driver (`/sqlalchemy/dialects/sqlite/aiosqlite.py`,
# `.../postgresql/psycopg.py`), and a loose substring tested driver-first
# credited that adapter work — real per-cursor/per-connection wrapping — to
# the raw driver, inflating the driver's share for every SQLAlchemy contender.
CATEGORIES = [
    ("orjson", [r"orjson"]),
    ("SQLAlchemy ORM", [r"/sqlalchemy/orm/"]),
    ("SQLAlchemy engine", [r"/sqlalchemy/engine/", r"/sqlalchemy/pool/", r"/sqlalchemy/ext/asyncio/"]),
    ("SQLAlchemy SQL", [r"/sqlalchemy/sql/", r"/sqlalchemy/util/", r"/sqlalchemy/dialects/"]),
    ("SQLAlchemy codegen", [r"<string>"]),
    # `'sqlite3\.` catches the C methods, which cProfile names
    # "<method 'execute' of 'sqlite3.Connection' objects>" under filename "~".
    ("sqlite3 driver", [r"/sqlite3/", r"_sqlite3", r"'sqlite3\.", r"/aiosqlite/"]),
    ("asyncpg", [r"/asyncpg/"]),
    ("psycopg", [r"/psycopg"]),
    ("asyncio / loop", [r"/asyncio/", r"selectors\.py", r"sslproto"]),
]


def categorize(filename: str, funcname: str) -> str:
    if filename.startswith(BENCH_DIR):
        return "benchmark harness"
    if filename.startswith(ROWFORM_DIR):
        return "rowform (library)"
    if filename == "<string>" and funcname in ROWFORM_GENERATED:
        return "rowform (codegen)"
    subject = f"{filename}:{funcname}"
    for label, patterns in CATEGORIES:
        if any(re.search(p, subject) for p in patterns):
            return label
    return "stdlib / other"


def rollup(stats, attribute_builtins: bool = True) -> list[tuple[str, float]]:
    """Aggregate per-function self-CPU into per-library shares.

    A flat rollup misattributes generic C builtins: `object.__new__` and
    `list.append` are almost entirely *caused by* rowform's generated
    hydrator, but their frames belong to no library and land in "stdlib". So
    by default each such builtin's self time is redistributed to its callers
    in proportion to call counts, one level up.
    """
    buckets: dict[str, float] = defaultdict(float)
    for key, (_cc, _nc, tottime, _ct, callers) in stats.stats.items():
        filename, _lineno, funcname = key
        own = categorize(filename, funcname)
        generic_builtin = filename == "~" and own == "stdlib / other"
        if generic_builtin and attribute_builtins and callers:
            total_calls = sum(c[1] for c in callers.values()) or 1
            for ckey, cval in callers.items():
                cfile, _cl, cfunc = ckey
                buckets[categorize(cfile, cfunc)] += tottime * cval[1] / total_calls
        else:
            buckets[own] += tottime
    return sorted(buckets.items(), key=lambda kv: -kv[1])


def top_functions(stats, n: int) -> list[tuple[int, float, float, str]]:
    rows = []
    for (filename, lineno, funcname), (_cc, nc, tottime, cumtime, _cal) in stats.stats.items():
        short = Path(filename).name if filename not in ("~", "<string>") else filename
        rows.append((nc, tottime, cumtime, f"{short}:{lineno}({funcname})"))
    rows.sort(key=lambda r: -r[1])
    return rows[:n]


# --- the impossible-row tripwire --------------------------------

# Contenders that never import rowform at all — profiling one of them must
# show exactly 0.0% in the rowform categories, or frame attribution has a bug
# (a stray import, a substring match catching something it shouldn't).
NON_ROWFORM_CONTENDER_TAGS = ("floor",)
IMPOSSIBLE_FOR_NON_ROWFORM = ("rowform (library)", "rowform (codegen)")


def check_impossible_rows(
    rolled_up: list[tuple[str, float]], forbidden_categories: tuple[str, ...],
) -> list[str]:
    """Return one warning per `forbidden_categories` entry with non-zero
    share in `rolled_up` — empty if every impossible-by-construction row
    reads exactly 0.0%, as it must."""
    shares = dict(rolled_up)
    total = sum(shares.values()) or 1.0
    warnings = []
    for category in forbidden_categories:
        share = shares.get(category, 0.0)
        if share:
            warnings.append(
                f"impossible row: {category!r} read {share / total * 100:.4f}% "
                f"(expected exactly 0.0%) — frame attribution likely has a bug"
            )
    return warnings


def print_rollup(stats, profiled_cpu: float, n_requests: int, real_cpu_ms: float, indent: str = "    ") -> None:
    inflation = (profiled_cpu * 1000 / n_requests) / real_cpu_ms if real_cpu_ms else 0
    print(f"{indent}profiled {profiled_cpu * 1000 / n_requests:.3f} ms/req vs "
          f"{real_cpu_ms:.3f} unprofiled ({inflation:.1f}x instrumentation overhead) "
          f"— read shares, not ms")
    print(f"{indent}generic C builtins attributed to their callers\n")
    print(f"{indent}{'library':<22}{'share':>8}{'flat':>8}")
    print(f"{indent}{'-' * 38}")
    flat = dict(rollup(stats, attribute_builtins=False))
    for lib, secs in rollup(stats):
        if profiled_cpu and secs / profiled_cpu < 0.005:
            continue
        share = secs / profiled_cpu * 100 if profiled_cpu else 0.0
        flat_share = flat.get(lib, 0) / profiled_cpu * 100 if profiled_cpu else 0.0
        print(f"{indent}{lib:<22}{share:>7.1f}%{flat_share:>7.1f}%")


def print_top(stats, n: int, indent: str = "    ") -> None:
    print(f"\n{indent}Top {n} functions by self CPU:\n")
    print(f"{indent}{'ncalls':>10}{'tot ms':>9}{'cum ms':>9}  where")
    print(f"{indent}{'-' * 74}")
    for ncalls, tot, cum, where in top_functions(stats, n):
        print(f"{indent}{ncalls:>10}{tot * 1000:>9.1f}{cum * 1000:>9.1f}  {where[:60]}")
