Forty-eight-hour session plan: level 20, a second character, and a streamlined build
====================================================================================

Written 25 September 2026, 11:30-12:10 BST, after sessions 88-133 and DECISIONS V158-V161
(all pushed, 0dbeb58). It covers the unattended session from Friday 25 September about 19:30 BST
to Sunday 27 September about 19:30-22:00 BST. The executor is a Claude session in a bb thread
with the PC to itself. Authority when documents disagree: ARCHITECTURE.md > DECISIONS.md >
docs/ROADMAP.md > STATUS.md (newest entry last). Every change passes ROADMAP's design gate (V37).
Hours count from T-0 = Friday 19:30 BST (hour 24 = Saturday 19:30, hour 40 = Sunday 11:30).

0. Operating card (read first, and again after every compaction)
-----------------------------------------------------------------

**Mission.** Testvvi (human paladin, key 548c8582) plays until level 20 or hour 15, whichever comes
first (the operator's rule). A human mage, made by the campaign tool, then levels from Northshire
on the first caster profile (§8b): ranged engagement and distant targeting. One general change at
a time, judged on its block, kept or reverted, and pushed. The build is streamlined and frozen at
hour 44.

**Cold start.** Read this card, the Now section of `docs/plans/forty-eight-hour-ledger.md` and the
last STATUS.md entry, then run `tools/keep.sh status`. State lives only in files: the ledger,
STATUS.md, `var/campaign.json`, `var/loop/` and `captures/session-loop.log`.

**Every 15 minutes:** run `tools/keep.sh status`. It is red if: there is no loop, or two; a
session ran under 850 s or exited non-zero, twice running; two sessions gained no XP; 3 or more
deaths came in 20 min; a new Traceback appeared; port 8085 or 3724 is closed; Wow.exe is missing;
the strip's key is not the active character's; or under 50 GB is free. Green: leave the loop alone.
**Every 2 hours:** add a scoreboard row to the ledger and judge the running trial (§7). Build the
next change from the largest measured loss, in the dev worktree, with tests.
**Every 4 hours:** make the checkpoint's go/no-go call (§9), write a STATUS entry, commit, push.

**Commands** (repo root; A1 builds the loop and `keep.sh`; Windows runs about 25 s ahead of WSL):
- Start the loop, once only (check `pgrep -af session_loop` first):
  `setsid nohup tools/session_loop.sh > /dev/null 2>&1 < /dev/null &`
- Stop after this session: `touch var/loop/stop`. Stop this session now:
  `touch captures/teaching/STOP` (it waits out a fight, up to 90 s).
- Deploy: `touch var/loop/hold`; after the `exit=` line, `git merge --ff-only w48` in the live
  checkout; offline check `/mnt/c/forever-win/Scripts/python.exe tools/start_teaching.py
  --dispatch hybrid`; `rm var/loop/hold`; `git push origin main`.
- Recovery: `tools/keep.sh servers`, `tools/keep.sh client` or `tools/keep.sh client-restart`
  (§10). The loop switches characters by itself. By hand: `tools/keep.sh client-restart`,
  then `/mnt/c/forever-win/Scripts/python.exe tools/character.py enter [--name NAME]` (§8).
  `tools/character.py status` shows the campaign.
- Safe park, for any stop longer than 10 minutes: stop the session, then close the client
  gracefully.

**Hard rules** (§13 has them verbatim): never run `look_tmp.py` or `captures/targeting/*`. The
repo is public: no credentials, no personal data. The addon never actuates; the bot does not talk.
Check the desk is idle before taking focus. `session.py` never contains the word "forever".
General fixes only, one session per place at most. Edit in the dev worktree; in the live tree,
one write per change. Database queries are read-only and never print credentials. Push every
commit.

1. Where things stand (measured 25 September)
----------------------------------------------

| Measure | Value | Source |
|---|---|---|
| Character 1 | Testvvi: level 13 with 546 of 11,000 XP, 67s 86c, in Goldshire at (-9425, 69); home is Goldshire's inn | characters DB, logout 09:13:34 |
| Play outside the bot | The operator's figures (12 at 98.4%, 42s, full bags) are the 08:51 tick, when the loop stopped. At 09:03 Kobold Candles (60) and A Fishy Peril (40) were rewarded, with no run recorded; the playhead does not know | `character_queststatus`, runs/ |
| Quest log | Riverpaw Gnoll Bounty 7/8. Complete, not handed in: Further Concerns, Shipment to Stormwind, Grape Manifest. Open: Goldtooth, A Bundle of Trouble 1/8 | same |
| Playhead | 1-12 guide, step 89 of 114 (Riverpaw "do"). 26 steps (counting this one) and 11 quests left, about 5,150 XP at 13. Rewarded on the server but missing from `completed`: 16, 21, 40, 60 | playhead, `compile_route` |
| XP to 20 | 104,854. From 13 to 20 is 105,400: 11,000 + 12,300 + 13,600 + 15,000 + 16,400 + 17,800 + 19,300 (not 113k) | `world_player_xp_for_level` |
| Nine hours (sessions 88-133) | 9.72 h played: 20,500 XP (2,108 an hour), 408 kills, 30 guide steps (3.1 an hour), 12 deaths, 439 stuck events. 763 tutor calls, 99 min spent waiting | `session_report.py`, ticks |
| 12-20 guide | Never run live. 81 supported steps, 28 hand-ins worth about 27,850 XP at 15, about 50,000 yards of legs. No guide follows it: a finished last guide leaves the character waiting and the session stops | `compile_route`, `NEXT_GUIDE`, supervisor |
| Learning | V158-V160 (choice points; the tutor only after a routine fails) and V161 (danger map): committed, not tested live. `choices.json` holds 247 backfilled station visits; nothing yet for the heal line or recovery | git, `choices_report.py` |
| Tests | 2,431 pass in 131 s. One flaky test: the adaptive canary-student runtime test failed in 1 of 2 full runs and passes alone | pytest, 11:45 |
| Servers | realmd and mangosd have run since 22 Sep 05:35 (PPID 1, cwd `~/cmangos/run/bin`, stdout in `/tmp`); nothing restarts them. MariaDB is a systemd service | ps, ss |
| Disk | `runs/` holds 72 GB for 191 runs, about 380 MB a session. 718 GB free in WSL, 1.3 TB on C: | df, du |
| Account | Test (guid 1801), Testvii (1802), Testvvi (1803). The list is ordered by guid, `lastCharacterIndex` is 2, and the realm allows 10 characters | DB, Config.wtf |

2. Where the time went, and what would pay
------------------------------------------

This covers the 42 sessions of 88-133 that played (four failed at start). Time is each tick's
armed skill, with gaps capped at 5 s. A kill's XP is the change from the tick before its fight to
4 s after it; its level difference is the target's most-seen level minus the character's.

| Where the time went | Share | Notes |
|---|---|---|
| Hunting (walking between stations, looking) | 32.6% | Stations pay off 7-71% of visits, depending on the creature |
| Fighting | 22.0% | 74% of all XP arrived during a fight |
| Bag service | 12.9% | 65 attempts: 44 cut short (35 by combat), 11 ended by the session, 10 finished (median 96 s). The bags already hold 40 slots |
| Travel | 12.5% | 13 sessions had 15 or more stuck events: 3.25 h at 1,540 XP/h, against 2,395 for the other 29 |
| Quest NPCs | 7.0% | 19% of the XP (10 hand-ins) |
| Meals | 6.0% | |
| Death (release, corpse run) | 3.6% | 12 deaths, plus 17 min as a ghost |
| Vendor, supplies, training | 3.5% | |
| Tutor waits (included in the rows above) | 17% | Median 7.4 s a call, in teach mode |

| Mob level minus the character's | Kills | XP a kill | Median fight |
|---|---|---|---|
| -7 or -6 (grey) | 105 | 0 | 9-11 s |
| -5 | 87 | 35 | 12 s |
| -4 | 87 | 42 | 16 s |
| -3 | 73 | 64 | 16 s |
| -2 | 51 | 75 | 19 s |
| -1 or 0 | 5 | 93-120 | 19-20 s |

The grey kills came from quest objectives (54, nearly all Goldtooth's and Riverpaw's), from fights
on the way to other steps (29) and from ribs (22). The average kill paid 40 XP; a kill within two
levels pays about twice that for about a third more fight time.

The levers, ranked by expected gain over the 2,108 XP/h baseline:

1. **Fight what pays: +1,000 to 1,300 XP/h**, projected from the table above (68% of kills were
   grey or 4-5 levels below). Three parts:
   - (a) *The band rule.* A guide the character has out-levelled hands over to the next at the
     next quest boundary. The rest of 1-12 pays about 5,150 XP of hand-ins against mobs at -3 to
     -5; the first 26 steps of 12-20 pay about 7,100 against mobs at about -2 to +2.
   - (b) When the last guide ends, the character grinds the rib for its level instead of waiting.
   - (c) Ribs are chosen by XP a minute net of deaths, not "never above the character"
     (`rib_for`). At level 12, rib 9-11 made 2,502 XP/h and rib 7-9 made 1,931.

   "Skip green quests" is the wrong rule: level-10 quests still pay full XP at 13-15
   (`Quest::XPValue` pays in full up to quest level + 5). The engine has no level-based trimming,
   only pass-by rules for impossible steps (V144, V152) and detours within one guide (V135, V148).
2. **The tutor only after a routine fails: about +20%, roughly +420 XP/h.** Built (V158). V129's
   A/B measured hybrid at 3,152 XP/h against teach at 2,620 on clean sessions; the nine hours spent
   99 minutes waiting on the tutor.
3. **Fights with two or more attackers: +100 to 300 XP/h, and they protect lever 1.** The last six
   deaths came at the Riverpaw camp and on the level 9-11 rib (sessions 126-133): 3.7 an hour over
   1.6 hours, after three at Jerod's Landing. Each costs about 1.7 min dead or as a ghost, plus the
   lost fight and repairs, and Westfall's packs are the character's own level. V159 (a fight stops
   looking for its target in order to heal) is built; "facing the wrong way" among several
   attackers is still open (STATUS NEXT 1).
4. **Multi-floor places: about +290 XP/h if contained.** Stuck-heavy sessions ran at 64% of the
   others' rate. In 12-20, 7 of the 21 quest NPCs stand over two or three navmesh floors: the
   Saldean farmhouse (3 floors), Sentinel Hill's tower, and Lakeshire's inn and dock.
5. **Bag service: about +130 XP/h by halving it.** A trip cut short resumes to the same merchant
   instead of starting over. Bags that fill in a hot cell (V161) finish the objective first,
   taking only quest items and coin, and then sell. A merchant within reach of the route is
   visited at 8 free slots or fewer.

Smaller levers: hunt stations (V158, built, about +5%); meals (6% of the time); Thunderbrew Lager,
12-20's first quest, which pays no XP, unlocks nothing and costs about 2,700 yards; trainer trips
from Westfall to Brother Wilhelm in Goldshire (1,400-2,300 yards, nearer than Stormwind's
cathedral). Session boundaries cost 1.6%, so they are not a lever.

At 2,108 XP/h, level 20 is 49.7 h of play away, longer than the window. Reaching 20 by hour 40
needs about 2,760 XP/h. The must tier below assumes no improvement at all.

3. Goals, in tiers
------------------

The operator set the switch at level 20 or hour 15, whichever comes first (§4), so Testvvi has
about 13.5 h of play (T-0 takes the rest) and the mage about 31 h. Testvvi's own times on older
code are the mage's baseline: level 5 at 2.1 h of play, 8 at 8.2 h, 10 at 12.7 h, 12 at 19.8 h.

| Tier | Goal | Target | Measured by |
|---|---|---|---|
| Must | M1 Testvvi | Level 15.0 or higher at the switch: about 24,000 XP from T-0, 1,780 XP/h over 13.5 h (85% of the nine-hour rate) | strip, report |
| Must | M2 Mage made by the tool | Created, entered and verified (key, class 8, race 1) at T-0, with its own playhead; the switch at hour 15 made by the loop | log, screenshot |
| Must | M3 Mage | Level 8 or higher by hour 48, on the caster profile | strip |
| Must | M4 Autonomy | No stop over 60 min. The loop recovers on its own from client, server and disconnect failures | `session-loop.log` |
| Must | M5 Discipline | Every change judged on its block, committed and pushed; suite green; final report written | ledger, git |
| Should | S1 Testvvi | Level 16.0 or higher at the switch (about 2,760 XP/h, 1.31 times the nine hours) | strip |
| Should | S2 Mage | Beats Testvvi's play time to every milestone above, so level 12 or higher by hour 48 | report |
| Should | S3 Caster profile | Pulls from 25 yards or more; drinks for mana rather than waiting; trains on time; 1.0 deaths an hour or fewer | evidence, report |
| Should | S4 Survival | 1.0 deaths an hour or fewer overall (nine hours: 1.23; their last 1.6 h: 3.7) | report |
| Should | S5 Streamlined | Retired paths removed, docs current, nothing left in `/tmp` | git, docs |
| Stretch | X1 | Testvvi at 17 at the switch (about 3,800 XP/h) | strip |
| Stretch | X2 | The mage at level 14 by hour 48 (64,700 XP) | strip |
| Stretch | X3 | A best 8-hour block at 4,000 XP/h or more; 10 tutor calls an hour or fewer | report |

4. Questions for the operator, and the answers
----------------------------------------------

**Answered 25 September 12:20.**
- Q1: **human mage**, "as we havent done a caster profile yet so that would be good to get dialled
  in properly as thats the other half of all classes kinda right so like ranged engagement and
  distant targetting for max effectiveness kinda thing" (§8b).
- Q3: **level 20 or hour 15, whichever comes first**. There is no extension.
- Q5: **yes to both**.
- Pre-flight A: **start now**.
- Q2, Q4, Q6, Q7 and Q8 keep their defaults.

The questions as asked:

- **Q1 Class and race for character 2.** Default: **human paladin**, the only class that has
  played live; the guides start at Northshire. The other classes have starting bars only.
- **Q2 Name.** Default: **generated**: pronounceable, letters only, 6-9 letters, checked unique
  (§8). Give a name instead if you prefer.
- **Q3 Switch rule.** Default: **at level 20 or at hour 40 (Sunday 11:30), whichever comes first**,
  with one extension to hour 43 if character 1 is at 19.5 or higher and projected to reach 20 by
  then.
- **Q4 Play outside the bot.** Kobold Candles and A Fishy Peril were handed in at 09:03. Default:
  add quests 16, 21, 40 and 60 to Testvvi's `completed` (the server rewarded all four). Say if
  anything else was done by hand.
- **Q5 Servers.** May the executor restart mangosd and realmd with the same binaries, config and
  flags, and add a user-level systemd timer that restarts them and the loop after a WSL restart?
  Default: yes to both; the timer comes off with one command.
- **Q6 Band rule.** At 13, character 1 leaves the rest of 1-12 after Riverpaw: about 5,150 XP of
  level-10 quests, mostly in east Elwynn. Default: yes, judged on its block and reverted if worse.
- **Q7 After hour 48.** Default: the loop keeps playing the frozen build until you return; your
  input pauses it within 2 s (V130).
- **Q8 Subscription.** If usage limits come close, the executor's own work goes first and the
  tutor pauses (routine-only). Default: yes.

**Before you leave (only you can do these):** pause Windows Update for 7 days; set sleep to Never
and keep the display settings the nine hours ran with; turn off any screen saver or lock, and do
not lock or sign out; leave WoW running and the bb thread open.

5. Pre-flight A: now to 19:30, without the client (the loop stays stopped)
--------------------------------------------------------------------------

The operator may be at the PC, so nothing here attaches to the client, raises its window or sends
input. Start by creating the ledger, with its Now, Blocks, Trials and Issues sections. Work in a
dev worktree (`git worktree add ../ForeverV2-dev -b w48`). Run tests from its root with
`/home/ash/ForeverV2/.venv/bin/python -m pytest`, after checking that `import jev` resolves to the
worktree (the editable install points at the main checkout). Merge with `--ff-only` and push each
item. In priority order:

- **A1 Autonomy (must, 60 min).** Move the loop into `tools/session_loop.sh`, starting from
  `/tmp/session_loop5.sh`, with no credentials in it.
  - Flags live in `var/loop/`: `arm` (defaults to hybrid), `quiet_s`, `stop` and `hold`. Session
    numbers continue from `session-loop.log`, and logs go to `live-<N>.log`.
  - Before each session the loop runs `tools/keep.sh servers` and `tools/keep.sh client`; after
    each it runs `tools/character.py due`.
  - `keep.sh status` shows one screen: loop PIDs, the last three exits and lengths, the last
    session's XP, deaths and stuck events, ports, Wow.exe, disk, the newest tick's key and level,
    and the campaign. `keep.sh loop` starts the loop unless `var/loop/stop` exists.
  - `session_report.py` reads both log names (it hard-codes `live-testvvi-N` today). Test with
    faked commands.
- **A2 Throughput (must, 90 min).** Three rules, each with tests:
  - (a) The band rule: at a quest boundary, a character above the guide's band finishes the guide
    and the next takes over (V124's chain). Keep the band as data beside `NEXT_GUIDE`: 1-12 moves
    on at 13.
  - (b) When the last guide finishes, grind `rib_for(level)` a level at a time; never wait.
  - (c) A quest with no XP and nothing depending on it leaves the supported route.

  Check offline: a replay of Testvvi's reconciled playhead should show Riverpaw, then 12-20 from
  Patrolling Westfall. Run `jev.run.cli --check` on the 12-20 guide. Planned routes from Goldshire
  to Sentinel Hill and from Sentinel Hill to Lakeshire must both come back complete.
- **A3 Reconcile Testvvi (must, 15 min).** Add 16, 21, 40 and 60 to `completed`, leave the step as
  it is, and note it in STATUS.
- **A8 Caster profile (must for the dry run; see §8b).** The level-1 core first: pull from
  range, cast, drink. The rest is built in hours 1.5-15, from the dry run's evidence.
- **A4 Character 2 tooling (must, 2 h; see §8).** `tools/character.py` (`name`, `due`,
  `measure`, `create`, `select`, `switch`) and `var/campaign.json`; Session stages with provisional
  constants; the wait after Enter World; verification in the world. Test on synthetic frames, as
  `tests/fixtures/login-empty.npz` was used for login. If it is not ready by T-0, the dry run moves
  to hours 8-12.
- **A5 Scoreboard (should, 45 min).** The §7 columns, and `--blocks` in `session_report.py`.
- **A6 Flaky test (15 min).** Make it deterministic, or remove it with its retired path (§11).
- **A7 If there is time.** Three sessions in a row with no XP and no step start the next one with
  a hearth home (V109 across sessions).

Gate: suite green, everything pushed, the live checkout at `origin/main`, the loop stopped.

6. T-0 with the client (Friday 19:30 to about 21:00)
----------------------------------------------------

- **B1 State (10 min).** The desk has been idle for 10 minutes (GetLastInputInfo, read-only) and
  no loop is running. Ports 3724 and 8085 are listening. The strip is schema 16, with key 548c8582
  and level 13. The tree is at `origin/main` and the arm is `hybrid`.
- **B2 Validate V158-V161 (two sessions, 30 min, one at a time).** The logs must show "danger: N
  cells learned" and "choices: N hunt station visits remembered"; `choices.jsonl` outcomes for
  `hunt.station` and `fight.heal_below`; tutor requests only after a routine failure; some XP, and
  no Traceback.

  **Rollback, decided within the session.** It is triggered by any of: a Traceback in the new
  code; no XP while the strip is healthy and nothing outside the bot explains it; two or more
  deaths; the tutor taking ordinary objectives. Then `git revert` the suspects in the order V161,
  V160/V159, V158, push, and rerun. After two failed reruns, go back to the **nine-hour
  configuration** (arm `tutor`, V158-V161 reverted) and record it.
- **B3 Glue screens and the dry run (45 min; see §8).**
  - Safe park, and wait for the Logout line in `Char.log`.
  - Relaunch and stop at character select. Measure character select, the create screen and a
    refusal dialog, and commit the constants.
  - Run `tools/character.py create`, which must verify in the world.
  - Play one 15-minute session on the mage. The proof: A Threat Within accepted, a kill opened
    from 25 yards or more, and a drink taken.
  - Return to Testvvi by clicking row 2 of 4, and confirm the key.

  Fallback: after two failed creates, run character 1 and retry, attended, between hours 8 and 12.
- **B4 Arm (10 min).** Start the loop, the 15-minute heartbeat (a recurring bb automation or
  equivalent) and, if Q5 allows, the keeper timer. Add the T-0 row to the ledger, write a STATUS
  entry, and push.

7. The improvement cycle and the scoreboard
-------------------------------------------

One behavioural change at a time, judged on its own block. Crash fixes skip the queue. Every two
hours:

1. **Measure** the block that ended: the scoreboard, the time use (§2's method, as a report
   option) and the deaths.
2. **Pick** the largest measured loss that a general mechanism can fix; §2's order is only the
   starting queue. Name in advance the metric the change should move, the log line that proves it
   fired, and the screenshot that shows it.
3. **Build** in the dev worktree, with tests that fail on the old code. Check the full suite's exit
   code, not the output of a pipe.
4. **Deploy** at a session boundary (§0). Commit, push, and add a DECISIONS row.
5. **Judge** after 4 clean sessions (8 if noisy). Keep the change if its metric moved as predicted
   and three guards held: XP/h at least 0.8 times the previous block's; deaths per hour no more
   than the previous block's plus 0.5; no new failure class. Otherwise revert it with a pushed
   commit and record why. Block XP/h alone decides nothing under a 30% difference: session XP/h
   ran from p25 1,235 to p75 2,624 over 42 sessions.

Every STATUS entry reports learner gain (the standing rule). The scoreboard, per session and per
block:

| Columns | From |
|---|---|
| XP/h, kills/h, XP a kill, share of grey kills | ticks, executions |
| Deaths/h, stuck events/h, minutes in walks that failed | ticks, logs, skills |
| Quests handed in per hour, guide steps per hour | tracker events |
| Bag-service and meal minutes per hour | skills |
| Tutor calls/h; after a failure, tutor wins/tries and routine wins/tries | play-teacher, choices |
| Learner gain: station visits won/tried, heal-line fights and their bad share per line, danger attacks added | `choices.jsonl`, `danger.json` |

**Candidate choice points.** V158's rule applies: a choice point needs real alternatives, and its
outcomes must be shown to vary first. Three earlier candidates had no signal once measured.
- `rib.choice` (first in line): which rib at a level, rewarded by XP a minute net of deaths. XP/h
  per rib and level ranges from 0 to 5,000: at level 12, 2,502 against 1,931; at level 6, 2,882
  with 7 deaths against 1,593 with 2. Each cell has 1-10 runs. It needs an arm that holds a
  reward, not wins and tries.
- `bag.when`: sell now, or finish the objective first. First check that trips begun in hot cells
  were cut short more often.
- `merchant.choice`: first check how much trip time varies by merchant.
- *Not* a choice point: which plate to pull. The bot's own pulls were safe (1 bad in 208), and XP
  by level is a formula. Make it a rule: prefer higher-level plates that are not grey, within the
  safe band.

Adopt one only when a held-out split shows the options differ on 30 or more outcomes, as V161's
did (fitted on the earlier 70% of runs, tested on the later 30%).

8. Character 2: automated creation and the switch
--------------------------------------------------

**What exists.** `tools/login.py` drives `jev/clients/session.py` through login, the realm screens
and character select. It recognises each screen by its red button plates, at measured fractions of
the 1600x900 client, and presses nothing on a screen it cannot read. Enter World goes to *whoever
is selected*, which follows the client's `lastCharacterIndex`. The addon paints only in the world,
so glue screens are known only by their measured plates. Playheads are keyed by `char.key`, the
FNV-1a hash of name and realm (V70); a new key starts at the guide's entry, from its own quest
log. The only thing tied to Testvvi is the log name `live-testvvi-N`, in the loop and in
`session_report.py` (lines 117 and 275). `.env` holds only the account and GLM keys, and the
launch config names no character.

**Server rules.** Names are 2-12 letters (`MinPlayerName`, `MAX_PLAYER_NAME`), with
`StrictPlayerNames` 0; the server makes them a capital followed by lower case. Reserved names and
names in use are refused, and 1,803 characters exist on 201 accounts (most from the playerbot run
of 18-19 September). `SkipCinematics` is 0, so a new character's first entry plays the intro.

**Design.**
1. **Name.** `tools/character.py name` draws a pronounceable name: alternating syllables, 6-9
   letters, no letter three times running, first letter capital. It refuses any name found by
   `SELECT 1 FROM characters WHERE name=?` (read-only; the script reads its credentials without
   printing them). The name and its `character_key(name, realm)` go into `var/campaign.json`,
   which git ignores; the realm name lives only there, never in `session.py`. The server's answer
   is final: a refusal dialog gets Okay and the next name, up to three.
2. **Glue stages**, measured at T-0 and added to `Session` under login's rules: Create New
   Character on character select; Human, Mage, the name box, Accept and Back on the create
   screen; the refusal dialog's Okay; list rows 0-9. The name box must show text (`_has_text`)
   before Accept is pressed, and the frame of any unknown screen is kept.
3. **After Enter World, wait.** An unreadable screen (loading, or the intro) is waited on for up to
   150 s, pressing nothing, until the strip paints.
4. **Verify in the world:** the recorded key, level 1, `class_id` 8 (mage), `race_id` 1. Any mismatch
   stops the tool with a report. Nothing is ever deleted.
5. **Trigger.** After every session the loop runs `tools/character.py due`, which reads files only
   (the active character's newest tick). It answers "switch" at level 20 or higher, or once the
   deadline in the campaign file has passed.
6. **Switch.** A client restart, not an in-world logout: the restart is proven and needs no chat
   command. `tools/character.py switch` (Windows Python) checks the desk is idle; closes the client
   gracefully and waits for Logout in `Char.log` (up to 120 s); relaunches through PowerShell
   `Start-Process` and signs in as far as character select. It then creates the character, or
   clicks its row (the list is in guid order: Test 0, Testvii 1, Testvvi 2, the new character 3);
   enters the world; verifies; and marks the campaign file. If the switch fails, the loop logs it
   and keeps playing character 1, and the heartbeat turns red.
7. **First session.** With no playhead file the character starts at the 1-12 entry: A Threat
   Within, at Northshire Abbey. Its gear, home and taxi files start empty, and it binds its
   hearthstone nearer the work (V126).
8. **What carries over.** In `choices.json`, hunt stations are keyed by creature and spawn point,
   the heal line is global, and recovery is keyed by skill and failure code. `route-memory.json`,
   `merchant-memory.json`, the motor corpus (V131) and `danger.json` carry over too.
   **Fix `danger.json` before the switch:** it counts every level from one below the character's
   upwards. At level 3 that includes Testvvi's level-12 walks past low camps that nothing
   attacked, which dilutes exactly the cells a low-level character must avoid. Count levels L-1
   to L+2 instead, tested offline on Testvvi's early runs.
9. **Class.** Human mage (the operator's answer to Q1): the first caster profile (§8b). The
   paladin's training (V119) and save roles (V96, V128, V159, V160) stay exactly as they are.
10. **Baseline for "fewer bugs".** Testvvi reached level 5 at 2.1 h of play, 8 at 8.2 h, 10 at
    12.7 h and 12 at 19.8 h. Character 2 goes back through Northshire Abbey, Echo Ridge,
    Fargodeep, Jasperlode and the Lion's Pride Inn, the places that cost the first character, so
    it is their regression test.

**The switch rule (the operator's).** At level 20 or at hour 15 (Saturday about 10:30),
whichever comes first, with no extension. `var/campaign.json` holds the deadline.
- Before hour 15, the mage plays only the T-0 dry run. One more test session is allowed if a
  caster change needs a live check before the switch.
- If the switch fails, Testvvi plays on while it is fixed, attended, and the switch is retried
  within two hours.
- If the mage cannot fight after the switch (a Traceback, or two sessions with no kill), Testvvi
  plays while the caster profile is fixed. Then the mage goes back.

8b. The caster profile (the mage)
---------------------------------

This is the design written for pre-flight A8, from the fight, targeting, rest and training
code and the world snapshot. Everything keys on spell facts, never on the class, so the same
code serves a warlock, priest, druid or shaman later. The paladin's behaviour stays as it is,
checked by golden tests.

**Stage 0, done before T-0.**
- Casting from range (V164):
  - Spell reach comes from `content/tbc/spell-reach.json`.
  - A caster is a class whose starting bar has a ranged attack with a cast time.
  - With a ranged attack usable, it casts from where it stands. It steps in on the client's
    "out of range", steps aside on "no line of sight", faces on "not facing", never moves
    during a cast and never toggles its staff. Out of mana, it fights with the staff.
  - A kill made from range is looted by walking to the corpse.
  - It rests below 55% mana, carries 20 waters, and keeps Frost Armor's clock between fights.
  - It draws no heal line.
- Roles and order (V165):
  - Frostbolt and Arcane Missiles are strikes, and go on the bar when trained.
  - The other new roles: Frost Nova `root`, Polymorph `cc`, the conjures `conjure`.
  - Order: at contact an instant (Fire Blast), before the mob has come for it a slow
    (Frostbolt), else the bar's order.

**Mage facts.**
- Trainers:
  - Khelden Bremen, upstairs in Northshire Abbey (-8851.6, -188.2, z 89.5). He teaches Arcane
    Intellect at 1 (10c); Frostbolt and Conjure Water at 4; Fireball 2, Conjure Food and
    Fire Blast at 6 (1s each).
  - Zaldimar Wefhellt, upstairs in the Lion's Pride Inn (z 63.9). He teaches Polymorph,
    Frostbolt 2 and Arcane Missiles at 8 (2s each); Frost Nova, Conjure Water 2 and Frost
    Armor 2 at 10; Fireball 3 at 12; Frostbolt 3 and Fire Blast 2 at 14; Fireball 4 at 18.
  - Both upstairs routes plan complete offline.
- Water: Brother Danil by Northshire's wagons sells water (item 159) and bread (4540).
- Conjured Water is spell 5350 at level 4 and Fresh Water 2288 at 10. Muffin 5349 at 6,
  Bread 1113 at 12.
- A Kobold Vermin (42-55 hp) takes about three Fireballs, 90 of a level-1 mage's 165 mana.

**Stage 1, hours 1.5-15, inert for the paladin, live at the switch.** In order of value:
1. **Conjuring and bag use.**
   - Put `conjure` in the training placements.
   - After a meal, cast a conjure twice when fewer than 4 of its item are in the bags.
   - When the bar's water or food slot is empty, `Rest` uses the best consumable in the bags
     by the same right-click that equips gear (`Vendor.use_item`).
   - `service()` stops buying a role the character conjures.
2. **Rest.**
   - `Rest.until_both(0.9, 0.9)` eats and drinks together.
   - It presses again when a gauge has stalled for 3 s short of the target (at most twice).
   - With nothing left to consume, it waits on regeneration while the gauge still rises.
3. **Measured mana line.**
   - `Fight.mana_spent` per kill.
   - The line is 1.15 times the median of the last 10, clamped to 0.35-0.85.
   - Read by `policy._recover` and the hunt's `_ready_to_pull`.
4. **Frost Nova (root) at 10.**
   - Pressed at contact.
   - Then `_step_clear`: S held 2 s (about 9 yards) still facing, a side step if blocked,
     then Frostbolt.
   - Its line `fight.nova_below` is a learned choice ("1.00", "0.60", "0.35"), judged like
     the heal line.
5. **Buffs out of combat.**
   - `Fight.buff_up()` before each pull and after meals, never in combat.
   - Frost Armor, and Arcane Intellect with Alt (a friendly spell).
6. **Caster gear.**
   - Staves (spell 227) and Wands (5009) proficiencies.
   - Slots: InventoryType 17 as main hand for classes without a shield, 23 as off hand, 26 as
     ranged.
   - `caster_score` for classes 5, 8 and 9: 10 Int + 8 Sta + 6 Spi + Agi + armour/10, plus
     staff dps x2 or wand dps x10.
7. **Schema 17**, which only appends fields:
   - `bars.in_range`/`bars.out_range` (IsActionInRange per slot);
   - `target.near28` (CheckInteractDistance 4);
   - `bars.aura_up` (the slot's spell among the player's buffs);
   - `bars.autorepeat`;
   - `combat.attackers` (from the combat log).
   - Installed at the switch's client restart. Painted range turns striding into one steered
     walk.
8. **The hunt's stand-off.** `Hunt.standoff_yards` is 18 for casters: the hunt stands short
   of each station.

**Stage 2, after the switch, largest measured loss first.**
- Polymorph when `combat.attackers` is 2 or more.
- Arcane Missiles, and a wand's Shoot.
- Tab-plus-mark pulls at 25-35 yards.
- The `hunt.standoff` choice (12, 18 or 26 yards), once its outcomes vary.
- Class masks on guide nodes.
- Buying Ice Cold Milk.

**Risks to measure live.**
- Spell errors reaching `ui.error_last`, and the delay from a press to `bars.casting`: the
  dry run's evidence.
- Aggro at the stand-off: the share of first casts with `target.attacking_me`.
- Loot from ranged kills: the first far kill.
- The upstairs trainers: the 10c Arcane Intellect visit.
- Starting water and food counts: the first tick.
- The Mage button's place on the create screen: T-0 B3.

9. Schedule and checkpoints
---------------------------

- **Hours 0-1.5:** T-0 (§6), including the mage's dry run.
- **Hours 1.5-15, Friday night:** Testvvi at Riverpaw. The band rule then takes it to Westfall.
  Meanwhile the caster profile is built out from the dry run's evidence (§8b), and the switch is
  rehearsed with fakes.
- **Hour 15, Saturday about 10:30:** the switch to the mage.
- **Hours 15-36:** the mage through Northshire and Elwynn. Changes come from the mage's measured
  losses, caster ones first.
- **Hours 36-44, Sunday morning:** the mage plays on; consolidation (§11).
- **Hours 44-48:** freeze, the final report, and the loop playing on.

| Hour (BST) | Go if | Otherwise (named fallback) |
|---|---|---|
| 0 (Fri 19:30-21:00) | §6 done; loop playing hybrid on Testvvi; the mage made, verified and one session played | **Nine-hour config** (§6 B2); **Mage later** (dry run at hours 4-8) |
| 4 (Fri 23:30) | Testvvi 13.5 or higher; no stop over 15 min; 6 or fewer deaths since T-0 | **Last good build**: revert the newest change and resume |
| 8 (Sat 03:30) | Testvvi 14.0 or higher (should: 14.5); the dry run's caster losses fixed offline | **Rib floor**: when 12-20 stalls, grind its rib (A2 b) while the failure is fixed, one session per place at most |
| 12 (Sat 07:30) | Testvvi 14.6 or higher (should: 15.4); switch rehearsed with fakes; `danger.json` counts a level band (§8) | **Largest loss only**: no new features until the pace holds |
| 15 (Sat 10:30) | The switch is made; the mage is in the world and verified | **Attended switch**: Testvvi plays on while it is fixed; retry within 2 h |
| 24 (Sat 19:30) | The mage at level 5 or higher (should: 8) | **Testvvi covers**: if the mage cannot fight, Testvvi plays while the caster profile is fixed |
| 36 (Sun 07:30) | The mage at level 7 or higher (should: 12) | **Largest loss only** |
| 44 (Sun 15:30) | Freeze begins; report drafted | none |
| 48 (Sun 19:30) | Report pushed; loop playing the frozen build | none |

Testvvi's go thresholds are the must pace, about 1,780 XP/h; the mage's follow Testvvi's own
times. Misses at two checkpoints in a row get a STATUS entry that names the cause.

10. Unattended safety
---------------------

**Layers**, each working without the ones above it: (1) the in-game ladders, unchanged: the
watchdog, ribs, retry and pass-over, hearth when wedged, and the death-trap rules; (2) the loop,
which ensures servers and client before each session, backs off after quick failures, restarts the
client and switches characters; (3) the keeper timer (Q5), running `keep.sh servers loop` every 5
minutes; (4) the executor's heartbeat. The loop never needs the executor: a stalled executor stops
improvements, not play.

| Failure | Detected by | Automatic response | Executor |
|---|---|---|---|
| Client closed or crashed | No Wow.exe in `tasklist.exe` | Once the desk is idle, launch through PowerShell `Start-Process` (never `cmd start`); the session's `--reconnect` logs in | Read `unknown-stage.npy` if login stops |
| Client hung, or stuck on "Retrieving character list" | Three sessions in a row cannot read the strip or quest log | Close with `taskkill` (no `/F`), wait up to 120 s for Logout in `Char.log`, launch, log in | Same, attended |
| Disconnect, or a login that does not take | The watchdog reconnects (V36, limit 3); login tries twice | The next session retries. After 3 quick failures the loop waits 5 min, then restarts the client | Read the kept frame; never print credentials |
| mangosd or realmd died | Port 8085 or 3724 closed | `keep.sh servers` starts it from `~/cmangos/run/bin` with the same `-c` and `-p` flags, logs to `~/cmangos/logs`, and waits up to 5 min; the client reconnects | After three crashes in an hour: stop restarting, park, read `Server.log` |
| WSL restarted | `/tmp` empty; no loop, no servers | The keeper timer restarts both; loop state is in `var/loop/` | The heartbeat does it if there is no timer |
| Windows restarted, slept or locked | Nothing | None | The operator's checklist (§4) is the only guard |
| Death loop | Three or more deaths in 20 minutes | In game: V63, V92, V157, V161 | Stop at the boundary, park, trace the fights from screenshots and evidence, fix generally, resume |
| Stuck in one place | Watchdog at 900 s; V99, V109, V145, V146 | A7: three sessions with no XP start the next with a hearth home | Look at a screenshot after the first session with no XP (the inn cost an hour before anyone looked) |
| Disk | `keep.sh status` | The loop stops under 50 GB free (runs grow about 72 GB in 48 h) | Prune only with the operator's consent |
| Executor stalls or hits usage limits | Nothing in the loop depends on it | Play continues on the last pushed build | On return: read the ledger and the loop log, and resume the cycle |
| Tutor unavailable | "tutor unavailable" in the log | The routine floor (V43); the recovery choice learns from it | Q8 |

**Safe park.** When play must stop for more than 10 minutes, stop the session, then close the
client gracefully; a character left standing with no session can die (24 September). **Before
focus is taken,** any launch, login or switch outside a session needs 10 minutes of desk idle.

11. Consolidation, freeze and the final report
----------------------------------------------

**Hours 36-44: consolidation.** Nothing new goes in; each removal gets its own commit and
DECISIONS row. The map was drawn on 25 Sep by a read-only survey (import graph plus grep).
Step 0, turning off live learning, went in before T-0 as V174. The order, each step
leaving the suite green:

1. **`HYBRID_SAMPLE`.** Code: `jev/play/runtime.py:5, 36-41, 385-386`. Tests:
   `test_play_runtime.py:687-718`.
2. **The tutor/ab arms,** after the B2 rollback window has closed.
   - Code: `play/runtime.py:38, 171-178, 235, 315`; `cli.py` `--play-dispatch`;
     `start_teaching.py --mode/--dispatch`; `session_loop.sh`'s arm file; `keep_status`;
     `session_report --compare`.
   - Tests: the `composition` fixture in `test_play_runtime.py:91-123` (force
     `_ask_tutor`); `test_keep.py`'s arm test.
3. **The decision learner** (about 4,100 source lines, 2,300 test lines).
   - Remove: `learn/{worker,registry,distill,dataset,evidence,parquet,promote,grade}`,
     `eval/*`, `run/background.py`, the grading parts of `episode.py`, the `learned` hooks,
     and sklearn/pyarrow from the `learn` extra.
   - Delete the tests `test_distill`, `test_learning_cycle`, `test_grade_run`,
     `test_worker_cli`, `test_background_services` and `test_eval`.
   - Move the helpers other tests import: `test_execution_evidence` imports from
     `test_grade_run`; `test_background_runtime` from `test_teacher`.
4. **The motor handover and canary, then motor training** (about 1,100 lines).
   - Keep `record`, `finish_episode`, `ingest_run`, `records`, `remember_controls` and the
     `_qualified` checks (`session_report --motor` uses them).
   - This removes the flaky test (`test_play_runtime.py:126-159`).
5. **Dead code.**
   - Remove `tools/probe_{input,motion,reaction,travel,turn,unstick,window}.py`,
     `contact_sheet.py`, `recover.py`, `verify_facing.py`, `calibrate_camera.py` and
     `env_lock.py`, which have no caller.
   - Remove the small functions the survey listed.
   - Only tests call `jev/clients/live.py`, `run/heartbeat.py`, `run/journal.py`,
     `eval/board.py` and `learn/promote.py`: remove them with their tests.
6. **Folds.**
   - One `.env` parser: `tools/login.py`'s does not strip quotes, unlike the reconnect's.
   - One `session_log` and loop-log parser, shared by `keep_status`, `session_report` and
     `session_check`.
   - `FakeClient` moves into tests.

Steps 0-5 remove about 7,250 source lines (18%) and 3,600 test lines.
- **Retired imitation paths.** Remove what no configuration reaches since V158: `HYBRID_SAMPLE`
  sampling, the teach-first arms (`tutor`, `ab`), the student canary and adaptive handover, and
  the flaky test. Stop training retired students inside live sessions (CPU, and the lock waits
  behind V74 and V153), but keep recording the corpus. Before any removal, prove there is no
  caller: an import graph from `jev.run.cli` and the tools, plus grep.
- **Scripts.** Nothing left in `/tmp`; the loop, keeper and character tools have tests with fakes.
- **Docs.** The ARCHITECTURE.md header (the learning contract is now V158's outcome-learned
  choices, with the tutor as the fallback after a routine fails); ROADMAP's status table;
  `docs/OPERATING.md` (the loop, keeper, campaign, switch and restarts); the README quick start.

**Freeze, from hour 44.** Only reverts of a measured regression, and docs. The loop plays the
frozen build.

**Final report**, the STATUS entry "the forty-eight hours, against the plan", pushed by hour 47:
the goal table by tier; each character's start and end levels, hours and XP/h; the block
scoreboard; every change, kept or reverted, with its measured effect; deaths and stuck events over
time; learner gains (choice points, danger map, tutor rescues); what failed, and the next backlog
by expected value; and how to run it, in one paragraph. The ledger is closed and pushed with it.

12. Risks
---------

| Risk | Mitigation |
|---|---|
| V158-V161 misbehave live (never run) | T-0 validation with a set revert order (§6 B2) |
| The caster profile fails live (the first non-paladin class) | The T-0 dry run; hours 1.5-15 to fix it; Testvvi covers after the switch (§8) |
| 12-20 fails in new places: Westfall's frame, the bind, the three-floor farmhouse, Lakeshire's inn | Watch the first Westfall hours; one session per place at most; the rib floor (A2 b) |
| The last guide ends before 20 and the character idles | A2 b, tested before T-0 |
| Same-level packs raise deaths | Lever 3 first; the death-loop rule; V161 routes |
| Glue screens unmeasured, the intro plays, names collide | Measure at T-0 and dry run; wait out the intro; DB pre-check, then the server's answer |
| Flights between Westfall and Redridge need Stormwind's node, and the guide never goes there | Expect to walk the one crossing; do not chase flights |
| Executor stalls | The loop is autonomous; front-load A1-A3; heartbeats cost one screen |
| Windows restarts, sleeps or locks | The operator's checklist; nothing recovers it unattended |
| WSL restart, server crash, client hang | §10 table |
| A deploy is imported half-written | Dev worktree, hold flag, offline check; one write per edit in the live tree |
| Credentials or personal data pushed to the public repo | `.env` and `var/` are ignored; names are generated; read `git diff --cached` before each commit |
| Resurrection sickness (level 11 and up) after a Spirit Healer rescue (V63, V92) | Watch for deaths just after a rescue; eat to full before the next pull |
| The tutor and executor share one subscription | Hybrid keeps tutor calls low; Q8 |

13. Hard constraints (verbatim)
-------------------------------

- Never run the untracked `look_tmp.py` or the ad hoc scripts in `captures/targeting/`. They attach to the client and send input.
- The repo remote (github.com/chalkychalk42/jev) is PUBLIC: never commit `.env` credentials or personal data.
- The addon (StatusStrip) must never actuate.
- The bot never fights the operator for the desktop: check idle before taking focus.
- "The bot itself does not talk."
- `session.py` must not contain the word "forever".
- Only general fixes, no scenario-specific rabbit holes. Time-box any single location to about one session, then let the fallback ladder handle it.
- While the live loop runs, each code edit is ONE write, with definitions written before their users. A half-edit crashed a session once.
- World DB credentials live in `~/cmangos/run/etc/mangosd.conf`: read-only queries, never print the credentials.
- Push committed work to origin main.
