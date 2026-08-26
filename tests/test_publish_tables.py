"""`scripts/publish_tables.py` — the script that turns recorded runs into the
tables checked into README.md and METHODOLOGY.md.

It had no tests, which is how three silent-wrongness bugs got into a tool whose
entire purpose is to stop numbers being transcribed by hand. Each test below is
one of them: a missing ratio published as a tie, a contender dropped for being
absent from a constant, and cells from two different commits merged into one
table. All three produce a table that looks finished and is wrong, which is the
failure mode a publishing script must not have.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import publish_tables as pt


def _cell(contender, median, *, backend="sqlite", gc="off", trials=2, limit=1000):
    return {
        "contender": contender,
        "params": {"backend": backend, "gc": gc, "limit": limit},
        "summary": {
            "median_ms": {"median": median, "spread_pct": 1.0},
            "p95_over_p50": {"max": 1.1},
            "max_over_p50": {"max": 1.2},
        },
        "trials": [{"metrics": {"p95_over_p50": 1.1, "max_over_p50": 1.2}}] * trials,
    }


def _run(cells, ratios, *, sha="abc123456", shape="flat"):
    return {
        "config": {"shape": shape},
        "git": {"sha": sha},
        "quotable": True,
        "cells": cells,
        "ratios": ratios,
    }


def _write(tmp_path, run, name="run.json"):
    path = tmp_path / name
    path.write_text(json.dumps(run))
    return path


def _ratio(contender, value, *, against="rowform", gc="off", tie=False):
    return {"contender": contender, "against": against, "gc": gc, "value": value, "tie": tie}


class TestAMissingRatioIsNotATie:
    """`--trials 1` records no ratios at all — `_ratios_for` needs two to bound
    one. Defaulting those to 1.0 rendered a 12x spread as a three-way tie."""

    def test_it_renders_an_em_dash(self, tmp_path):
        run = _run([_cell("rowform", 0.27), _cell("SQLAlchemy ORM", 3.21)], ratios=[])
        table = pt.load([_write(tmp_path, run)])
        rendered = pt.render(table, "sqlite")
        assert "1.00x" not in rendered
        assert rendered.count("—") >= 2

    def test_the_reference_still_reads_as_1x(self, tmp_path):
        """The reference has no ratio *record* either — it is the denominator.
        That is the one case where a missing entry does mean 1.00x, and the
        `against` field on the other records is what tells them apart."""
        run = _run(
            [_cell("rowform", 0.27), _cell("SQLAlchemy ORM", 3.21)],
            ratios=[_ratio("SQLAlchemy ORM", 11.9)],
        )
        rendered = pt.render(pt.load([_write(tmp_path, run)]), "sqlite")
        assert "**1.00x**" in rendered
        assert "11.90x" in rendered


class TestAnUnknownContenderIsNotDropped:
    def test_it_refuses_rather_than_omitting_the_row(self, tmp_path):
        """Registering a contender and forgetting `ROW_ORDER` used to delete it
        from the table with no sign it had ever run."""
        run = _run(
            [_cell("rowform", 0.27), _cell("brand new contender", 0.31)],
            ratios=[_ratio("brand new contender", 1.15)],
        )
        table = pt.load([_write(tmp_path, run)])
        with pytest.raises(SystemExit, match="brand new contender"):
            pt.render(table, "sqlite")


class TestOneTableIsOneCommit:
    def test_runs_from_two_shas_are_refused(self, tmp_path):
        """"Later runs win" is per contender, so re-running one cell and
        re-globbing over both leaves every other row at the older commit's
        numbers — in one table, with nothing saying so."""
        old = _run([_cell("rowform", 0.27)], ratios=[], sha="1111111aa")
        new = _run([_cell("rowform", 0.19)], ratios=[], sha="2222222bb")
        paths = [_write(tmp_path, old, "a.json"), _write(tmp_path, new, "b.json")]
        with pytest.raises(SystemExit, match="more than one commit"):
            pt.load(paths)

    def test_one_sha_across_several_runs_is_fine(self, tmp_path):
        one = _run([_cell("rowform", 0.27)], ratios=[], shape="flat")
        two = _run([_cell("rowform", 1.82)], ratios=[], shape="join")
        table = pt.load([_write(tmp_path, one, "a.json"), _write(tmp_path, two, "b.json")])
        assert set(table) == {("sqlite", "flat", 1000), ("sqlite", "join", 1000)}


class TestGcFiltering:
    def test_the_unselected_mode_is_not_merged_in(self, tmp_path):
        """A `--gc both` sweep records each contender twice. Keying the table by
        contender alone let one mode overwrite the other silently."""
        run = _run(
            [_cell("rowform", 0.30, gc="off"), _cell("rowform", 0.28, gc="on")],
            ratios=[],
        )
        path = _write(tmp_path, run)
        assert pt.load([path], gc="off")[("sqlite", "flat", 1000)]["rowform"]["median"] == 0.30
        assert pt.load([path], gc="on")[("sqlite", "flat", 1000)]["rowform"]["median"] == 0.28

    def test_a_mode_that_was_never_recorded_is_an_error(self, tmp_path):
        run = _run([_cell("rowform", 0.30, gc="off")], ratios=[])
        with pytest.raises(SystemExit, match="no cells with gc"):
            pt.load([_write(tmp_path, run)], gc="on")


class TestTwoReadSizesAreTwoColumns:
    """One shape recorded at two rows-per-read is two measurements, not one. Keyed
    by shape alone the second silently overwrote the first — and since a 1-row
    read is ~10x faster than a 1000-row one, the survivor looked like a result
    rather than like a different question."""

    def test_the_small_read_does_not_overwrite_the_big_one(self, tmp_path):
        big = _run([_cell("rowform", 2.66, limit=1000)], ratios=[])
        small = _run([_cell("rowform", 0.22, limit=1)], ratios=[])
        table = pt.load([_write(tmp_path, big, "a.json"), _write(tmp_path, small, "b.json")])
        assert table[("sqlite", "flat", 1000)]["rowform"]["median"] == 2.66
        assert table[("sqlite", "flat", 1)]["rowform"]["median"] == 0.22

    def test_the_columns_say_which_read_they_are(self, tmp_path):
        big = _run([_cell("rowform", 2.66, limit=1000)], ratios=[])
        small = _run([_cell("rowform", 0.22, limit=1)], ratios=[])
        table = pt.load([_write(tmp_path, big, "a.json"), _write(tmp_path, small, "b.json")])
        assert pt.columns(table, "sqlite") == [("flat", 1000), ("flat", 1)]
        assert "| flat @1000 | flat @1 |" in pt.render(table, "sqlite")

    def test_one_read_size_still_renders_bare_shape_names(self, tmp_path):
        run = _run([_cell("rowform", 2.66, limit=1000)], ratios=[])
        rendered = pt.render(pt.load([_write(tmp_path, run)]), "sqlite")
        assert "| contender | flat | | flat |" in rendered


class TestItKnowsEveryContender:
    def test_every_registered_name_is_in_row_order_and_labels(self):
        """`render` refuses at publish time (see above). This refuses at test
        time, which is where the person who just registered a contender is
        looking — and it is a `SystemExit` in a script nobody runs until they are
        publishing a table, so the gap between the two is a whole sweep long.
        """
        import benchmarks.micro.contenders  # noqa: F401 -- registration side-effects
        from benchmarks.harness import registry

        names = {spec.name for spec in registry.REGISTRY.values()}
        assert sorted(names - set(pt.ROW_ORDER)) == []
        assert sorted(names - set(pt.LABELS)) == []
