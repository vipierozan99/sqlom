"""Load-test case registry — derived from the FastAPI app's own route table.

A case *is* a `service/app.py` route whose path names a harness contender
slug (`/{backend}-{shape}-{name}`). The hand-written routes are the ground
truth: adding a route adds a case, and a route whose path names no registered
contender fails discovery loudly instead of producing a case `bench load run`
would refuse later. This replaced a directory of one-boilerplate-locustfile-
per-case (each just `CASE = "<slug>"` + `path = f"/{CASE}"`), whose hand-
copied slugs drifted from the harness registry until 7 of 16 cases were
unrunnable.

Which route the (single) locustfile drives comes from `LoadCase.route`,
passed through `locust.run(route=...)` as an env var — see
`benchmarks/load/locustfile.py`.
"""

from __future__ import annotations

from dataclasses import dataclass

import benchmarks.micro.contenders  # noqa: F401 -- @contender registration side-effects
from benchmarks.harness import registry as harness_registry

CASE_HELP = (
    "a contender slug ('{backend}-{shape}-{name}'), or 'all'/'sqlite'/'postgres' "
    "to sweep a group — see `bench load cases`"
)

#: Only paths shaped like a contender slug are candidate cases — everything
#: else (`/noop`, FastAPI's own `/openapi.json`/`/docs`/...) is not a case.
_CASE_PREFIXES = ("/sqlite-", "/postgres-", "/mock-")


@dataclass(frozen=True, slots=True)
class LoadCase:
    slug: str
    route: str  # the service/app.py path locust drives traffic at


def discover() -> dict[str, LoadCase]:
    """One `LoadCase` per case route in `service/app.py`, keyed by slug.

    Imports the app lazily: fastapi lives in the `bench` dependency group, and
    importing it at module scope would break every consumer that only wants
    `CASE_HELP` (or a test environment without the group installed).
    """
    from benchmarks.service.app import app as service_app

    cases: dict[str, LoadCase] = {}
    for route in service_app.routes:
        path = getattr(route, "path", "")
        if not path.startswith(_CASE_PREFIXES):
            continue
        slug = path.lstrip("/")
        if slug not in harness_registry.REGISTRY:
            raise ValueError(
                f"service route {path!r} names no registered contender — a case "
                f"route's path must equal a harness slug (`bench contenders list`)"
            )
        cases[slug] = LoadCase(slug=slug, route=path)
    return dict(sorted(cases.items()))


def get(slug: str) -> LoadCase:
    cases = discover()
    try:
        return cases[slug]
    except KeyError:
        known = ", ".join(sorted(cases))
        raise KeyError(f"no loadtest case {slug!r}; known: {known}") from None


def locustfile() -> str:
    """The one locustfile every case runs — filesystem path, what `locust -f`
    takes. Which route it hits is per-run (`locust.run(route=...)`), not
    per-file."""
    import benchmarks.load.locustfile as module

    assert module.__file__ is not None
    return module.__file__
