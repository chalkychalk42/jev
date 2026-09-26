#!/bin/bash
# One session on another character of the campaign, then back to the active one: a check
# between two of the loop's sessions (the mage's checks at T+3 h, 26 September).
#   touch var/loop/hold      # and wait for the loop's `exit=` line
#   tools/visit.sh NAME LOG [SESSIONS]  # e.g. tools/visit.sh Itheamar captures/live-mage-check-2.log
#   rm var/loop/hold
# With SESSIONS above one, the sessions' logs are LOG.1, LOG.2 and so on.
# It restarts the client (the desk must be idle), enters NAME, plays one session as the
# loop would, restarts the client again and enters the campaign's active character. It
# refuses to start unless the loop is held.
set -u
cd "$(dirname "$0")/.." || exit 1
name=${1:?usage: tools/visit.sh NAME LOG}
log=${2:?usage: tools/visit.sh NAME LOG}
sessions=${3:-1}
WINPY=${JEV_WINPY:-/mnt/c/forever-win/Scripts/python.exe}
say() { echo "visit $(date +%H:%M:%S): $*"; }

[ -e var/loop/hold ] || { say "the loop is not held; not starting"; exit 1; }
if pgrep -f "[s]tart_teaching.py --run" > /dev/null; then
  say "a session is still running; wait for the loop's exit= line"; exit 1
fi
back=$(.venv/bin/python - <<'EOF'
import sys
sys.path.insert(0, "tools")
import character
campaign = character.load()
print(campaign["characters"][campaign["active"]]["name"])
EOF
)
say "restarting the client"
tools/keep.sh client-restart || { say "client restart refused (desk in use?)"; exit 1; }
say "entering $name"
timeout 900 "$WINPY" -u tools/character.py enter --name "$name" || { say "could not enter $name"; exit 1; }
for i in $(seq "$sessions"); do
  out=$log
  [ "$sessions" -gt 1 ] && out=$log.$i
  say "session $i of $sessions on $name"
  WSLENV="${WSLENV:+$WSLENV:}JEV_OPERATOR_QUIET_S" JEV_OPERATOR_QUIET_S=600 \
    timeout 1080 "$WINPY" -u tools/start_teaching.py --run --dispatch hybrid > "$out" 2>&1
  say "session exit=$?"
done
say "back to $back"
tools/keep.sh client-restart || { say "client restart refused"; exit 1; }
timeout 900 "$WINPY" -u tools/character.py enter --name "$back" || { say "could not enter $back"; exit 1; }
say "done; $back is in the world"
