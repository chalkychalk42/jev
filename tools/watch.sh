#!/bin/bash
# The executor's heartbeat (docs/plans/forty-eight-hour-session.md, section 0): run in the
# background, it exits - and so wakes whoever started it - when the status turns red, when a
# session ends (with --sessions), or after --max minutes, printing the status screen.
#   tools/watch.sh [--sessions] [--every SECONDS] [--max MINUTES]
set -u
cd "$(dirname "$0")/.." || exit 1
sessions=0 every=60 max=240
while [ $# -gt 0 ]; do
  case $1 in
    --sessions) sessions=1 ;;
    --every) every=$2; shift ;;
    --max) max=$2; shift ;;
    *) echo "watch: unknown option $1" >&2; exit 2 ;;
  esac
  shift
done
LOG=captures/session-loop.log
ended() { grep -c ' exit=' "$LOG" 2>/dev/null || echo 0; }
start=$(date +%s)
seen=$(ended)
reason="the watch ran its $max minutes"
while [ $(( $(date +%s) - start )) -lt $(( max * 60 )) ]; do
  if ! tools/keep.sh status > /dev/null 2>&1; then
    reason="the status is red"
    break
  fi
  if [ "$sessions" = 1 ] && [ "$(ended)" != "$seen" ]; then
    reason="a session ended: $(grep ' exit=' "$LOG" | tail -1)"
    break
  fi
  sleep "$every"
done
echo "watch: $reason ($(date +%H:%M))"
tools/keep.sh status
exit 0
