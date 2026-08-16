"""Regression tests for the benchmark harness's measurement primitives —
each pins a fix to a bug that made recorded numbers wrong or gates vacuous
(wrong nearest-rank percentile, negative CPU deltas from exited pids, monitor
averages diluted by post-exit samples, a boost gate that silently passed when
the sysfs knob was unreadable, an equivalence gate that only re-ran the
reference contender, and profiler frame attribution crediting SQLAlchemy's
driver adapters to the raw driver).
"""

import math

import pytest

from benchmarks.harness import cpuacct, equivalence, stats
from benchmarks.harness import env as env_module
from benchmarks.harness.monitor import ProcessMonitor
from benchmarks.profiling import attribution

# --- stats ------------------------------------------------------------------


def test_percentile_is_true_nearest_rank():
    values = [1, 2, 3, 4]
    # ceil(4 * 50/100) = 2nd order statistic; the old floor-based index
    # returned the 3rd.
    assert stats.percentile(values, 50) == 2
    assert stats.percentile(values, 100) == 4
    assert stats.percentile(values, 1) == 1
    assert stats.percentile(list(range(1, 101)), 95) == 95
    assert math.isnan(stats.percentile([], 50))


def test_spread_pct_zero_median_with_nonzero_range_is_not_perfect():
    # Used to return 0.0 — "perfectly reproducible" — for a zero median over
    # a nonzero range.
    assert math.isnan(stats.spread_pct([-1.0, 0.0, 1.0]))
    assert stats.spread_pct([0.0, 0.0]) == 0.0
    assert stats.spread_pct([10.0, 11.0, 12.0]) == pytest.approx((12 - 10) / 11 * 100)


def test_ratio_interval_key_is_not_named_like_the_trial_spread():
    ratio = stats.ratio_with_spread([2.0, 2.1], [1.0, 1.05])
    assert "interval_pct" in ratio
    assert "spread_pct" not in ratio


# --- cpuacct ----------------------------------------------------------------


def test_cpu_accountant_clamps_exited_pids(monkeypatch):
    readings = {1: 100.0, 2: 5.0}

    monkeypatch.setattr(cpuacct, "read_pid_cpu_seconds", lambda pid: readings[pid])
    accountant = cpuacct.CpuAccountant({"server": [1, 2]})
    accountant.start()
    # pid 1 exits (reads 0.0 — /proc entry gone); pid 2 does 3 cpu-seconds.
    readings.update({1: 0.0, 2: 8.0})
    utilization = accountant.stop(elapsed_s=10.0)
    # The old sum-based delta reported (0 + 8 - 105) / 10 = -9.7.
    assert utilization["server"] == pytest.approx(0.3)


def test_read_pid_cpu_seconds_handles_paren_in_comm(tmp_path, monkeypatch):
    # A comm containing ") " used to misalign the field split at the first
    # closing paren.
    stat = tmp_path / "stat"
    fields = ["4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "300", "500"]
    stat.write_text("123 (a) b) R " + " ".join(fields) + " 0 0\n")
    monkeypatch.setattr(cpuacct, "Path", lambda _: stat)
    ticks = cpuacct._CLOCK_TICKS
    assert cpuacct.read_pid_cpu_seconds(123) == pytest.approx((300 + 500) / ticks)


# --- monitor ----------------------------------------------------------------


def test_monitor_averages_ignore_gaps_and_keep_untracked_roles():
    monitor = ProcessMonitor()
    monitor.samples = [
        {"t": 1.0, "locust": 0.8, "server": 0.5},
        {"t": 2.0, "locust": 0.6, "server": 0.5},
        {"t": 3.0, "server": 0.5},  # locust exited — no 0% dilution
    ]
    averages = monitor.averages()
    assert averages["locust"] == pytest.approx(0.7)
    assert averages["server"] == pytest.approx(0.5)


# --- env gates --------------------------------------------------------------


def _env(boost, **overrides):
    base = {
        "cpu": {"boost": boost, "physical_cores": 8},
        "python": {"gevent_monkey_patched": False},
        "git": {"dirty": False},
        "loadavg": [0.0, 0.0, 0.0],
    }
    base.update(overrides)
    return base


def test_boost_unknown_does_not_silently_pass_the_gate():
    assert any("boost" in w for w in env_module.warnings_for(_env(True)))
    # None is "unknown", which must warn — on the drivers this capture can't
    # read, boost is usually on.
    assert any("boost" in w for w in env_module.warnings_for(_env(None)))
    assert not any("boost" in w for w in env_module.warnings_for(_env(False)))


def test_throttle_delta_and_gevent_patch_warn():
    env = _env(False)
    env["cpu"]["throttle_count_delta"] = 3
    env["python"]["gevent_monkey_patched"] = True
    warnings = env_module.warnings_for(env)
    assert any("throttle" in w for w in warnings)
    assert any("gevent" in w for w in warnings)


# --- equivalence ------------------------------------------------------------


async def test_equivalence_catches_flaky_non_reference_contender():
    flaky_payloads = iter([b'[{"id": 1}]', b'[{"id": 1}]', b'[{"id": 2}]'])

    async def steady() -> bytes:
        return b'[{"id": 1}]'

    async def flaky() -> bytes:
        return next(flaky_payloads, b'[{"id": 3}]')

    result = await equivalence.check({"reference": steady, "other": flaky})
    # `flaky` matched the reference on its first call; only re-running every
    # contender (not just the reference) catches it.
    assert not result.self_consistent
    assert any("other" in failure for failure in result.failures)


# --- profiler attribution ---------------------------------------------------


def test_sqlalchemy_driver_adapters_are_not_credited_to_the_driver():
    sa = "/venv/lib/python3.12/site-packages/sqlalchemy"
    assert attribution.categorize(f"{sa}/dialects/sqlite/aiosqlite.py", "execute").startswith(
        "SQLAlchemy"
    )
    assert attribution.categorize(f"{sa}/dialects/postgresql/psycopg.py", "connect").startswith(
        "SQLAlchemy"
    )
    site = "/venv/lib/python3.12/site-packages"
    assert attribution.categorize(f"{site}/aiosqlite/core.py", "fetchall") == "sqlite3 driver"
    assert (
        attribution.categorize(
            "~", "<method 'execute' of 'sqlite3.Connection' objects>"
        )
        == "sqlite3 driver"
    )
    assert attribution.categorize(f"{site}/asyncpg/connection.py", "fetch") == "asyncpg"
    assert attribution.categorize(f"{site}/psycopg/cursor.py", "execute") == "psycopg"
