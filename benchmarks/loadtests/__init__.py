"""One locustfile per load-test case — "each contender is a locust file".

Each module here defines a `locust.User` subclass (via
`benchmarks.load.locust.CaseUser`) and a module-level `CASE` constant naming
the contender slug it targets (matching `harness/registry.py`'s
`{backend}-{shape}-{name}` scheme, e.g. `"sqlite-flat-rowform"`). `locust -f
<file>` targets exactly one case, no env-var indirection for *which* route.

`benchmarks/load/registry.py` discovers cases by scanning this package for
modules that define `CASE` — a module without one (`_noop.py`, the framework
floor `bench load` hits for its headroom check) is not a case and is skipped.

This is a separate registry from `harness/registry.py` (which `bench micro`
and the FastAPI worker use to build routes) by design: the worker's routes
are generated once, in Python, from the compiled hydrator/query objects that
actually serve a request; a load-test case only needs to know which URL path
to hit, a much thinner concern that doesn't need the same machinery — and one
the two registries can, in principle, drift out of sync on (adding a new
`harness/registry.py` contender doesn't automatically get a loadtest file;
see PLAN.md for that trade-off).
"""
