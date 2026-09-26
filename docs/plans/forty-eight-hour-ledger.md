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
  - Sessions 151-155 on V188-V191: 14-16 kills a session, deaths 1, 0, 1, 1; Testvvi 14 at
    03:41. Blind melee (V191) fired 6-11 times a session; fights given up on the facing
    look fell to 0-3.
  - 04:13-04:31, the mage's second check (`captures/live-mage-check-2.log`): Arcane
    Intellect trained upstairs; Wolves Across the Border 1 of 8 in four kills (V192);
    the step's failover rib by Goldshire's road killed the level 2 mage twice (V193).
  - Session 156 began in the Defias Smugglers' camp where the visit had logged Testvvi out:
    four attackers at 26 s. After the Spirit Healer and the hearth, the repair walk wandered
    the Sentinel Hill inn for 110 s and ended on a crate in a corner of its loft, where it
    moves 0 yards in every direction (`tools/probe_unstick.py`, jumps and turns included).
    The grind then stopped for "durability is low" 17 times (V194). The hearthstone is out
    until about 05:37; the wedged-walks rule takes it home then.
  - 04:53-05:45: three mage sessions (`captures/live-mage-check-3.log.1-3`) while the
    paladin's hearthstone cools. Level 2.4 to 3.48; deaths 1, 5 and 3. Restock walks for
    water it could not pay (V195), repair walks after each copper (V196), and five deaths
    getting up at the body among level 5-6 units (V197).
  - Session 158 (05:41-05:57, V197): the wedged-walks rule took the hearthstone at 05:45 and
    the character came down to Innkeeper Heather. The first leg out, 37 yards through the
    inn's door, met the wall four yards west of it (V198); the re-plans drifted into the
    corner by the stove, then out and back onto the crate behind the inn. Both spots are
    blocked in route memory now (9 and 2 hits). Session 159 started 4 s before the hold and
    was stopped at 65 s (`captures/teaching/STOP`).
  - 05:58-06:48, the mage's fourth visit (`captures/live-mage-4.log.1-3`) while the
    paladin's stone cools again: session 1's restock passed Brother Danil for Ben Trias in
    Stormwind, 1,030 yards, since every merchant near had one failure (V199); the walk
    back from Stormwind timed out its repair (V201) and died twice; too poor to repair at
    Goldshire. Level 3.48 to 3.83 by 06:19.
  - Sessions 160-162 (06:40-07:18): 160 hearthed off the crate at 06:46 and its repair then
    walked for the Defias Profiteer in Moonbrook (V203, stopped by hand); 161 walked from the
    inn to MacGregor with no stuck event and repaired (gear 100%), 872 XP, 0 deaths; 162
    played on. Testvvi 14.33 at 07:03.
  - 07:18-07:55, the mage's fifth visit (`captures/live-mage-5.log.1-2`) on V202-V204:
    session 1 made 5 kills in 6 fights, level 4.0 to 4.44, one death on the Goldshire road to
    level 5-6 units; the session began with the hearthstone and a too-poor repair at Godric
    Rothgar (V206) and walked 675 yards for cheese (V205). Session 2: 10 kills, 0 deaths, 3
    blind casts (V204), 7 hint searches (V202); level 4.73 by 07:51, 24 copper.
  - Session 163 (07:52): died at once to a level 13 Fleshripper hovering eight yards off,
    blind melee swinging "too far away" (V207); then 1,259 XP, the run's best rate (5,038/h).
    Where 161-163's skill time went: hunting 42% (walks between stations 24%), fights 23%,
    the level-14 training trip to Goldshire 14%.
  - Sessions 164-165: 1,356 and 1,162 XP (5,424 and 4,650/h), 0 deaths; V207 stepped in 4
    times in 164. Testvvi about 14.66 at 08:38, past hour 12's 14.6.
  - **Deviation from §8:** before hour 15 the plan allows the mage its T-0 dry run and one
    more check. It has played about 14 sessions (about 3.3 h, level 1 to 5.41): four in the
    paladin's hearthstone cooldowns (sessions 156-159, the paladin wedged), the rest to check
    caster fixes live (V195-V208, found on them). No more before the switch; the switch is
    V208's live check. Testvvi reached 5 at 2.1 h of play; the mage at about 3.2 h.
  - 08:38-09:12, the mage's sixth visit (`captures/live-mage-6.log.1-2`): the first V206
    session remembered nothing yet, took the hearthstone and was too poor at 24 copper, then
    wrote `character-73ce06a8.purse.json`; Skirmish at Echo Ridge done, its hand-in walk stuck
    on its first leg inside Echo Ridge Mine and failed over, then was handed in; level 5.05.
    One death: a Defias Cutpurse behind the mage, between it and the camera, and Fireball
    "not in front" 40 times at full mana (V208).
  - Sessions 166-168: 166 made 2,373 XP (9,328/h, the run's best) on three quest steps; 167
    died twice in a Defias pack at the Furlbrow farm (The Forgotten Heirloom), 581 XP; 168 died
    twice to a level 19 Dust Devil north of Sentinel Hill, the first time fighting a Young
    Goretusk it had chosen instead (V209), and got up at the body beside it. Testvvi 14.9.
  - Sessions 169-178 (09:57-11:54):
    - Testvvi reached level 15 at 10:30.
    - 172 made the run's best, 2,517 XP (9,440/h).
    - V209 alone made worse an attacker found nowhere by Jangolode Mine: 33 turns round in
      four minutes. V210 followed within the session.
    - Sessions 173-177 were pinned about 45 minutes by a Defias Smuggler at 6% health throwing
      knives from out of sight on a slope: 74, 41 and more fights "not visible". V211 (a plate
      under our strip) did not free it.
    - V212's first deploy met the body's "combat before travel" and re-armed the hunt about
      once a second. Its `choices.json` rename then failed ("Access is denied" over the WSL
      share) and ended sessions 176 and 177 (exit 1).
    - The body's leg start and a retried rename followed. Session 178 began after a death,
      which freed it.
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

- **Deviation from §11, 26 Sep 16:05:** the consolidation was prepared on a side branch from 13:00 (steps 1-6: 6,923 source lines and 3,207 test lines out), replayed onto w48 with its DECISIONS rows renumbered V222-V229, and deployed at hour 17 rather than hours 36-44: a day of play on it before the freeze, where the plan left eight hours. The addon's field table is unchanged (the generated `Fields.lua` is identical), and the loop's launch flags pass the real offline check (a new test).

Blocks
------

| Block | Hours | Character | Level from-to | XP/h | Deaths/h | Stuck/h | Quests/h | Tutor calls/h | Change under trial | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 (sessions 134-143, 1.65 h played) | 0-2 | Testvvi | 13.0-13.45 | 2,682 | 4.25 | 52.8 | 2.4 guide steps/h | 0 | V175-V177 at T-0; V178-V181 from 142, V182 from 143 | XP/h above the nine-hour 2,108. Deaths high: two at the Taskmasters (V179), one standing through a restart (V182), two on the road in fights with more than one attacker (next) |
| 2 (sessions 144-150, about 1.8 h played) | 2-4 | Testvvi | 13.45-13.78 | 2,466 (145-150) | 10.1 | 11.4 | 0 guide steps/h | 0 | V178-V189 | Deaths: Westfall's units two to five levels up (Brutes, Bandits, Dust Devils), and faults fixed as found (V182-V184, V188, V189). Stuck events down about fivefold (V178). Hour-4 checkpoint: deaths missed |
| 3 (sessions 151-159, 1.83 h played) | 4-7 | Testvvi | 13.78-14.23 | 2,768 (3,973 in 151-155) | 2.7 | 56 (41 of the 103 in 156-159, wedged) | 0 guide steps/h | 0 | V188-V197 | 151-155 the best XP/h of the run with deaths at 2.4/h; 156-159 lost to the Sentinel Hill inn: a repair walk's hearth, the door missed (V198), the corner by the stove and the crate behind the inn |
| 4 (sessions 160-172, 3.04 h played) | 7-12 | Testvvi | 14.23-15.25 | 4,234 | 2.6 | 30.3 | - | 0 | V198-V210 | The run's best block: quest hand-ins at 9,000+ XP/h in 166 and 172; the mage's worst faults found on its visits (V204, V208) |
| 5 (sessions 173-188, 3.53 h played) | 12-15 | Testvvi | 15.25-15.87 | 2,365 | 2.8 | 22.9 | - | 0 | V211-V217 | Lost to three things: the Smuggler's facing loop (173-175), V212's crash (176-177), and Jangolode's Smugglers with a bags-full loop (184-187; V217). 181 and 183 made 4,713 and 5,786 |

Trials
------

| Change | Deployed | Predicted effect (metric) | Proof it fired (log line) | Judged | Kept/reverted |
|---|---|---|---|---|---|
| V178 walks no longer go by learned passages | next boundary after session 141 | stuck events a walk (7.0 over 215 walks, sessions 88-140) and walks not arrived (15 of 215) fall or hold; guards XP/h and deaths | `route memory: N blocked spots kept clear of; 151 old passages not taken` | | |
| V179 item droppers within two levels of the quest; 12-20 regenerated | with V178 | deaths/h on 12-20 objectives fall; Patrolling Westfall done with no death | the hunt's target is Riverpaw Mongrel (`hunt.request` wanted_name_id) | | |
| V180 a moved plate is found again when it is the only one of its colour | with V178 | `target-no-proposal` frames (7 in the last 80 runs) and "no eligible target geometry" failures fall | a `target.proposal` after a selection whose plate moved | | |
| V183 a walk from inside a death's reach keeps the distance it has | session 144 | deaths within 60 s of getting up (4 of 7 in sessions 134-142) fall | a walk after getting up that bends round the body (`round where the character died`) | | |
| V184 the strip is read where it was last read whole (`var/radio-grid.json`) | session 148 | no session lost to an unreadable strip | `var/radio-grid.json` present; sessions start reading | | |
| V185 a failed repair never stops the run | session 149 | no exit=1 on VENDOR_REPAIR | a repair abort followed by play | | |
| V186 a purchase the purse could not pay waits for the purse it needed | session 149 (paladin); the mage's next check | no walk to a merchant and back after each kill | `too_poor` once, not every kill | | |
| V187 a selected unit's body point is aimed at again once the view settles | session 150 | trainer and quest clicks under a zone title succeed | `interact.settle` events | | |
| V188 a runner is stunned where it stands | session 151 | fights with 3+ attackers and deaths at camps fall | `fight.runner` events (2 in session 151) | | |
| V189 resurrection sickness is waited out | session 151 | no death within 3 min of a Spirit Healer revive | `resurrection sickness: waiting` | | |
| V190 every reader shares the strip's last good grid | session 152 | no `target observation: checksum` | none in the log | | |
| V191 blind melee is decided on a fresh reading | session 153 | fewer fights ending `not_visible ... time to face` (6 in session 151) | `engage.blind_melee` events | | |
| V192 a caster's kill at range is looted where it fell | the mage's second check | caster loot misses fall | a loot walk after a kill `ended_far` | | |
| V193 no rib beside units above the band's top | guides regenerated, session 154 | deaths at ribs fall | the rib chosen on the level 2 mage's failover | | |
| V194 a service blocked for the step is not armed again | session 157 | no "durability is low" loop | one VENDOR_REPAIR a step after it is blocked | | |
| V195 no restock walk without one purchase in the purse | the mage's third check | no merchant walk too poor to buy | `service.supplies` only with the price in the purse | | |
| V196 a too-poor repair waits for the purse to grow | the mage's third check | no repair walk after each copper | `too_poor` once a purse | | |
| V197 killed by a unit 3+ levels up: the Spirit Healer | session 158 | no second death getting up at the body | `up at the Spirit Healer` after such a death (mage, 26 Sep 06:05 and 06:16) | | |
| V198 a planned leg is walked along its line | session 160 | stuck events a walk fall (door sim 108 to 6 in 189); open-ground time holds | Sentinel Hill: hearth to the door without a stuck event | | |
| V199 a merchant's failures cost it walk | the mage's fourth visit, session 2 | no restock past a near merchant | the chosen merchant's walk against the nearest's | | |
| V200 NPC body probes to 290 px | with V199 | "world focus without a mouseover unit" interact failures fall | a `target.proposal` 160+ px under the bar that opens a window | | |
| V201 a repairer is chosen as a merchant is | the mage's fourth visit, session 3 | repairs at the nearest repairer; no repair walk over 1,000 yards | `trying the next repairer`, or a repair at a smith the guide does not name | | |
| V202 a plate just clicked is looked for toward where it was | session 160 | "no plate proved" pulls fall (30 of 33 looks in the mage's session of 06:24) | a `face.search` toward the hint's side out of combat | | |
| V203 a failed repairer is followed only by its neighbours | session 162 | no repair walk into another town | `trying the next repairer` only in one town | | |
| V204 a caster casts at a Tab pick it cannot see | the mage's fifth visit | the mage's fights given up with nothing pressed fall (5 of 9 on 06:24) | `engage.blind_cast` | | |
| V205 no restock walk over 400 yards | after the mage's fifth visit | no restock walk through another level band | `BUY_AMMO_REAGENT_FOOD: aborted ... too_far` | | |
| V206 the purse's lessons kept between sessions | with V205 | no session-start repair walk the purse cannot pay | `character-KEY.purse.json` written; no `too_poor` twice at one purse | | |
| V207 blind melee steps in on "too far away" | session 164 | no fight lost standing out of reach of a flyer (session 163's death) | an `approach.request` with mode `blind_melee` | | |
| V208 a cast "not in front" with the plate centred turns round | after the mage's sixth visit | no caster death at full mana with the attacker behind | `engage.realign` then `engage.turn_round` in a caster's fight | | |
| V209 self-defence with no name wanted takes only attackers | session 169 | no death fighting a bystander while attacked from behind | `selection.expected` with `attackers_only` true and no second pass in self-defence | | |
| V210 an attacker found nowhere leaves Tab's pick as the fight | session 171 | no fight loop turning round with nothing taken (33 turns in four minutes, session 169) | `acquire.anything` | | |
| V211 a plate turned under the radio strip is on the centre line | session 175 | fewer "time to face ran out" fights at units up a slope | `target.face` faced "under the radio strip" | | |
| V212 fights that never engage pause combat and the walk goes on | session 176 (policy, supervisor), 178 (body) | no half hour pinned by an unreachable attacker (sessions 173-177) | `fights that never engaged: combat paused, walking on`, then a walk | | |
| V213 a ghost that does not get up short of the body goes closer | session 179 | no corpse run repeated "still a ghost" | session 179: still a ghost at 25 yards, then alive at 12.5 (11:57) | | |
| V214 a step's entry level outlives the session | session 181 | a failover grind ends a level above where it began, whatever the sessions between | the playhead's `entry_level` (15 on Poor Old Blanchy, session 182); session 181 left the rib, 4,713 XP/h, no death | | |
| V215 a restock keeps what the trainer is owed | session 183 (paladin); the mage from 14:15 | the mage trains at each level it can pay for; no water bought while Conjure Water is untrained and in reach | `TRAIN_CLASS: done` for the mage before its next `BUY_AMMO_REAGENT_FOOD` | | |
| V216 a person is three inputs inside three seconds | session 184 | no false pause; a person still pauses the run | strays logged "1 of 3" and never confirmed (session 184: one) | | |
| V217 full bags a merchant cannot help do not stop a hunt | session 188 | no run of `GRIND_UNTIL: bags_full` (583 in session 186, all of 187) | session 188: `no_junk`, then the hunt walked and fought | | |
| V218 the walk out of reach does not stop for a meal | session 190 | no "not fit to travel: in combat" during a pause | a leg started in combat under the pause at low health | | |
| V219 an accept needing a quest the route will not finish is passed by | session 191 | no accept tried whose prerequisite is lost (Milly Osworth twice and Milly's Harvest once in sessions 189-190, each with a rib) | the playhead moving from a Milly accept to the step past its chain with no ACCEPT_QUEST | | |
| V220 a rib waiting to retry a step that can no longer happen ends | session 192 | no rib for a lost chain | session 192: from the kobold rib straight to Bounty on Garrick Padfoot | | |
| V221 a lone spawn not seen from a stand-off is looked for from its spot | session 194 | no session spent at a named mob's stand-off | a second `hunt.approach` to a lone spawn without the stand-off | | |
| V222-V229 the consolidation (§11) | session 196 | no behaviour change in play; the suite and the offline check green | session 196: the usual proof lines, no Traceback, 9 kills; `keep.sh status` and `session_check` read the new logs | | |
| V230 a walk wedged indoors backs out the way it came in | session 198 | no minutes-long wedge in a building the character walked into | `wedged indoors: backed out the way it came in` | | |
| V215 in the mage's hands | session 189 | the mage trains when it can pay | session 189: `Khelden Bremen: done, 1 bought for 95 copper`, Conjure Water on slot 5; the purse had reached 153 | | |

Issues
------

| Seen | Issue | Evidence | State |
|---|---|---|---|
| 25 Sep 12:00 | The adaptive canary-student runtime test failed once in 2 full runs (planning agent) | Not seen in 3 focused runs or 4 full runs since | Watching; remove it with the retired paths (§11) |
| 26 Sep 04:35 | `test_grade_run.py::test_new_grades_are_not_hidden_by_an_older_parquet_copy` failed once in a full run under load; 5 of 5 alone passed | `dataset.py` compares `st_mtime_ns` of the parquet copy and the JSONL: two writes inside one timestamp tie | Flaky; goes with the decision learner (§11 step 3) |
| 26 Sep 05:12 | `test_play_learning_integration.py::test_actual_controller_corpus_trains_shadows_hands_over_and_rolls_back` failed once in a full run under load; 2 of 2 alone passed | The motor learner's canary and handover, timing-sensitive | Flaky; goes with the motor handover and canary (§11 step 4) |
| 26 Sep 15:35 | The mage's first 66 minutes on the loop (sessions 189-193): 43% on the level 1-3 kobold rib waiting to retry the Milly chain, 28% at Garrick Padfoot's stand-off | The chain needs Wolves Across the Border, whose objective was passed over on a visit; Garrick stood by the shack with his plate at the screen's edge | V219, V220, V221 |
| 26 Sep 16:50 | The mage at Fargodeep (sessions 195-197, level 6.3-6.6): six deaths in 45 minutes, gear broken, too poor to repair; 7 of 37 fights had two or more kobolds, and those were the deaths | Fireball rank 1 only: a Kobold Tunneler (102-120 hp) takes 6-7 casts, two of its mana bar's three. The quests there pay 125 and 175 copper, which buy Fire Blast and Fireball 2; `TRAIN_CLASS` failed once at Zaldimar Wefhellt, the walk arriving under him on the inn's ground floor | Watching; the trainer's floor is the multi-floor problem again |
| 26 Sep 13:20 | `test_learning_cycle.py::test_worker_grades_trains_and_restarts_without_duplicate_candidates` failed once in a full run while a second suite ran alongside (the consolidation branch's); alone, 23 of 23 passed | The worker's training-interval gate (`reason='training interval/new-row gate pending'`) is timing-sensitive | Flaky; consolidation step 3 removes the decision learner |
| 26 Sep 12:30 | The operator watch counts inputs 400-625 ms after the bot's own key-ups (A, Tab, Shift), with nobody at the desk: session 174 reached "2 of 3" during the Smuggler's facing loop; three within 10 s pause the run for `quiet_s` | Every injection is stamped (`win32.send_inputs`), key-ups included, so these are other events, a Windows keyboard quirk by their timing. The 3-in-10 s rule has held | V216: three inside 3 s, not 10 |
| 26 Sep 12:15 | A failover grind's end moves with the session: `until_level` is the level at entry plus one, and entry is per process, so the paladin (15.3, failed over from The People's Militia's hand-in to `grind_westfall_12_14`) now wants 16 on the 12-14 rib, among Dust Devils (two deaths, sessions 178-179) | Candidates: keep `level_at_entry` in the playhead; take the rib for the character's level on failover | Open; the paladin stops at 14:15 |
| 26 Sep 09:55 | A hopeless fight is fought where it stands: a level 19 Dust Devil north of Sentinel Hill killed the 14.9 paladin twice in 90 s (session 168), 150 yards from the hill's guards | Candidate: at four levels down with the guards near, run to them (Divine Protection first). V209 now lets V197 see the killer's level | Open |
| 26 Sep 08:30 | A loot objective's hunt holds too few spawns for its kills: Goretusk Liver Pie (8 livers at 33%, about 24 kills) gets the 8 Goretusks within 150 yards of 99 in Westfall; session 164 made a liver in 12 minutes, 116 of 136 looks with no plate | Sessions 163-164 still made 5,000+ XP/h on what else was there. Sizing the cluster by kills (count / chance) moves the centre and renames the hunt to Young Goretusk (12-13, less XP): the draft (`/tmp/w48/v208-generator.patch`) is shelved. Better: keep the name, add that creature's spawns out to 300 yards | Open; before the mage reaches Westfall |
| 26 Sep 07:30 | `test_play_runtime.py::test_real_supervisor_reaches_teacher_escape_and_confirms_modal_closed[True]` failed once in a full run under load (two teacher prompts 24 s apart, one expected); 2 of 2 alone and the next full run passed | The teacher escape's timing under load | Flaky; goes with the tutor paths (§11) |
