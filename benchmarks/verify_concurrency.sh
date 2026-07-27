#!/usr/bin/env bash
# Prove the load generators actually drive FastAPI concurrently.
#
# Every end-to-end ratio in docs/BENCHMARKS.md assumes the generator keeps N
# requests in flight. If it serialised them instead, throughput would be
# 1/latency for every contender, the ranking could invert, and nothing in the
# output would look wrong. So this checks it three independent ways rather than
# assuming it:
#
#   1. Direct observation. Count ESTABLISHED sockets on the server's port from
#      /proc/net/tcp while a run is in flight. With N connections requested there
#      must be exactly N. (`ss` is unavailable in some containers — it needs
#      netlink — so this reads /proc directly. Port is matched in the hex form
#      /proc/net/tcp uses, and state 01 is ESTABLISHED.)
#
#   2. Little's Law. In a closed loop with no think time, throughput x mean
#      latency must equal the number of in-flight requests. If it comes out at
#      ~1 regardless of N, the generator is serialising. If it comes out at N,
#      it is not.
#
#   3. Throughput scaling. A serialising generator cannot go faster with more
#      connections, because there is only ever one request outstanding. Real
#      concurrency shows rising throughput up to a knee and falling past it.
#
# Cores: server 0, generator 1, Postgres 2-3.
#
# Usage:
#   benchmarks/verify_concurrency.sh [--path /psy-sqlom] [--levels 1,2,4,8,16]

set -euo pipefail

PATH_UNDER_TEST="/psy-sqlom"
LEVELS="1,2,4,8,16"
DURATION=6
PORT=8000
SERVER_CORE=0
CLIENT_CORE=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --path)     PATH_UNDER_TEST="$2"; shift 2 ;;
        --levels)   LEVELS="$2"; shift 2 ;;
        --duration) DURATION="$2"; shift 2 ;;
        --port)     PORT="$2"; shift 2 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
LOG="$(mktemp)"

count_established() {
    # /proc/net/tcp stores local address as HEX_IP:HEX_PORT and state 01 is
    # ESTABLISHED. Counting server-side sockets (local port == our port) counts
    # accepted client connections, not the listening socket, which is state 0A.
    local hexport
    hexport=$(printf '%04X' "$PORT")
    awk -v p=":$hexport" 'NR>1 && $4=="01" && index($2, p) == length($2)-length(p)+1 {n++}
                          END {print n+0}' /proc/net/tcp
}

cd "$ROOT"
taskset -c "$SERVER_CORE" python3 -m uvicorn benchmarks.fastapi_app:app \
    --port "$PORT" --loop uvloop --http httptools --no-access-log >"$LOG" 2>&1 &
UV=$!
trap 'kill $UV 2>/dev/null || true; wait $UV 2>/dev/null || true; rm -f "$LOG"' EXIT

for _ in $(seq 1 60); do
    curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$PORT/noop" && break
    sleep 0.5
done
curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/noop" \
    || { echo "server did not start:" >&2; cat "$LOG" >&2; exit 1; }

echo "server pid $UV on core $(taskset -cp $UV | sed 's/.*list: //')   "\
     "generator on core $CLIENT_CORE   path $PATH_UNDER_TEST"
echo "baseline established sockets on :$PORT (before load): $(count_established)"
echo
printf '%-6s %-10s %10s %10s %12s\n' "conns" "sockets" "rps" "mean ms" "in flight"
printf '%.0s-' {1..52}; echo

# A verification script that prints a mismatch and still exits 0 is worse than
# no verification: the run gets recorded as evidence and nothing surfaces the
# failure. Every check below contributes to this status.
STATUS=0

for n in ${LEVELS//,/ }; do
    out="$(mktemp)"
    taskset -c "$CLIENT_CORE" python3 "$HERE/httpload.py" --port "$PORT" \
        --path "$PATH_UNDER_TEST" --connections "$n" --duration "$DURATION" \
        --warmup 1 >"$out" 2>&1 &
    hl=$!
    # Sample mid-run, after warmup has finished and all connections are open.
    sleep "$(python3 -c "print($DURATION / 2 + 1)")"
    sockets="$(count_established)"
    if ! wait "$hl"; then
        printf '%-6s %s\n' "$n" "GENERATOR FAILED: $(tail -1 "$out")"
        rm -f "$out"; STATUS=1; continue
    fi

    read -r _ _ rps mean _ _ _ _ < "$out"
    rm -f "$out"
    inflight=$(python3 -c "print(f'{$rps * $mean / 1000:.2f}')")
    notes=""
    if [[ "$sockets" != "$n" ]]; then
        notes+="  <-- sockets != $n"; STATUS=1
    fi
    # Little's Law within 10%: tighter would flag ordinary jitter, looser would
    # miss a generator running at half the requested concurrency.
    if ! python3 -c "import sys; sys.exit(0 if abs($inflight - $n) / $n <= 0.10 else 1)"; then
        notes+="  <-- in flight != $n"; STATUS=1
    fi
    printf '%-6s %-10s %10s %10s %12s%s\n' "$n" "$sockets" "$rps" "$mean" "$inflight" "$notes"
done

echo
echo "Reading this: 'sockets' must equal 'conns' (observation), 'in flight' must"
echo "equal 'conns' (Little's Law), and rps must rise with conns up to the knee."
echo "All three failing the same way would mean the generator is serialising."
if [[ "$STATUS" -ne 0 ]]; then
    echo
    echo "VERIFICATION FAILED — do not treat this run as evidence of concurrency."
else
    echo
    echo "All checks passed."
fi
exit "$STATUS"
