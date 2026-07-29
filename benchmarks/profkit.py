"""Shared profiling helpers for the profile_* scripts.

Extracted so the Postgres and sqlite profilers cannot drift apart on how they
attribute frames — the attribution rules are the part most likely to be subtly
wrong, and one of them already was (see docs/METHODOLOGY.md on categorizing by
substring).
"""

import os
import re
from collections import defaultdict
from pathlib import Path

import rowform as _rowform_pkg

# Resolved once. Substring matching on "rowform" is a trap: this repo's own
# directory is named rowform, so a naive r"/rowform/" pattern also matches
# /home/user/rowform/benchmarks/bench_pg_load.py and credits harness work to the
# library. Compare against real package directories instead.
ROWFORM_DIR = str(Path(_rowform_pkg.__file__).resolve().parent) + os.sep
BENCH_DIR = str(Path(__file__).resolve().parent) + os.sep

# Functions rowform generates via exec(). Their code object filename is "<string>",
# which SQLAlchemy also uses for its own codegen, so match on name.
ROWFORM_GENERATED = {"_hydrate", "_hydrate_all", "_default", "_rows_to_dicts"}

# Checked in order after the path-based tests; first match wins. C functions are
# matched on funcname too, which is how sqlite3's methods get recognised: they
# appear as "<method 'execute' of 'sqlite3.Cursor' objects>".
CATEGORIES = [
    ("orjson",             [r"orjson"]),
    ("sqlite3 driver",     [r"sqlite3"]),
    ("asyncpg",            [r"/asyncpg/"]),
    ("SQLAlchemy ORM",     [r"/sqlalchemy/orm/"]),
    ("SQLAlchemy engine",  [r"/sqlalchemy/engine/", r"/sqlalchemy/pool/",
                            r"/sqlalchemy/ext/asyncio/"]),
    ("SQLAlchemy SQL",     [r"/sqlalchemy/sql/", r"/sqlalchemy/util/",
                            r"/sqlalchemy/dialects/"]),
    ("SQLAlchemy codegen", [r"<string>"]),
    ("asyncio / loop",     [r"/asyncio/", r"selectors\.py", r"sslproto"]),
]


def categorize(filename, funcname):
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


def rollup(stats, attribute_builtins=True):
    """Aggregate per-function self-CPU into per-library shares.

    A flat rollup misattributes generic C builtins: `object.__new__` and
    `list.append` are almost entirely *caused by* rowform's generated hydrator, but
    their frames belong to no library and land in "stdlib". So by default each
    such builtin's self time is redistributed to its callers in proportion to
    call counts, one level up.

    A C function that belongs to a real library (orjson.dumps, sqlite3.Cursor
    methods) is doing that library's work and keeps its own time.
    """
    buckets = defaultdict(float)
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


def top_functions(stats, n):
    rows = []
    for (filename, lineno, funcname), (_cc, nc, tottime, cumtime, _cal) in stats.stats.items():
        short = Path(filename).name if filename not in ("~", "<string>") else filename
        rows.append((nc, tottime, cumtime, f"{short}:{lineno}({funcname})"))
    rows.sort(key=lambda r: -r[1])
    return rows[:n]


def print_rollup(stats, profiled_cpu, n_requests, real_cpu_ms, indent="    "):
    inflation = (profiled_cpu * 1000 / n_requests) / real_cpu_ms if real_cpu_ms else 0
    print(f"{indent}profiled {profiled_cpu * 1000 / n_requests:.3f} ms/req vs "
          f"{real_cpu_ms:.3f} unprofiled ({inflation:.1f}x instrumentation overhead) "
          f"— read shares, not ms")
    print(f"{indent}generic C builtins attributed to their callers\n")
    print(f"{indent}{'library':<22}{'share':>8}{'flat':>8}")
    print(f"{indent}{'-' * 38}")
    flat = dict(rollup(stats, attribute_builtins=False))
    for lib, secs in rollup(stats):
        if secs / profiled_cpu < 0.005:
            continue
        print(f"{indent}{lib:<22}{secs / profiled_cpu * 100:>7.1f}%"
              f"{flat.get(lib, 0) / profiled_cpu * 100:>7.1f}%")


def print_top(stats, n, indent="    "):
    print(f"\n{indent}Top {n} functions by self CPU:\n")
    print(f"{indent}{'ncalls':>10}{'tot ms':>9}{'cum ms':>9}  where")
    print(f"{indent}{'-' * 74}")
    for ncalls, tot, cum, where in top_functions(stats, n):
        print(f"{indent}{ncalls:>10}{tot * 1000:>9.1f}{cum * 1000:>9.1f}  {where[:60]}")
