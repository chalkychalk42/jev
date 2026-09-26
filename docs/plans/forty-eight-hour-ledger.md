Forty-eight-hour ledger
======================

The running state of `docs/plans/forty-eight-hour-session.md`, newest last in each section.
Read "Now" after every compaction. Times are BST (the Windows clock runs about 25 s ahead).

Now
---

- **T-0 was Friday 25 Sep 23:14 BST**, when the operator said "start the 48 hours".
  - Hours count from then: hour 15 is Saturday 14:15, hour 36 Sunday 11:15, hour 44
    Sunday 19:15, and hour 48 Sunday 23:15.
  - The campaign's switch deadline was moved to Saturday 14:15.
- **T-0, step by step:**
  - 23:14: the desk had been idle 4.5 h and the servers were up.
  - 23:15: the client was launched; the login put Testvvi in Goldshire at level 13; the
    strip read as schema 17.
  - Session 134 (hybrid) started 23:16 with all three proof lines: danger 1,217 cells,
    247 station visits, and the band armed at 13.
  - Session 134 stopped after 371 s. The food restock walked into the Lion's Pride Inn,
    ended upstairs and timed out, and one failure stops a session (`--retries 1`).
    V175 (26750a7): an unreachable restock is blocked for the step and never stops the run.
  - Session 135 (908 s):
    - 24 kills, about 650 XP, 0 deaths, 0 tutor records.
    - The heal line 34 choices and 24 outcomes; stations 42 and 21.
    - It bought 10 food and walked about 1,400 yards to Riverpaw.
    - At the client restart after it, Testvvi showed "(Ghost)" at character select: it
      died at the session's end.
  - B3, 23:39-23:43:
    - Glue screens measured (231f870).
    - `character.py enter` made **Itheamar, a human mage**, entered the world and checked
      it: **M2 met**.
  - Session 136, the mage's dry run by hand (534 s, stopped):
    - A Threat Within accepted.
    - Its turn-in at Marshal McBride timed out 6.8 yards short, at the Abbey door
      (16 stuck), and failed over to the 1-3 rib.
    - BIND_HEARTH then walked the level-1 mage toward Goldshire. It was attacked in melee
      and died twice.
    - The one fight: Frost Armor pressed and cast, read as unanswered, and cast again
      (60 mana each, 73% of 165); then one Fireball, then the staff.
    - Fixes to make:
      - a new character's home is where it starts;
      - a press is answered by its mana leaving;
      - a caster keeps its lasting buffs up out of combat, not in it.
  - 23:54: back to Testvvi by row 2 (`enter --name Testvvi`); the loop resumed at
    session 137.
  - Sessions 137-138: the band rule finished 1-12 with the Riverpaw bounty's hand-in left
    behind (850 XP), V177. At 00:20 the playhead was pointed back at that hand-in; session
    139 handed it in (+0.083 level) and 1-12 finished, and session 140 started 12-20.
  - 00:30-01:10, the mage's Abbey door, offline:
    - The walk to Marshal McBride was planned at 49.7 yards. Three learned passages bent it
      into 124, and a straight leg to one inside the Abbey met its front wall beside the
      door. McBride hand-ins took 15-26 s before any passage there and 120-240 s after.
      V178 stops taking passages.
    - The second attempt selected McBride by his plate, but the view moved 76 px before
      the click frame, past the anchor's half-plate width: "no eligible target geometry".
      On that frame the ring bracket finds his chest: V180.
  - Session 140 (905 s): bound at Sentinel Hill, Thor's flight node, Patrolling Westfall
    accepted; then two deaths at its objective, the level 17-18 Riverpaw Taskmasters, for a
    level 14 quest. The generator had placed the Gnoll Paws among the densest droppers,
    whatever their level: V179 keeps them within two levels of the quest and regenerates
    12-20 (Mongrels 13-14 now); 1-12 comes out unchanged.
  - Session 141 (689 s) played 1-12 again: once 12-20 had saved its place, the next run
    read 1-12 as nothing remembered. About 6,700 yards walked toward Goldshire, four kills,
    and 1-12 marked finished again. V181 starts a run on the guide the playhead names.
  - 00:55 deploy of V178-V181 (c7cf1cc). Session 142 began dead: 141's run had ended as a
    fight began, since a finished guide stopped the run at once and would not fight. V182.
  - Session 142 (905 s, +0.102): two deaths on the road to the Mongrels among four attackers,
    the second 42 s after getting up 32 yards short of the first body. Of the seven deaths in
    play since T-0, four came within 45 s of getting up: the walk after getting up was exempt
    from keeping clear of the death, being inside its 35 yards. V183.
  - Session 144 (exit 1, 758 s): two deaths to a Dust Devil (18-19) wandering into the
    Mongrels' camp, the second after getting up 32 yards off; the Spirit Healer, the hearth
    to Sentinel Hill, and a repair walk stuck in the inn's doorway stopped the session.
  - Sessions 145-147: at Stendel's Pond the strip reader was misled by scenery behind the
    strip's corner; the fight went blind every other second, the character died, and the
    next two sessions could not read the strip at all. V184 reads the strip on the grid of
    its last good read, kept in `var/radio-grid.json`; the file was seeded from the
    measured grid for the restart.
  - Session 148: started a ghost; two deaths to Fleshrippers, one 23 s after getting up at
    half health, one under resurrection sickness after the Spirit Healer.
  - 02:06-02:30, the mage's check (Itheamar, `captures/live-mage-check-1.log`): A Threat
    Within handed in to McBride at once (V178, V180); Kobold Camp Cleanup done by Fireball
    from range, no melee walk; level 2. Faults: out of water and too poor, the grind walked
    to the merchant and back after every kill (V186); Khelden Bremen's click upstairs failed
    while the view settled ("point no longer has current geometry", three stale hovers).

- **25 Sep 14:10.** Pre-flight A is under way, with the loop stopped and the client closed.
  - The operator logged Testvvi out at 09:13, in Goldshire, at level 13.
  - T-0 is Friday 19:30.
  - The campaign is `var/campaign.json`: Testvvi until level 20 or Saturday 10:30, then the
    human mage Itheamar (row 3, not made yet).
- **Pre-flight A done:**
  - A1 is the repo loop, the keeper and the status screen (58fa756).
  - A2 (a) is the band rule, V162 (bde61ca).
  - A3 put quests 16, 21, 40 and 60 into Testvvi's playhead (not in git; `var/`).
  - A4 is the campaign tool and the create screen, unmeasured (486f754).
  - The danger band from L-1 to L+2 (b02409a).
- **Pre-flight A, since:**
  - A2 (c): a zero-XP quest leaves the route, V163 (3a274bf). 12-20 now starts at
    Patrolling Westfall.
  - A5: the scoreboard, `session_report.py --blocks 2` (29f4947).
  - A8 stage 0: a caster casts from range, V164 (fc2ab85). Roles and the spell order, V165
    (2236784). Planned routes to Khelden Bremen, upstairs in the Abbey, and to Zaldimar,
    upstairs in the Lion's Pride Inn, come back complete.
  - A6: the flaky test did not recur in 3 focused runs or 4 full ones. It stays in Issues.
  - A2 (b) is deferred: neither character reaches the end of 12-20 inside the window.
- **Caster stage 1 (§8b), 25 Sep 13:40-14:05:**
  - Done:
    - conjuring, and meals from the bags, V166 (14f92ec);
    - eating and drinking at once, and an 18-yard stand-off in the hunt, V167 (dc7cb28);
    - Frost Nova at contact, then a step clear, V169 (5d14c38);
    - the mana line measured from kills, V170 (93aa6d1).
  - Also V168 (e6741c1): a trainer teaching two spells or more is worth up to 3,000 yards,
    so Testvvi trains from south Westfall.
  - Open, for the session: out-of-combat buffs, caster gear, schema 17.
  - The hunt's stand-off and the rest changes are inert for Testvvi.
- **Also, 25 Sep 14:10-14:55:**
  - V171, schema 17: range per slot and the attacker count, installed with the client
    closed (5f31717).
  - V172: the pack heal line (d9c2b47).
  - V173: a review of the whole day's diff by a subagent found 10 defects, all fixed
    (0b0706a). The worst three:
    - the mage's conjured stock ran dry mid-hunt and stopped the session;
    - character select could be read as the create screen;
    - a failed switch left the status green.
  - A meal now takes another food or drink when the first ends short, and waits on
    regeneration rather than stopping.
- **Also, 25 Sep 15:00-15:25:**
  - V174: no student is trained inside a live session (02db32b). A survey found three
    learner threads in every session re-reading hundreds of megabytes for students that
    never act.
  - Consolidation step 1 (9b9cc58).
  - The consolidation order is now in the plan's §11.
  - `tools/session_check.py` (2e68841).
  - The keeper timer is installed, held off by `var/loop/stop`.
  - **Pre-flight is complete.** The loop is stopped, the client is closed, and the tree
    is at origin/main.
- **Offline checks at 14:05:**
  - The 1-12 route starts at A Threat Within, with 41 quests (Give Gerard a Drink pays
    nothing, V163).
  - The 12-20 route starts at Patrolling Westfall, with 27 quests.
  - Planned routes: Goldshire to Sentinel Hill 1,656 yards, Westbrook to Sentinel Hill 799.
- **T-0 runbook (§6), step by step.** WINPY is `/mnt/c/forever-win/Scripts/python.exe`.
  - **B1, state.**
    - `tools/keep.sh status` shows the servers up and no loop running.
    - `$WINPY tools/desk.py 600` exits 0: nobody has touched the desk for 10 minutes.
    - `tools/keep.sh client` launches WoW.
    - `$WINPY tools/login.py` should end with "in the world: True". The list's selection is
      Testvvi (row 2).
    - `$WINPY tools/observe.py`: **schema 17** (installed 14:10, V171), key 548c8582,
      level 13.
    - If the strip does not read: close the client and copy
      `captures/addon-backup/20260925T131155-StatusStrip` back over
      `/mnt/c/Games/WoW243/Interface/AddOns/StatusStrip` (schema 16; the decoder reads both).
      Then relaunch and log the issue.
  - **B2, two sessions on Testvvi.** `rm var/loop/stop`, then start the loop. Once the
    second session starts, touch `var/loop/stop`: the timer then leaves the loop off.
    - The log must show `danger: ... cells`, `choices: ... hunt station visits`,
      `guide alli_human_1_12: outgrown at level 13, then ally_human_12_20.json`, some XP and
      no Traceback. The tutor is asked only after a routine fails.
    - Expected route: Riverpaw's last armband and its hand-in, then the band rule ends the
      guide. The next session starts 12-20 at Patrolling Westfall (V163 drops Thunderbrew
      Lager).
    - **Rollback map.** Revert the commit the failure points at: the Traceback's module,
      or the log line of the thing that went wrong.
      - What reaches the paladin:
        - V174 learners off (02db32b);
        - V173 rest and the review's fixes (0b0706a);
        - V172 pack heal line (d9c2b47);
        - V171 schema 17 (5f31717, plus the addon backup);
        - V168 trainer reach (e6741c1);
        - V163 worthless quests (3a274bf);
        - V162 band rule (bde61ca);
        - V161 danger (0dbeb58, b02409a);
        - V160/V159 heal line and look (0254706);
        - V158 choices (7e1d7f7).
      - Caster-only, inert for the paladin, proven at the mage's session: V164-V167 and
        V169-V170 (fc2ab85, 2236784, c999e25, 14f92ec, dc7cb28, 5d14c38, 93aa6d1).
      - After two failed reruns, go back to the nine-hour configuration: arm `tutor`, and
        V158-V174 reverted in reverse order. Record it.
  - **B3, the glue screens and the mage.**
    1. `tools/keep.sh client-restart`.
    2. `$WINPY tools/character.py enter` stops at character select with "not measured".
    3. `$WINPY tools/character.py measure --focus`, then read the PNG. Measure
       `CREATE_NEW`, `CHARACTER_ROW_FIRST` and `CHARACTER_ROW_STEP`.
    4. `measure --click FX,FY` at Create New Character, then read the create screen. Measure
       `RACE_BUTTONS["human"]`, `CLASS_BUTTONS["mage"]`, `NAME_BOX`, `CREATE_ACCEPT` and
       `CREATE_BACK`.
    5. `measure --click` at Back.
    6. Set the constants in `jev/clients/session.py` in one edit. Run the session and
       character tests, then commit and push.
    7. `$WINPY tools/character.py enter` makes Itheamar, waits out the introduction and
       checks key, class 8 and race 1.
    8. One session on the mage: `timeout 1080 $WINPY -u tools/start_teaching.py --run
       --dispatch hybrid > captures/live-N.log 2>&1`. The proof is A Threat Within
       accepted, a Fireball kill with no melee walk, and a drink.
    9. `tools/keep.sh client-restart`, then `$WINPY tools/character.py enter --name Testvvi`.
  - **B4, arm.**
    - The keeper timer was installed at 15:20 on 25 Sep and tested under systemd. With
      `var/loop/stop` set, it only checks the servers.
    - Removing `var/loop/stop` lets it start the loop within 5 minutes; start it by hand
      rather than wait.
    - Start the loop.
    - Start the heartbeat.
    - Add the T-0 row here, write a STATUS entry and push.

- **The heartbeat, from T-0.**
  - Keep a background `tools/watch.sh --sessions --max 90` running. When it exits, it
    wakes the executor.
  - Then run `tools/session_check.py` on the session that ended, and act on anything red
    or `[!!]`.
  - Restart the watch.
  - Every 2 hours, add a row from `.venv/bin/python tools/session_report.py --since <the
    T-0 session> --blocks 2` to the table below, judge the trial, then build the next
    change in the dev worktree.

Blocks
------

| Block | Hours | Character | Level from-to | XP/h | Deaths/h | Stuck/h | Quests/h | Tutor calls/h | Change under trial | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 (sessions 134-143, 1.65 h played) | 0-2 | Testvvi | 13.0-13.45 | 2,682 | 4.25 | 52.8 | 2.4 guide steps/h | 0 | V175-V177 at T-0; V178-V181 from 142, V182 from 143 | XP/h above the nine-hour 2,108. Deaths high: two at the Taskmasters (V179), one standing through a restart (V182), two on the road in fights with more than one attacker (next) |
| 2 (sessions 144-150, about 1.8 h played) | 2-4 | Testvvi | 13.45-13.78 | 2,466 (145-150) | 10.1 | 11.4 | 0 guide steps/h | 0 | V178-V189 | Deaths: Westfall's units two to five levels up (Brutes, Bandits, Dust Devils), and faults fixed as found (V182-V184, V188, V189). Stuck events down about fivefold (V178). Hour-4 checkpoint: deaths missed |

Trials
------

| Change | Deployed | Predicted effect (metric) | Proof it fired (log line) | Judged | Kept/reverted |
|---|---|---|---|---|---|
| V178 walks no longer go by learned passages | next boundary after session 141 | stuck events a walk (7.0 over 215 walks, sessions 88-140) and walks not arrived (15 of 215) fall or hold; guards XP/h and deaths | `route memory: N blocked spots kept clear of; 151 old passages not taken` | | |
| V179 item droppers within two levels of the quest; 12-20 regenerated | with V178 | deaths/h on 12-20 objectives fall; Patrolling Westfall done with no death | the hunt's target is Riverpaw Mongrel (`hunt.request` wanted_name_id) | | |
| V180 a moved plate is found again when it is the only one of its colour | with V178 | `target-no-proposal` frames (7 in the last 80 runs) and "no eligible target geometry" failures fall | a `target.proposal` after a selection whose plate moved | | |
| V183 a walk from inside a death's reach keeps the distance it has | session 144 | deaths within 60 s of getting up (4 of 7 in sessions 134-142) fall | a walk after getting up that bends round the body (`round where the character died`) | | |

Issues
------

| Seen | Issue | Evidence | State |
|---|---|---|---|
| 25 Sep 12:00 | The adaptive canary-student runtime test failed once in 2 full runs (planning agent) | Not seen in 3 focused runs or 4 full runs since | Watching; remove it with the retired paths (§11) |
