"""Load-test case registry — discovered from `benchmarks/loadtests/`, one
module per case, each a real locustfile. A different registry from
`harness/registry.py` (which `bench micro` and the FastAPI worker use); see
`benchmarks/loadtests/__init__.py` for why they're kept separate.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass

import benchmarks.loadtests

CASE_HELP = "a contender slug or 'all' — see `bench contenders list` or `bench load cases`"


@dataclass(frozen=True, slots=True)
class LoadCase:
    slug: str
    module: str  # dotted module path, for error messages
    file: str  # filesystem path — what `locust -f` takes


def discover() -> dict[str, LoadCase]:
    """Import every module in `benchmarks.loadtests` and collect the ones
    that declare a `CASE` slug — a module without one (`_noop.py`) isn't a
    contender case and is silently skipped, not an error."""
    cases: dict[str, LoadCase] = {}
    for info in pkgutil.iter_modules(benchmarks.loadtests.__path__, prefix="benchmarks.loadtests."):
        module = importlib.import_module(info.name)
        slug = getattr(module, "CASE", None)
        if slug is None:
            continue
        if slug in cases:
            raise ValueError(
                f"loadtest case slug {slug!r} is declared by both "
                f"{cases[slug].module!r} and {info.name!r}"
            )
        assert module.__file__ is not None
        cases[slug] = LoadCase(slug=slug, module=info.name, file=module.__file__)
    return cases


def get(slug: str) -> LoadCase:
    cases = discover()
    try:
        return cases[slug]
    except KeyError:
        known = ", ".join(sorted(cases))
        raise KeyError(f"no loadtest case {slug!r}; known: {known}") from None


def noop_file() -> str:
    """The framework-floor locustfile (`_noop.py`) — not a case (no `CASE`
    constant), so it's addressed directly rather than through `discover()`."""
    import benchmarks.loadtests._noop as noop_module

    assert noop_module.__file__ is not None
    return noop_module.__file__
