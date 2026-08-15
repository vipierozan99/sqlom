"""The load-test wiring is three names that must agree per case: the harness
registry slug (`benchmarks/harness/registry.py`), the loadtest module's `CASE`
constant (`benchmarks/loadtests/`), and the FastAPI route path
(`benchmarks/service/app.py`). `bench load run` resolves a case in both
registries and the locustfile drives traffic at `/{CASE}`, so any disagreement
makes that case unrunnable — which is exactly what happened to 7 of 16 cases
before this test existed (invented `raw-*`/`*-async-*` spellings).

The loadtests and app.py are parsed with `ast` rather than imported: importing
a loadtest imports locust, which runs `gevent.monkey.patch_all()` (poisoning
the test process), and app.py needs fastapi, which the default test
environment doesn't install (it lives in the `bench` dependency group).
"""

import ast
from pathlib import Path

BENCHMARKS = Path(__file__).resolve().parent.parent / "benchmarks"


def _loadtest_cases() -> dict[str, str]:
    """`{CASE slug: module stem}` for every module under `benchmarks/loadtests/`
    that declares one (`_noop.py` doesn't — it isn't a case)."""
    cases: dict[str, str] = {}
    for path in sorted((BENCHMARKS / "loadtests").glob("*.py")):
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "CASE" for target in node.targets
            ):
                assert isinstance(node.value, ast.Constant)
                cases[node.value.value] = path.stem
    return cases


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


def _harness_registry():
    import benchmarks.micro.contenders  # noqa: F401 -- @contender registration side-effects
    from benchmarks.harness import registry

    return registry.REGISTRY


def test_every_load_case_resolves_in_the_harness_registry():
    registry = _harness_registry()
    cases = _loadtest_cases()
    assert cases, "no loadtest cases discovered — the ast scan is broken"
    missing = sorted(slug for slug in cases if slug not in registry)
    assert not missing, (
        f"loadtest CASE slugs with no harness contender (bench load run would "
        f"refuse them): {missing}"
    )
    unprovisionable = sorted(
        slug for slug in cases if registry[slug].backend not in ("sqlite", "postgres")
    )
    assert not unprovisionable, (
        f"loadtest cases naming contenders bench load cannot provision: {unprovisionable}"
    )


def test_every_load_case_has_a_service_route():
    routes = _app_route_paths()
    missing = sorted(slug for slug in _loadtest_cases() if f"/{slug}" not in routes)
    assert not missing, f"loadtest cases with no matching app.py route: {missing}"


def test_loadtest_filename_matches_its_case():
    mismatched = {
        slug: stem
        for slug, stem in _loadtest_cases().items()
        if stem != slug.replace("-", "_")
    }
    assert not mismatched, (
        f"loadtest filename disagrees with its CASE slug (this drift is how "
        f"cases went unrunnable before): {mismatched}"
    )
