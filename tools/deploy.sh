#!/bin/bash
# Deploy a tested branch into the live checkout at the next session boundary (the plan's §0):
# hold the loop, wait for the suite's `exit=` line, wait until no session runs, merge the branch
# fast-forward only, run the offline check (the merge is taken back if it fails), push, and
# release the hold whatever happened, so play never waits on a failed deploy.
#
#   tools/deploy.sh w48 /tmp/w48-suite.log      # from the live checkout, as a background task
#
# The suite runs in the dev worktree first and ends its log with its exit code:
#   cd ../ForeverV2-dev && .venv/bin/python -m pytest -q > LOG 2>&1; echo "exit=$?" >> LOG
set -u
BRANCH=${1:?usage: tools/deploy.sh BRANCH SUITE_LOG}
SUITE=${2:?usage: tools/deploy.sh BRANCH SUITE_LOG}
WINPY=${JEV_WINPY:-/mnt/c/forever-win/Scripts/python.exe}
POLL_S=${JEV_DEPLOY_POLL_S:-5}
cd "$(dirname "$0")/.." || exit 1
say() { echo "deploy $(date +%H:%M:%S): $*"; }
HOLD=var/loop/hold
mkdir -p var/loop captures
touch "$HOLD"
trap 'rm -f "$HOLD"; say "hold released"' EXIT
until grep -q "^exit=" "$SUITE" 2>/dev/null; do sleep "$POLL_S"; done
if ! grep -q "^exit=0$" "$SUITE"; then
  say "the suite failed; nothing merged"
  grep -E "FAILED|ERROR" "$SUITE" | head
  exit 1
fi
say "the suite passed; waiting for the session to end"
while pgrep -f "tools/[s]tart_teaching.py --run" > /dev/null; do sleep "$POLL_S"; done
[ -e "$HOLD" ] || { say "the hold was taken away; not merging"; exit 1; }
say "no session running; merging $BRANCH"
git merge -q --ff-only "$BRANCH" || { say "the merge failed"; exit 1; }
timeout 300 "$WINPY" tools/start_teaching.py --dispatch hybrid > captures/deploy-offline.log 2>&1
rc=$?
say "offline check exit=$rc"
if [ "$rc" != 0 ]; then
  say "the offline check failed: the merge is taken back"
  git reset -q --keep ORIG_HEAD
  exit 1
fi
git push -q origin HEAD && say "pushed $(git log --oneline -1)"
