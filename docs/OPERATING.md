# Operating the bot

How the bot runs unattended as of 26 September 2026: what is running, how to start, pause
and stop it, how to see what it is doing, and how a change goes live. The 48-hour plan
(`docs/plans/forty-eight-hour-session.md`) and its ledger hold the history. Every rule below
has a row in `DECISIONS.md`.

## What runs

- **The servers**: CMaNGOS `realmd` (port 3724) and `mangosd` (port 8085), in WSL.
- **The game client**: the Windows WoW 2.4.3 client, with the JevRadio addon. The addon only
  paints a strip of coloured cells that the bot reads; it never presses anything.
- **The session loop** (`tools/session_loop.sh`), in WSL. It plays back-to-back sessions of
  about 15 minutes. Each session is a fresh Windows Python process
  (`tools/start_teaching.py --run --dispatch hybrid`). A session attaches to the client,
  plays the active character along its generated guide, and records the run under `runs/`.
  Because each session is a fresh process, a change merged between two sessions takes effect
  at the next one.
- **The keeper** (`tools/keep.sh`). Before each session the loop has it check the servers, the
  disk and the client. A user systemd timer (`jev-keeper.timer`, from
  `tools/keep.sh install-timer`) also runs `tools/keep.sh servers loop` every five minutes, so a
  WSL restart brings everything back.

The desk comes first. When the operator uses the mouse or keyboard, the session pauses for
`var/loop/quiet_s` seconds (600 by default). The keeper restarts or launches the client only
after the desk has been idle for 10 minutes.

## Start, pause and stop

| To | Do |
|---|---|
| Start the loop by hand | `setsid nohup tools/session_loop.sh > /dev/null 2>&1 < /dev/null &` (the timer also starts it) |
| Pause between sessions | `touch var/loop/hold`. `rm var/loop/hold` resumes |
| End the current session now | `touch captures/teaching/STOP` (it finishes a fight first, up to 90 s; the loop clears the file before the next session) |
| Stop the loop | `touch var/loop/stop` (checked between sessions; the keeper leaves the loop alone while it exists) |
| Restart the client | `tools/keep.sh client-restart` (a graceful close, then a relaunch; only at an idle desk) |
| Take the timer off | `tools/keep.sh remove-timer` |

A hold touched just after a session ends can be too late: the next session starts right away.
To be sure, touch the hold, then the STOP file.

## See what it is doing

- `tools/keep.sh status`: one screen covering GREEN or RED (with the reasons), the last
  sessions, each run's level and gain, recent deaths, the servers, the client, the disk and
  the campaign.
- `tools/watch.sh --sessions --every 30 --max 90`: waits, then prints the status screen when a
  session ends, when the status turns red, or after 90 minutes.
- `tools/session_check.py N`: session N's checks. These are the proof lines each change prints
  at the start, plus kills, level gain, deaths and choices.
- `tools/session_report.py --since N`: a row per session with minutes, XP, XP an hour, kills,
  quest steps, deaths, loot, stuck events and money.
- The logs:
  - `captures/session-loop.log`: a line per session start and exit, and the campaign's
    decision.
  - `captures/live-N.log`: what session N did, skill by skill.
  - `runs/<run>/`: the ticks, every executed operation (`executions.jsonl`) and a screenshot
    every second.

## The characters

The campaign in `var/campaign.json` lists the characters in order. Each has a level or a
deadline to play until, and git ignores the file. After each session the loop runs
`tools/character.py due --mark`. When it answers "switch", the loop restarts the client and
`tools/character.py enter` enters the next character, or makes it if it does not exist yet.
It checks the name, class and race by the strip. `tools/character.py status` shows who plays
and until when.

Each character's state is in `var/playheads/character-KEY.json`: its guide, current step and
completed quests. Alongside it:
- `.home.json`: where the hearthstone goes.
- `.equipped.json`: what it has been given to wear.
- `.purse.json`: what it could not pay for (V206).
- `.taxi.json`: the flight points it knows.

To play another character between the loop's sessions, hold the loop, then run
`tools/visit.sh NAME LOG [N]`. It plays N sessions on NAME and returns to the active
character.

## What it learns while it plays

These files are read at each session's start. Each prints a proof line in `live-N.log`.

| File | What it holds |
|---|---|
| `var/danger.json` | Where a character keeps being attacked, by level. Walks bend round the hot cells. |
| `var/route-memory.json` | Spots where walks got stuck outdoors. Plans keep clear of them. |
| `var/choices.json` | Outcome-learned choices: which hunt stations pay, and when to heal. |
| `var/merchant-memory.json` | Merchants that could not be reached or clicked. Each failure costs 250 yards in the ranking; a sale clears it. |
| `var/radio-grid.json` | Where the strip was last read whole. |

## Making a change

Never edit the live checkout while a session runs: the running process imports from it.
1. Work in the worktree `../ForeverV2-dev` (branch `w48`). Add a test that fails on the old
   code.
2. Run the full suite there and check its exit status in a log file, not through `| tail`:
   `.venv/bin/python -m pytest -q -p no:cacheprovider`.
3. Add a `DECISIONS.md` row with the rule, the evidence and the reach (everyone, melee only,
   or casters only).
4. At a session boundary, with the loop held:
   - in the live checkout, `git merge --ff-only w48`;
   - run the offline check: Windows Python `tools/start_teaching.py --dispatch hybrid`, which
     must exit 0;
   - release the hold and push.

Three tests are known to fail now and then under the full suite's load (listed in the
ledger). Rerun the suite before calling a change broken.

### A change to what the strip paints

A new schema changes the addon as well as the decoder: schema 18 (V237) paints the class
trainer's list, which the trainer desk needs to buy by value. The client paints the schema
that is installed until the addon is installed again. So the two go live in this order, each
at a session boundary with the loop held:
1. **The decoder**, merged as above. It reads the installed schema 17 as it did, with the new
   fields unknown, so sessions play on unchanged; the desk still presses Train on the stock
   window's own choice.
2. **The addon**, from the live checkout after the merge: `tools/gen_addon_fields.py --install`
   (it moves the old install to `captures/addon-backup/`). Then restart the client:
   `tools/keep.sh client-restart`, at an idle desk. The addon loads at login, so nothing
   changes until the restart.
3. Check the strip reads as the new schema: Windows Python `tools/observe.py` shows `schema`
   18. Then release the hold.

The other order would leave every session in between blind: a decoder that does not know the
new schema refuses the strip, as a checksum or schema fault. To go back, put the backup
(`captures/addon-backup/<time>-StatusStrip`) back as `Interface/AddOns/StatusStrip` and restart
the client; the new decoder still reads the old strip. The grid kept in `var/radio-grid.json` (eleven rows) needs no change: the
decoder reads the twelve-row strip on its position.

## When something is wrong

- **Red status**: `tools/keep.sh status` gives the reasons, for example no XP in two runs, a
  server down, no client, or a low disk.
- **A character wedged** where no step frees it: after two wedged walks the bot uses the
  hearthstone if it is ready. Until then each session tries again, and the blocked spots are
  remembered.
- **A session that cannot start**: after three quick failures the loop restarts the client.
- **A server down**: `tools/keep.sh servers` starts it again. After three restarts in an hour
  it stops trying and reports red.
