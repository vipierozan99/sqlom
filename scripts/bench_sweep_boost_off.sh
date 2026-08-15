#!/usr/bin/env bash
# One full recorded benchmark sweep with CPU boost/turbo disabled — the run
# the RUNS.md calibration log asks for (`quotable` has only ever failed on the
# boost clause, and one contender has drifted between boost-on sweeps).
#
#     sudo scripts/bench_sweep_boost_off.sh
#
# Run it via sudo from your own user: root is needed only for the sysfs knobs,
# and every benchmark command runs as $SUDO_USER (uv env, docker group, and
# the recorded artifacts' ownership all belong to you, not root).
#
# What it does, in order:
#   1. disables turbo/boost (intel_pstate/no_turbo, else cpufreq/boost) and
#      pins every core's governor to `performance` — both restored on exit,
#      success or failure, Ctrl-C included;
#   2. gates on `bench env check` (dirty tree, loadavg, and the boost state
#      itself — if the knob didn't take, this aborts before spending 30 min);
#   3. runs the full publishing matrix: flat/join/wide on sqlite and postgres,
#      flat/join on mock, `--iterations 1500 --warmup 200 --trials 3
#      --isolate --record`;
#   4. renders the tables from exactly the runs this sweep produced and prints
#      the quotable verdicts.
#
# It does NOT commit anything: pick the runs, commit them to a dated `bench/`
# branch, and index them in docs/RUNS.md — the publishing ritual stays by hand.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "run as root: sudo $0" >&2
    exit 1
fi
BENCH_USER="${SUDO_USER:-}"
if [[ -z "$BENCH_USER" || "$BENCH_USER" == "root" ]]; then
    echo "invoke via sudo from your own account — the benchmarks must not run as root" >&2
    exit 1
fi
REPO="$(cd "$(dirname "$0")/.." && pwd)"
RUNS_DIR="$REPO/benchmarks/results/runs"

as_user() {
    runuser -u "$BENCH_USER" -- bash -lc "cd '$REPO' && $*"
}

if ! as_user "command -v uv" >/dev/null; then
    echo "uv is not on $BENCH_USER's login-shell PATH" >&2
    exit 1
fi

# --- 1. boost off + performance governor, restored on any exit ------------
NO_TURBO=/sys/devices/system/cpu/intel_pstate/no_turbo
BOOST=/sys/devices/system/cpu/cpufreq/boost
restore_cmds=()
restore() {
    local cmd
    for cmd in "${restore_cmds[@]}"; do eval "$cmd" || true; done
}
trap restore EXIT

if [[ -w $NO_TURBO ]]; then
    prev=$(cat "$NO_TURBO")
    restore_cmds+=("echo $prev > $NO_TURBO")
    echo 1 > "$NO_TURBO"
    echo "turbo disabled via intel_pstate/no_turbo (was $prev)"
elif [[ -w $BOOST ]]; then
    prev=$(cat "$BOOST")
    restore_cmds+=("echo $prev > $BOOST")
    echo 0 > "$BOOST"
    echo "boost disabled via cpufreq/boost (was $prev)"
else
    echo "no writable boost/turbo knob found — is this box neither intel_pstate nor acpi-cpufreq?" >&2
    exit 1
fi

for gov in /sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_governor; do
    prev=$(cat "$gov")
    restore_cmds+=("echo $prev > $gov")
    echo performance > "$gov"
done
echo "governors pinned to performance"

# --- 2. the suite's own pre-flight, which now includes the boost state -----
if ! as_user "uv run --all-groups python -m benchmarks env check" >/dev/null; then
    echo >&2
    echo "bench env check still warns — run it yourself to see why (dirty tree," >&2
    echo "high loadavg, or the boost knob not taking effect) and retry" >&2
    exit 1
fi
echo "env check clean — boost is off as far as the harness is concerned"

# --- 3. the publishing matrix ----------------------------------------------
before=$(ls -d "$RUNS_DIR"/*/ 2>/dev/null || true)

for shape in flat join wide; do
    as_user "uv run --all-groups python -m benchmarks micro run --shape $shape \
        --iterations 1500 --warmup 200 --trials 3 --isolate --record"
done
for shape in flat join; do
    as_user "uv run --all-groups python -m benchmarks micro run --shape $shape --backend mock \
        --iterations 1500 --warmup 200 --trials 3 --isolate --record"
done

as_user "uv run --all-groups python -m benchmarks db up"
restore_cmds+=("runuser -u '$BENCH_USER' -- bash -lc 'cd \"$REPO\" && uv run --all-groups python -m benchmarks db down' >/dev/null 2>&1")
DSN=$(as_user "uv run --all-groups python -m benchmarks db dsn" | tail -1)
for shape in flat join wide; do
    as_user "uv run --all-groups python -m benchmarks micro run --shape $shape --backend postgres \
        --iterations 1500 --warmup 200 --trials 3 --isolate --record --pg-dsn '$DSN'"
done
as_user "uv run --all-groups python -m benchmarks db down"

# --- 4. render exactly this sweep's runs ------------------------------------
after=$(ls -d "$RUNS_DIR"/*/ 2>/dev/null || true)
new_runs=$(comm -13 <(echo "$before") <(echo "$after") | sed 's:$:run.json:')
if [[ -z "$new_runs" ]]; then
    echo "no new runs recorded?" >&2
    exit 1
fi

echo
echo "=== tables from this sweep ==="
as_user "uv run python scripts/publish_tables.py $(echo "$new_runs" | tr '\n' ' ')"
echo
echo "=== quotable verdicts ==="
for run in $new_runs; do
    as_user "uv run python -c \"import json; r = json.load(open('$run')); print(r['run_id'], 'quotable =', r['quotable'], r['warnings'] or '')\""
done
echo
echo "next: commit the new run directories to a dated bench/ branch and index"
echo "them in docs/RUNS.md — this script deliberately does not do that for you."
