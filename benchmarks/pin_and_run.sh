#!/usr/bin/env bash
# Run the load benchmark with Postgres and the client pinned to disjoint cores.
#
# Why: in an unpinned run on one box, client and server compete for the same
# cores, so a client that burns less CPU leaves more for Postgres and gets
# faster queries in return. That compounds an efficient client's advantage and
# flatters it. Pinning to disjoint sets removes the feedback loop: Postgres
# gets a fixed budget no matter how wasteful the client is.
#
# CPU affinity is inherited across fork(), so pinning the postmaster is enough
# for backends started afterwards. Existing backends are pinned explicitly.
# Pooled connections opened before pinning would keep the old mask, so this
# must run before the benchmark creates its pool (it does).
#
# Usage:
#   benchmarks/pin_and_run.sh [--db-cores 2,3] [--client-cores 0,1] -- <bench args>
#
# Example:
#   benchmarks/pin_and_run.sh -- --limit 100 --concurrency 1,8,32 --duration 4

set -euo pipefail

DB_CORES="2,3"
CLIENT_CORES="0,1"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --db-cores)     DB_CORES="$2"; shift 2 ;;
        --client-cores) CLIENT_CORES="$2"; shift 2 ;;
        --)             shift; break ;;
        *)              echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

command -v taskset >/dev/null || { echo "taskset not found (install util-linux)" >&2; exit 1; }

# The postmaster is the postgres process whose parent is not itself postgres.
# Process substitution (not a pipe into `head`) keeps the assignment in this
# shell and avoids a SIGPIPE that `pipefail` would turn into an early exit.
POSTMASTER=""
while read -r pid ppid _; do
    parent="$(ps -o comm= -p "$ppid" 2>/dev/null || true)"
    if [[ "$parent" != "postgres" ]]; then
        POSTMASTER="$pid"
        break
    fi
done < <(ps -eo pid,ppid,comm | awk '$3=="postgres"')

if [[ -z "${POSTMASTER:-}" ]]; then
    echo "could not find the postgres postmaster; is the server running?" >&2
    exit 1
fi

echo "postmaster: pid $POSTMASTER"
echo "pinning postgres -> cores $DB_CORES"
for pid in "$POSTMASTER" $(pgrep -P "$POSTMASTER" || true); do
    taskset -a -cp "$DB_CORES" "$pid" >/dev/null 2>&1 || echo "  warn: could not pin $pid" >&2
done
# Any already-connected backends are grandchildren; catch them too.
for pid in $(pgrep -u postgres postgres || true); do
    taskset -a -cp "$DB_CORES" "$pid" >/dev/null 2>&1 || true
done

echo "postgres affinity now: $(taskset -cp "$POSTMASTER" | sed 's/.*list: //')"
echo "pinning client    -> cores $CLIENT_CORES"
echo

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec taskset -c "$CLIENT_CORES" python3 "$HERE/bench_pg_load.py" "$@"
