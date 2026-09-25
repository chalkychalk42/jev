Forty-eight-hour ledger
======================

The running state of `docs/plans/forty-eight-hour-session.md`, newest last in each section.
Read "Now" after every compaction. Times are BST (the Windows clock runs about 25 s ahead).

Now
---

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
  - **B2, two sessions on Testvvi.** Start the loop. Once the second session starts, touch
    `var/loop/stop`.
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
    - `tools/keep.sh install-timer`.
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

Trials
------

| Change | Deployed | Predicted effect (metric) | Proof it fired (log line) | Judged | Kept/reverted |
|---|---|---|---|---|---|

Issues
------

| Seen | Issue | Evidence | State |
|---|---|---|---|
| 25 Sep 12:00 | The adaptive canary-student runtime test failed once in 2 full runs (planning agent) | Not seen in 3 focused runs or 4 full runs since | Watching; remove it with the retired paths (§11) |
