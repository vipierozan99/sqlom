"""Load-test case identity: a case *is* a `service/app.py` route whose path
names a harness contender slug (`load/registry.py` derives the case list from
the app's route table). These tests pin the invariants that make every case
runnable: each case route resolves to a provisionable harness contender, and
each served backend/shape has the rowform reference route `bench load run`'s
HTTP equivalence gate compares against. Before this was mechanical, the
hand-copied slugs of per-case locustfiles drifted until 7 of 16 cases were
unrunnable.

app.py is parsed with `ast` rather than imported: it needs fastapi, which the
default test environment doesn't install (it lives in the `bench` dependency
group).
"""

import ast
from pathlib import Path

BENCHMARKS = Path(__file__).resolve().parent.parent / "benchmarks"

_CASE_PREFIXES = ("sqlite-", "postgres-", "mock-")


def _app_route_paths() -> set[str]:
    """Every `@app.get("<path>")` literal in `benchmarks/service/app.py`."""
    paths: set[str] = set()
    for node in ast.walk(ast.parse((BENCHMARKS / "service" / "app.py").read_text())):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            paths.add(node.args[0].value)
    return paths


def _case_slugs() -> set[str]:
    return {
        path.lstrip("/")
        for path in _app_route_paths()
        if path.lstrip("/").startswith(_CASE_PREFIXES)
    }


def _harness_registry():
    import benchmarks.micro.contenders  # noqa: F401 -- @contender registration side-effects
    from benchmarks.harness import registry

    return registry.REGISTRY


def test_every_case_route_resolves_in_the_harness_registry():
    registry = _harness_registry()
    slugs = _case_slugs()
    assert slugs, "no case routes discovered in service/app.py — the ast scan is broken"
    missing = sorted(slug for slug in slugs if slug not in registry)
    assert not missing, (
        f"service routes naming no harness contender (bench load run would "
        f"refuse them): {missing}"
    )
    unprovisionable = sorted(
        slug for slug in slugs if registry[slug].backend not in ("sqlite", "postgres")
    )
    assert not unprovisionable, (
        f"case routes naming contenders bench load cannot provision: {unprovisionable}"
    )


def test_every_served_backend_shape_has_its_rowform_reference_route():
    registry = _harness_registry()
    routes = _app_route_paths()
    missing = sorted(
        f"/{spec.backend}-{spec.shape}-rowform"
        for spec in (registry[slug] for slug in _case_slugs())
        if f"/{spec.backend}-{spec.shape}-rowform" not in routes
    )
    assert not missing, (
        f"bench load's HTTP equivalence gate byte-compares every case against "
        f"the rowform route for its backend/shape; these are missing: {missing}"
    )
