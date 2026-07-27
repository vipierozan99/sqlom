#!/usr/bin/env bash
# End-to-end FastAPI benchmark driven by locust, on a fully partitioned box.
#
# Why this exists: every end-to-end number in docs/BENCHMARKS.md came from
# benchmarks/httpload.py, a generator written for this repo. A benchmark whose
# load generator has no second opinion is one bug away from being wrong in a way
# nobody can see. Locust shares no code with httpload.py, so agreement between
# the two is real evidence and disagreement is a bug worth finding.
#
# Core partition (4 cores, nothing shared):
#   core 0    uvicorn + the data layer under test
#   core 1    locust (and this script, so orchestration can never steal core 0)
#   cores 2,3 Postgres
#
# The client is the thing to distrust here. Locust on one core costs far more per
# request than httpload.py does, so it can hit its own ceiling before the server
# hits its. That is what the /noop endpoint is for: it does no database work, so
# if /noop does not come out well above every database endpoint, the client
# saturated and the run must be thrown away. The script checks this and says so.
#
# Usage:
#   benchmarks/bench_locust.sh [-u 8] [-t 10s] [-r 3] [--endpoints a,b,c]

set -euo pipefail

USERS=8
DURATION=10s
REPEAT=3
ENDPOINTS="/noop,/psy-sqlom,/psy-core,/psy-orm"
SERVER_CORE=0
CLIENT_CORE=1
DB_CORES="2,3"
PORT=8000

while [[ $# -gt 0 ]]; do
    case "$1" in
        -u|--users)     USERS="$2"; shift 2 ;;
        -t|--time)      DURATION="$2"; shift 2 ;;
        -r|--repeat)    REPEAT="$2"; shift 2 ;;
        --endpoints)    ENDPOINTS="$2"; shift 2 ;;
        --port)         PORT="$2"; shift 2 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --- Postgres on its own cores ------------------------------------------------
# Affinity is inherited across fork(), so pinning the postmaster covers backends
# started later; existing ones are pinned explicitly.
POSTMASTER=""
while read -r pid ppid _; do
    [[ "$(ps -o comm= -p "$ppid" 2>/dev/null || true)" != "postgres" ]] && { POSTMASTER="$pid"; break; }
done < <(ps -eo pid,ppid,comm | awk '$3=="postgres"')
[[ -n "$POSTMASTER" ]] || { echo "postgres is not running" >&2; exit 1; }
for pid in $(pgrep -u postgres postgres || true); do
    taskset -a -cp "$DB_CORES" "$pid" >/dev/null 2>&1 || true
done
echo "postgres  pid $POSTMASTER -> cores $(taskset -cp "$POSTMASTER" | sed 's/.*list: //')"

# --- uvicorn on core 0 --------------------------------------------------------
cd "$ROOT"
taskset -c "$SERVER_CORE" python3 -m uvicorn benchmarks.fastapi_app:app \
    --port "$PORT" --loop uvloop --http httptools --no-access-log \
    >"$WORK/uvicorn.log" 2>&1 &
UVICORN_WRAPPER=$!
cleanup() {
    kill "$UVICORN_WRAPPER" 2>/dev/null || true
    wait "$UVICORN_WRAPPER" 2>/dev/null || true
    rm -rf "$WORK"
}
trap cleanup EXIT

for _ in $(seq 1 60); do
    if curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$PORT/noop"; then break; fi
    sleep 0.5
done
curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/noop" || {
    echo "server did not come up:" >&2; cat "$WORK/uvicorn.log" >&2; exit 1; }

# The `python3 -m uvicorn` wrapper and the server can be separate pids; report
# the affinity of every python process in the tree so a mis-pin is visible
# rather than silently invalidating the run.
echo -n "uvicorn   "
for pid in $UVICORN_WRAPPER $(pgrep -P "$UVICORN_WRAPPER" || true); do
    echo -n "pid $pid -> core $(taskset -cp "$pid" | sed 's/.*list: //')  "
done
echo
echo "locust    -> core $CLIENT_CORE"
echo

# --- Output equivalence -------------------------------------------------------
# If two endpoints return different bytes, their throughput is not comparable.
echo "response sizes:"
declare -A SIZE
for ep in ${ENDPOINTS//,/ }; do
    n=$(curl -s "http://127.0.0.1:$PORT$ep" | wc -c)
    SIZE[$ep]=$n
    echo "  $ep  $n bytes"
done
DB_SIZES=$(for ep in ${ENDPOINTS//,/ }; do [[ "$ep" == "/noop" ]] || echo "${SIZE[$ep]}"; done | sort -u | wc -l)
if [[ "$DB_SIZES" -ne 1 ]]; then
    echo "FAIL: database endpoints do not return identical payload sizes" >&2
    exit 1
fi
echo "  -> all database endpoints agree"
echo

# --- Measure ------------------------------------------------------------------
# Both generators drive the same uvicorn process in the same session, so a
# difference between them cannot be blamed on server warmup, page cache or
# Postgres state — only on the generator.
RAW="$WORK/raw.tsv"
HRAW="$WORK/httpload.tsv"
: >"$RAW"; : >"$HRAW"
SECS="${DURATION%s}"
for ep in ${ENDPOINTS//,/ }; do
    expect="${SIZE[$ep]}"
    for i in $(seq 0 "$REPEAT"); do   # iteration 0 is a discarded warmup
        prefix="$WORK/$(echo "$ep" | tr -d /)-$i"
        LOCUST_PATH="$ep" LOCUST_EXPECT="$expect" \
        taskset -c "$CLIENT_CORE" locust -f "$HERE/locustfile.py" --headless \
            --host "http://127.0.0.1:$PORT" -u "$USERS" -r "$USERS" \
            -t "$([[ $i -eq 0 ]] && echo 5s || echo "$DURATION")" \
            --only-summary --csv "$prefix" --reset-stats \
            >"$prefix.log" 2>&1 || { echo "locust failed on $ep:" >&2; tail -20 "$prefix.log" >&2; exit 1; }
        [[ $i -eq 0 ]] && continue
        python3 - "$prefix" "$ep" >>"$RAW" <<'PY'
import csv, sys
prefix, ep = sys.argv[1], sys.argv[2]
with open(f"{prefix}_stats.csv") as fh:
    row = next(r for r in csv.DictReader(fh) if r["Name"] == "Aggregated")
print("\t".join([ep, row["Requests/s"], row["Average Response Time"],
                 row["50%"], row["95%"], row["99%"],
                 row["Request Count"], row["Failure Count"]]))
PY
    done
    # Same endpoint, same core, same server: the second opinion's second opinion.
    for i in $(seq 1 "$REPEAT"); do
        taskset -c "$CLIENT_CORE" python3 "$HERE/httpload.py" --port "$PORT" \
            --path "$ep" --connections "$USERS" --duration "$SECS" \
            | awk -v OFS='\t' '$1=="RESULT"{print $2,$3,$4,$5,$6,$7}' >>"$HRAW"
    done
    printf '  measured %s\n' "$ep"
done

echo
python3 - "$RAW" "$USERS" "$DURATION" "$REPEAT" "$HRAW" <<'PY'
import statistics, sys
from collections import defaultdict

raw, users, duration, repeat = sys.argv[1], int(sys.argv[2]), sys.argv[3], int(sys.argv[4])
hraw = sys.argv[5]
rows = defaultdict(list)
for line in open(raw):
    ep, rps, mean, p50, p95, p99, count, fail = line.rstrip("\n").split("\t")
    rows[ep].append((float(rps), float(mean), float(p50), float(p95), float(p99),
                     int(count), int(fail)))

hrows = defaultdict(list)
for line in open(hraw):
    ep, rps, mean, p50, p95, p99 = line.rstrip("\n").split("\t")
    hrows[ep].append((float(rps), float(mean), float(p50), float(p95), float(p99)))

print(f"locust  FastHttpUser  u={users}  t={duration}  median of {repeat}")
print("NOTE: locust rounds sub-100ms response times to whole ms, so its")
print("      percentiles are +/-1 ms. rps and mean are exact.\n")
print(f"{'endpoint':<14}{'rps':>8}{'mean ms':>10}{'p50':>7}{'p95':>7}{'p99':>7}{'fails':>7}")
print("-" * 60)
med = {}
for ep, trials in rows.items():
    m = [statistics.median(t[i] for t in trials) for i in range(5)]
    fails = sum(t[6] for t in trials)
    med[ep] = m[0]
    print(f"{ep:<14}{m[0]:>8.0f}{m[1]:>10.2f}{m[2]:>7.0f}{m[3]:>7.0f}{m[4]:>7.0f}{fails:>7}")
    if fails:
        print(f"  ^ {fails} failures: this row is not usable")

# Little's Law: in a closed loop with N users and no think time, throughput
# times mean latency must equal N. Anything else means the generator is not
# keeping N requests in flight, and every ratio derived from it is suspect.
print(f"\nconcurrency check (rps * mean latency, should be ~{users}):")
for ep, trials in rows.items():
    inflight = med[ep] * statistics.median(t[1] for t in trials) / 1000
    flag = "" if abs(inflight - users) / users < 0.1 else "   <-- NOT saturating"
    print(f"  {ep:<14}{inflight:>6.2f}{flag}")

if "/noop" in med:
    print("\nclient headroom (/noop does no database work; if it is not well")
    print("above the database endpoints, locust itself was the bottleneck):")
    for ep, rps in med.items():
        if ep == "/noop":
            continue
        ratio = med["/noop"] / rps
        verdict = "ok" if ratio >= 2 else "TOO CLOSE - client may be the limit"
        print(f"  /noop is {ratio:>5.2f}x {ep:<14} {verdict}")

db = {ep: r for ep, r in med.items() if ep != "/noop"}
if "/psy-sqlom" in db:
    print("\nratios, locust:")
    for ep, rps in db.items():
        if ep != "/psy-sqlom":
            print(f"  sqlom vs {ep:<12}{db['/psy-sqlom'] / rps:>6.2f}x")

# --- Cross-check against httpload.py -----------------------------------------
# Two independent generators, same server, same core, same run. Where they agree
# the number is a property of the server; where they diverge, one of them is
# measuring itself, and /noop shows which.
hmed = {ep: statistics.median(t[0] for t in trials) for ep, trials in hrows.items()}
if hmed:
    print(f"\n{'endpoint':<14}{'locust rps':>12}{'httpload rps':>14}{'delta':>9}")
    print("-" * 49)
    for ep in rows:
        if ep not in hmed:
            continue
        delta = (med[ep] - hmed[ep]) / hmed[ep] * 100
        print(f"{ep:<14}{med[ep]:>12.0f}{hmed[ep]:>14.0f}{delta:>8.1f}%")
    hdb = {ep: r for ep, r in hmed.items() if ep != "/noop"}
    if "/psy-sqlom" in hdb:
        print("\nratios, httpload:")
        for ep, rps in hdb.items():
            if ep != "/psy-sqlom":
                print(f"  sqlom vs {ep:<12}{hdb['/psy-sqlom'] / rps:>6.2f}x")
PY
