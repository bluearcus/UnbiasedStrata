#!/bin/bash
# tools/wait_for.sh TIMEOUT_S PATTERN FILE...
#
# Waits until PATTERN appears in any FILE, or TIMEOUT_S elapses.  Exits the moment it appears (1 s granularity)
# and prints how long that took, so a caller never guesses a duration and never over-sleeps.
#
# Every script in tools/ ends with a terminal marker (BENCH_DONE, SWEEP_DONE, SOAK_DONE, PROBE_DONE, ...) and the
# pull scripts write STATUS files (RUNNING/DONE/FAILED), so this covers both patterns:
#
#   tools/wait_for.sh 1800 BENCH_DONE /home/mike/models/bench_out.log
#   tools/wait_for.sh 3600 DONE       /home/mike/models/flash-next/STATUS_S
#
# Use it instead of `sleep N && cat log`.  Keep the tool call's own timeout a little above TIMEOUT_S.
set -u
T=${1:?usage: wait_for.sh TIMEOUT_S PATTERN FILE...}
PAT=${2:?usage: wait_for.sh TIMEOUT_S PATTERN FILE...}
shift 2
[ $# -gt 0 ] || { echo "wait_for.sh: no file given" >&2; exit 2; }
start=$(date +%s)
while :; do
  for f in "$@"; do
    if [ -f "$f" ] && grep -q -- "$PAT" "$f" 2>/dev/null; then
      echo "matched '$PAT' in $f after $(( $(date +%s) - start ))s"
      exit 0
    fi
  done
  el=$(( $(date +%s) - start ))
  if [ "$el" -ge "$T" ]; then
    echo "TIMEOUT after ${T}s waiting for '$PAT' in: $*" >&2
    exit 1
  fi
  # a heartbeat every 30 s: a long wait should look alive, not hung
  if [ $(( el % 30 )) -eq 0 ] && [ "$el" -gt 0 ] && [ "$el" != "${last_hb:-}" ]; then
    last_hb=$el
    echo "  ... waiting ${el}s for '$PAT'${FILE_HINT:-}"
  fi
  sleep 1
done
