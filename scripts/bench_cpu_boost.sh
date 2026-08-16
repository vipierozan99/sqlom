#!/usr/bin/env bash
# Turn cpu boost/turbo off (or back on) — the one condition `bench env check`
# cannot satisfy without root, and the only reason a recorded sweep reports
# `quotable=False` on this box.
#
#     sudo scripts/bench_cpu_boost.sh off
#     <run the sweep as yourself — see docs/METHODOLOGY.md "Results">
#     sudo scripts/bench_cpu_boost.sh on
#
# Deliberately does *not* run the sweep. An earlier version of this file did the
# whole session — knobs, gate, matrix, tables — which meant the privileged part
# and the measured part could not be reviewed, rerun, or interrupted separately.
# The sweep is a handful of `bench micro run` lines that want no privileges;
# only these two writes do.
#
# It also leaves `scaling_governor` alone, which that earlier version pinned to
# `performance`. Measured reason: with turbo off this box's busy core already
# sits at `base_frequency` (1.9 GHz — the 2026-08-16 sweep recorded 1874-1894
# MHz on its pinned cores), so pinning would mostly raise the *idle* floor
# (`min_perf_pct` ~9%, ~400 MHz) during the awaits inside the timed region.
# Keeping the stock governor means the published numbers come from the machine
# as it normally runs. If that assumption is ever worth retesting, change the
# governor by hand and record a sweep against it rather than folding it in here.
set -euo pipefail

usage() { echo "usage: sudo $0 {off|on|status}" >&2; exit 2; }
[[ $# -eq 1 ]] || usage

# A power daemon will undo the write below on its own schedule, and the harness
# only sampled boost at run *start*, so this was invisible: `tuned` (balanced)
# re-enabled turbo between two shapes of one sweep, and the affected runs
# recorded boost=False. `off` stops whichever daemon is active and `on` starts it
# again; `status` reports it, because "boost is off" is not the whole story if
# something is about to turn it back on.
DAEMONS=(tuned power-profiles-daemon thermald tlp auto-cpufreq)
STATE_DIR=/var/tmp/rowform-bench-cpu
STOPPED_FILE="$STATE_DIR/stopped-daemons"

active_daemons() {
    local d
    for d in "${DAEMONS[@]}"; do
        systemctl is-active --quiet "$d" 2>/dev/null && echo "$d"
    done
}

NO_TURBO=/sys/devices/system/cpu/intel_pstate/no_turbo   # inverted: 1 == boost off
BOOST=/sys/devices/system/cpu/cpufreq/boost              # direct:   1 == boost on

# `intel_pstate/no_turbo` and `cpufreq/boost` are never both present; which one
# exists depends on the scaling driver, and their polarities are opposite.
if [[ -e $NO_TURBO ]]; then
    knob=$NO_TURBO off_value=1 on_value=0 label="intel_pstate/no_turbo"
elif [[ -e $BOOST ]]; then
    knob=$BOOST off_value=0 on_value=1 label="cpufreq/boost"
else
    echo "no boost/turbo knob in sysfs — scaling driver is $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_driver 2>/dev/null || echo unknown)" >&2
    exit 1
fi

case $1 in
    status)
        current=$(cat "$knob")
        if [[ $current == "$off_value" ]]; then state="off"; else state="on"; fi
        echo "boost is $state ($label=$current)"
        running=$(active_daemons || true)
        if [[ -n $running ]]; then
            echo "WARNING: $(echo "$running" | tr '\n' ' ')active — expect boost to be re-applied mid-sweep"
        else
            echo "no power daemon active"
        fi
        exit 0
        ;;
    off) want=$off_value ;;
    on)  want=$on_value ;;
    *)   usage ;;
esac

if [[ ! -w $knob ]]; then
    echo "$knob is not writable — run under sudo" >&2
    exit 1
fi

# Stop the daemons before writing the knob (so nothing races the write), and
# start them again only after restoring it.
if [[ $1 == "off" ]]; then
    mkdir -p "$STATE_DIR"
    active_daemons > "$STOPPED_FILE"
    while read -r d; do
        [[ -n $d ]] || continue
        systemctl stop "$d"
        echo "stopped $d (restored by '$0 on')"
    done < "$STOPPED_FILE"
fi

echo "$want" > "$knob"
# Read back rather than trusting the write: some firmware silently ignores it.
got=$(cat "$knob")
if [[ $got != "$want" ]]; then
    echo "wrote $want to $label but it reads $got — firmware or driver refused it" >&2
    exit 1
fi
echo "boost $1 ($label=$got)"

if [[ $1 == "on" && -f $STOPPED_FILE ]]; then
    while read -r d; do
        [[ -n $d ]] || continue
        systemctl start "$d" && echo "restarted $d"
    done < "$STOPPED_FILE"
    rm -f "$STOPPED_FILE"
fi

if [[ $1 == "off" ]]; then
    remaining=$(active_daemons || true)
    if [[ -n $remaining ]]; then
        echo "WARNING: still active: $(echo "$remaining" | tr '\n' ' ')" >&2
    fi
    echo "now run 'just bench env check' as yourself; it must print 'no warnings'"
fi
