#!/bin/bash
# The keeper: makes sure of what the live loop needs, and says what it did, one line each
# (docs/plans/forty-eight-hour-session.md, section 10). It never prints credentials.
#   servers         realmd (3724) and mangosd (8085) listening. Either one down is started as
#                   they were on 22 September; after three restarts within the hour it stops
#                   trying and fails
#   disk            50 GB or more free
#   client          a game client running. A missing one is launched once nobody has touched
#                   the desk for 10 minutes; the next session logs in (--reconnect)
#   client-restart  the client closed gracefully (forced only after 90 s), the server's
#                   Logout line awaited, and a new one launched. Only at an idle desk
#   loop            the session loop running, unless var/loop/stop exists
#   status          one screen of what the heartbeat checks (tools/keep_status.py)
#   install-timer   a user systemd timer that runs "servers loop" every five minutes from boot,
#                   so a WSL restart brings everything back; remove-timer takes it off
# Several may be given in one call: tools/keep.sh servers disk client
set -u
# The loop runs this with its lock on fd 9: a server started from here must not hold it
# after the loop has gone, or no loop could start again (review, 25 September).
exec 9>&-
cd "$(dirname "$0")/.." || exit 1
ROOT=$(pwd)
WINPY=${JEV_WINPY:-/mnt/c/forever-win/Scripts/python.exe}
CMANGOS=${JEV_CMANGOS:-$HOME/cmangos/run}
SERVER_LOGS=${JEV_SERVER_LOGS:-$HOME/cmangos/logs}
IDLE_S=${JEV_KEEP_IDLE_S:-600}
WAIT_S=${JEV_KEEP_WAIT_S:-5}
SETTLE_S=${JEV_KEEP_SETTLE_S:-20}
MIN_FREE_GB=${JEV_MIN_FREE_GB:-50}
WOW_DIR='C:\Games\WoW243'
mkdir -p var/loop

note() { echo "keep: $* $(date -Is)"; }
listening() { ss -ltnH "sport = :$1" 2>/dev/null | grep -q .; }

start_server() {        # a server's name, then its arguments
  local name=$1 recent
  shift
  if pgrep -x "$name" > /dev/null; then
    note "$name is running but not listening yet"
    return 0
  fi
  recent=$(awk -v since=$(( $(date +%s) - 3600 )) '$1 >= since' var/loop/server-restarts \
           2>/dev/null | wc -l)
  if [ "$recent" -ge 3 ]; then
    note "$name is down after three restarts within the hour: not restarting it (read Server.log)"
    return 1
  fi
  date +%s >> var/loop/server-restarts
  mkdir -p "$SERVER_LOGS"
  note "starting $name"
  (cd "$CMANGOS/bin" && setsid nohup "./$name" "$@" >> "$SERVER_LOGS/$name-keep.log" 2>&1 \
     < /dev/null &)
}

servers() {
  local i
  listening 3724 || start_server realmd -c "$CMANGOS/etc/realmd.conf" || return 1
  listening 8085 || start_server mangosd -c "$CMANGOS/etc/mangosd.conf" \
                                         -p "$CMANGOS/etc/aiplayerbot.conf" || return 1
  # mangosd loads the world for a minute or two before it listens.
  for i in $(seq 60); do
    listening 3724 && listening 8085 && return 0
    sleep "$WAIT_S"
  done
  note "the servers are not listening after five minutes"
  return 1
}

disk() {
  local free
  free=$(df -BG --output=avail "$ROOT" | tail -1 | tr -dc 0-9)
  if [ "${free:-0}" -lt "$MIN_FREE_GB" ]; then
    note "only ${free:-0} GB free: the loop needs $MIN_FREE_GB"
    return 1
  fi
}

# Every call into Windows is bounded: one hung interop call would stall the loop with the
# status still green (review, 25 September).
wow_pid() {
  timeout 30 tasklist.exe /FI "IMAGENAME eq Wow.exe" /FO CSV /NH 2>/dev/null | tr -d '\r' \
    | awk -F'","' 'tolower($1) ~ /wow\.exe/ {print $2; exit}'
}

desk_idle() { timeout 60 "$WINPY" tools/desk.py "$IDLE_S" > /dev/null 2>&1; }

launch() {
  local i
  note "launching the game client"
  # Never cmd.exe /c start from WSL: it hung and never started the game (23 September).
  timeout 60 powershell.exe -NoProfile -Command \
    "Start-Process -FilePath '$WOW_DIR\\Wow.exe' -WorkingDirectory '$WOW_DIR'" \
    < /dev/null > /dev/null 2>&1
  for i in $(seq 24); do
    if [ -n "$(wow_pid)" ]; then
      sleep "$SETTLE_S"              # the login screen draws after the window opens
      return 0
    fi
    sleep "$WAIT_S"
  done
  note "the game client did not start"
  return 1
}

client() {
  [ -n "$(wow_pid)" ] && return 0
  if ! desk_idle; then
    note "no game client, and someone has used the desk within $IDLE_S s: not launching"
    return 1
  fi
  launch
}

client_restart() {
  local pid mark i
  if ! desk_idle; then
    note "someone has used the desk within $IDLE_S s: not restarting the client"
    return 1
  fi
  pid=$(wow_pid)
  if [ -n "$pid" ]; then
    mark=0
    [ -f "$CMANGOS/bin/Char.log" ] && mark=$(wc -l < "$CMANGOS/bin/Char.log")
    note "closing the game client (pid $pid)"
    timeout 30 taskkill.exe /PID "$pid" > /dev/null 2>&1        # no /F: a graceful close
    for i in $(seq 18); do
      [ -z "$(wow_pid)" ] && break
      sleep "$WAIT_S"
    done
    if [ -n "$(wow_pid)" ]; then
      note "the client did not close in 90 s: forcing it"
      timeout 30 taskkill.exe /F /PID "$pid" > /dev/null 2>&1
      sleep "$WAIT_S"
    fi
    # A client that asks for the character list while the old session is still logging out
    # waits on it forever (24 September): the server's Logout line comes first.
    for i in $(seq 24); do
      tail -n +"$(( mark + 1 ))" "$CMANGOS/bin/Char.log" 2>/dev/null \
        | grep -q "Logout Character" && break
      sleep "$WAIT_S"
    done
  fi
  # The close and the Logout wait can take minutes: look at the desk again before taking it.
  if ! desk_idle; then
    note "someone came to the desk while the client closed: not launching"
    return 1
  fi
  launch
}

loop_running() { ! flock -n var/loop/lock true 2>/dev/null; }

loop() {
  if [ -f var/loop/stop ]; then
    note "var/loop/stop is set: not starting the session loop"
    return 0
  fi
  loop_running && return 0
  note "starting the session loop"
  setsid nohup tools/session_loop.sh > /dev/null 2>&1 < /dev/null &
}

install_timer() {
  local dir=$HOME/.config/systemd/user
  mkdir -p "$dir"
  # KillMode=process: the servers and the loop it starts outlive the check that started them.
  cat > "$dir/jev-keeper.service" <<EOF
[Unit]
Description=Jev keeper: the game servers and the session loop ($ROOT/tools/keep.sh)

[Service]
Type=oneshot
KillMode=process
Environment=PATH=/usr/local/bin:/usr/bin:/bin:/mnt/c/WINDOWS/system32:/mnt/c/WINDOWS/System32/WindowsPowerShell/v1.0
ExecStart=$ROOT/tools/keep.sh servers loop
EOF
  cat > "$dir/jev-keeper.timer" <<EOF
[Unit]
Description=Run the Jev keeper every five minutes, from boot

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
EOF
  systemctl --user daemon-reload && systemctl --user enable --now jev-keeper.timer \
    && note "keeper timer installed"
}

remove_timer() {
  systemctl --user disable --now jev-keeper.timer > /dev/null 2>&1
  rm -f "$HOME/.config/systemd/user/jev-keeper.service" "$HOME/.config/systemd/user/jev-keeper.timer"
  systemctl --user daemon-reload
  note "keeper timer removed"
}

[ $# -gt 0 ] || { sed -n '2,19p' "$0"; exit 2; }
for command in "$@"; do
  case $command in
    servers) servers || exit 1 ;;
    disk) disk || exit 1 ;;
    client) client || exit 1 ;;
    client-restart) client_restart || exit 1 ;;
    loop) loop || exit 1 ;;
    status) "$ROOT/.venv/bin/python" tools/keep_status.py || exit 1 ;;
    install-timer) install_timer || exit 1 ;;
    remove-timer) remove_timer ;;
    *) echo "keep: unknown command $command" >&2; exit 2 ;;
  esac
done
