#!/bin/bash
# The live session loop: back-to-back supervised sessions, each a fresh process, so a change
# merged between two sessions applies at the next (docs/plans/forty-eight-hour-session.md).
# It replaces /tmp/session_loop5.sh: nothing it needs lives in /tmp, which a WSL restart empties.
#
# Knobs, each read before every session, in var/loop/ (git ignores var/):
#   arm      "hybrid" (the default), "tutor", or "ab" to alternate every two sessions
#   quiet_s  the operator-pause window in seconds (default 600)
#   hold     while it exists the loop waits between sessions: a deploy is in progress
#   stop     the loop ends before the next session
# Stop the running session now: touch captures/teaching/STOP (it waits out a fight).
#
# Before each session the keeper makes sure of the servers, the disk and the client
# (tools/keep.sh); after each, the campaign may hand over to the next character
# (tools/character.py). One loop at a time: a second start exits while the first holds
# var/loop/lock. Usage: tools/session_loop.sh [first session number]
set -u
cd "$(dirname "$0")/.." || exit 1
mkdir -p var/loop captures/teaching
exec 9>var/loop/lock
flock -n 9 || { echo "another session loop is running" >&2; exit 1; }
LOG=captures/session-loop.log
WINPY=${JEV_WINPY:-/mnt/c/forever-win/Scripts/python.exe}
SESSION_S=${JEV_SESSION_S:-1080}
QUICK_S=${JEV_QUICK_S:-90}
PAUSE_S=${JEV_PAUSE_S:-60}
say() { echo "$* $(date -Is)" >> "$LOG"; }
last=$(grep -oE '^session [0-9]+ start' "$LOG" 2>/dev/null | tail -1 | grep -oE '[0-9]+')
n=${1:-$(( ${last:-0} + 1 ))}
fails=0
say "loop started at session $n"
while [ ! -f var/loop/stop ]; do
  if [ -f var/loop/hold ]; then sleep 5; continue; fi
  if ! tools/keep.sh servers disk client >> "$LOG" 2>&1; then
    say "keeper: not ready; trying again in a minute"
    sleep "$PAUSE_S"
    continue
  fi
  rm -f captures/teaching/STOP
  arm=$(cat var/loop/arm 2>/dev/null || echo hybrid)
  if [ "$arm" = "ab" ]; then
    if [ $(( (n / 2) % 2 )) -eq 0 ]; then arm=tutor; else arm=hybrid; fi
  fi
  quiet=$(cat var/loop/quiet_s 2>/dev/null || echo 600)
  start=$(date +%s)
  echo "session $n start $(date -Is) arm=$arm quiet=$quiet" >> "$LOG"
  WSLENV="${WSLENV:+$WSLENV:}JEV_OPERATOR_QUIET_S" JEV_OPERATOR_QUIET_S="$quiet" \
    timeout "$SESSION_S" "$WINPY" -u tools/start_teaching.py --run --dispatch "$arm" \
    > "captures/live-$n.log" 2>&1
  code=$?
  dur=$(( $(date +%s) - start ))
  echo "session $n exit=$code after ${dur}s $(date -Is)" >> "$LOG"
  n=$((n+1))
  # The campaign: at its level or its deadline the next character takes over (section 8):
  # a fresh client, then the next character made or picked and entered.
  if [ -f tools/character.py ] \
      && [ "$(.venv/bin/python tools/character.py due --mark 2>> "$LOG")" = "switch" ]; then
    say "campaign: switching characters"
    if tools/keep.sh client-restart >> "$LOG" 2>&1 \
        && timeout 900 "$WINPY" -u tools/character.py enter >> "$LOG" 2>&1; then
      say "campaign: switched"
    else
      say "campaign: the switch failed; the next session plays whoever is selected"
    fi
  fi
  if [ "$dur" -lt "$QUICK_S" ]; then fails=$((fails+1)); sleep "$PAUSE_S"; else fails=0; fi
  if [ "$fails" -ge 3 ]; then
    # Three sessions that could not start: a hung client ("Retrieving character list", an
    # unreadable strip) is the one cause the next session cannot clear by itself.
    say "three quick failures in a row; restarting the client"
    tools/keep.sh client-restart >> "$LOG" 2>&1 || say "keeper: the client was not restarted"
    sleep $(( PAUSE_S * 4 ))
    fails=0
  fi
done
say "loop ended"
