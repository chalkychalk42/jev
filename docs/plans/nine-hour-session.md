Nine-hour session plan: speed, learning that pays for itself, and the Westfall frontier
=======================================================================================

Written 24 September 2026, after sessions 55-87 (the day's report is STATUS.md 10:30-16:15).
For the next uninterrupted nine-hour control session. Operator decisions are in section 4;
every item follows ROADMAP.md's design gate (shared mechanisms, no identity branches).

1. Where things stand (measured, not remembered)
------------------------------------------------

| Measure | Value | Source |
|---|---|---|
| Character | Testvvi, level 10 at 72%, 997 copper, home Goldshire | playhead, strip |
| Guide | step 77 of 147 (Red Linen Goods), 13 quests done; 16 objectives left in 1-12 | playhead |
| XP rate, teach mode | 2,909 XP/h at level 10; 2,590 at 9; 2,568 at 8 | ticks, 23-24 Sep |
| XP rate, no tutor | 2,760 XP/h at level 2 (teach: 1,733); 8,723 at 3 (teach: 1,581; 0.2 h sample) | ticks, 20-23 Sep, older code |
| Deaths | 0 in the last 10 sessions (84 kills); 7 in the 23 before | session logs |
| Loot rate | 84% of kills (58% before V121/V123) | session logs |
| Travel | 0 walk timeouts since the slope fix (V120) | session logs |
| Time use (12:00-14:40) | grind 38%, fights 22%, travel 18%, meals 7%, corpse runs 6%, bags 5% | ticks |
| Tutor | ~90 calls/h, median 7.6 s (claude-opus-4-7, medium effort, V51); 21% of play time | play-teacher |
| Motor learner | 1,804 records, ~20% qualified; students abstain on every held-out example | offline replay |
| Routine choices | 917 of 1,804 tutor actions; a student covers 12% of held-out ones at 100% precision | offline replay |
| New, untested live | 12-20 guide chain (V124), flights (V127) | - |

2. What the data says
---------------------

1. **The tutor is the largest cost on progress.** Every walk under a teaching episode waits
   for one decision before its first step, and every fight on the way means another.
   Tutor-free runs at level 2-3 earned 1.6-5.5 times the XP an hour, on older code. The
   ROADMAP's end goal asks learning to reduce teacher cost *without worsening progress*;
   today it costs progress and has not yet reduced anything.
2. **The student is data- and feature-starved, not wrong.** Where it answers it is right
   (100% on held-out routine choices), but it answers 12% of the time: every numeric
   feature (health, mana, bags, durability, GCD) must match within one radius, and
   categorical fields such as the 24 action-bar bits and `ui.error_id` split states that
   make the same decision. Only ~20% of recorded actions qualify, mostly because the
   teaching episode around them was cut short.
3. **Survival and looting are solved for now** (0 deaths, 84% loot) - but the next 16
   objectives include the day's known killers: murloc packs (Collecting Kelp, Bounty on
   Murlocs), Goldtooth with adds, Princess with her boars.
4. **The frontier arrives mid-session.** At 2,900 XP/h the character reaches level 12
   and the end of the 1-12 guide in roughly 4-5 hours, then switches to the 12-20 guide,
   walks to Westfall, and meets its first flight master and inn outside Elwynn. Those
   paths (V124, V126 at a new inn, V127) have unit tests only.
5. **One principle is violated:** the bot pulled focus back from the operator eight times
   before the last stop. It cannot tell a person's input from its own.

3. Goals for the nine hours
---------------------------

| Goal | Target | Measured by |
|---|---|---|
| G1 Never fight the operator | Human input pauses the bot within 2 s, every time | live synthetic-input test |
| G2 Faster levelling | ≥1.5x XP/h over teach mode in a same-level A/B (if approved) | `tools/session_report.py` |
| G3 Learning that reduces teacher cost | A student ≥50% held-out coverage at ≥90% precision for routine choices, then shadow agreement ≥90% live | offline replay, shadow logs |
| G4 Frontier verified | 1-12 finished, 12-20 guide active, Sentinel Hill flight node visited, hearth bound there, one flight flown | live logs + screenshots |
| G5 Survival holds | ≤1 death per 2 h; no death spiral; murloc and Goldtooth objectives done | session logs |
| G6 Zero unrecoverable stops | Loop runs 9 h with every stop self-recovered | session-loop.log |
| G7 Level | ≥12.5 (teach) / ≥13.5 (hybrid) by the end | strip |

4. Decisions needed from the operator (before the session)
----------------------------------------------------------

- **D1 Tutor strategy.** (a) A/B for the first ~3 h, then adopt the winner automatically
  by the rule in 6.3 - *recommended*; (b) keep full teach mode (the V51 status quo);
  (c) hybrid from the start (scripted routines drive; the tutor takes failures, novel
  states and a fixed sample of ordinary objectives); (d) scripted only.
- **D2 Tutor model/effort.** Keep claude-opus-4-7 at medium (V51), or add an arm with a
  faster setting (same model at low effort, or a newer model after a smoke test).
- **D3 Student handover.** May a capability that passes the held-out gates run as a
  bounded canary (adaptive mode: ~10% of its decisions, automatic rollback on any
  regression in deaths, stalls or XP)? Without this, G3 stops at shadow.
- **D4 New-character benchmark.** Spend ~2 h of the nine on a fresh level 1 character
  (which class) to measure time-to-level-5/10 on today's code and find new-character bugs?
  Default without an answer: no; all nine hours on Testvvi.

5. Schedule
-----------

Times are budgets, not promises; the loop plays throughout except where marked.

**Hour 0 - pre-flight (15 min, loop stopped)**
- Client running, strip schema 16, one loop, monitor armed, disk space, learner stores
  healthy (`latest-cycle.json` both), `pytest` green. Baseline snapshot: level, XP, money,
  playhead, and a first `session_report` row.

**Hours 0-1 - foundations (loop running in teach mode unless D1=c/d)**
- *W1 Focus arbitration (G1).* The HID stamps the moment of every injected input;
  a Windows probe reads `GetLastInputInfo`. Input newer than the bot's own by >300 ms is
  a person: release every key, never refocus, pause the session, and resume only after
  10 minutes with no human input. Tests: unit tests with a fake clock and idle source;
  live test by injecting a mouse move from a separate process while WoW is foreground -
  the bot must pause and log it. Rollback: none needed (pure safety).
- *W2 Metrics (all goals).* `tools/session_report.py`: one row per session in
  `captures/metrics.csv` - XP points/h (per-level table, not levels/h), kills, deaths,
  loot rate, stuck events, walk timeouts, watchdog failovers, tutor calls/h and median
  latency, qualified motor examples, time by skill, money delta, mode arm.
  `--compare` prints arm means with confidence intervals. Tested against sessions 55-87.
- *W3 Loop arms.* `start_teaching.py --mode off` (keeping `--learn --screenshots`, so the
  outcome learner still records), and the loop reads `/tmp/session_mode` each session so
  arms switch without restarts. Monitor patterns gain BIND/DISCOVER/flight/mode lines.

**Hours 1-4 - the tutor A/B (G2) and the learning work beside it (G3)**
- *W4 A/B protocol (6.3)*, arms per D1/D2, alternating every two sessions (30 min).
- *W5 Hybrid dispatch (if D1 allows).* In `PlayingBody`: an objective runs its scripted
  routine first; the tutor is asked when that routine fails, stalls (no progress in its
  window) or meets a state the scripted floor has no rule for, plus a fixed sample (1 in
  4) of ordinary objectives so broad data keeps coming. Records and episodes unchanged.
  Tests: dispatch unit tests (fail -> tutor; success -> no tutor; sample rate); replay of
  a recorded run's decisions; live A/B.
- *W6 Student features (offline, beside the live runs).* Evaluate on a copy of the motor
  store, held-out by run: (i) per-decision-class feature relevance - a field is used only
  where it separates labels in training runs; (ii) coarse bins for vitals and bags on
  state decisions; (iii) drop action-bar bits and error ids outside combat decisions;
  (iv) count an episode's successful actions when the episode was cut short by a fight
  that V118 credits. Acceptance: ≥50% coverage at ≥90% precision on routine choices,
  with no loss of precision on the existing models. Then ship to shadow; canary only per D3.

**Hours 4-7 - the frontier (G4) and survival (G5)**
- Level 11 and 12 training; 1-12 finish; the chain to 12-20; the walk to Westfall.
- Live checklist, each item a log line and a screenshot: `guide ... finished; continuing
  with ally_human_12_20.json`; `DISCOVER_FLIGHT: done Thor's node remembered`;
  `BIND_HEARTH: done Innkeeper Heather's inn is home` ("Sentinel Hill is now your home");
  `flight from ... : landed`. Anything that fails gets its fix and a rerun the same hour.
- Deaths: every death traced the same session (who, how many, what was pressed); fixes
  only as shared mechanisms (e.g. an add check before engaging, save-then-heal timing).

**Hours 7-9 - consolidate**
- Adopt the A/B winner (6.3) for the rest of the run; D4 benchmark if chosen.
- Final report: metrics table per arm, goals met or not with evidence, DECISIONS rows,
  STATUS entry, everything pushed.

6. Testing methodology
----------------------

**6.1 Offline gates (every change)**
- Full `pytest` with its exit code checked before every commit (the pipe that hid a
  failure on 24 September is not used). New behaviour ships with tests that fail on the
  old code. Addon changes run the Lua harness (`tests/lua/paint_once.lua`) and the
  paint-only test list (`TakeTaxiNode` and friends stay forbidden).
- Learning changes are judged on held-out *runs* from a copy of the live store, never
  the store itself; the report states coverage, precision, and what was excluded.
- Guide changes: `compile_route` counts and a spine walk-distance check; the 1-12 guide
  file is never regenerated (its bytes are the learner's corpus fingerprint).

**6.2 Live verification (every change)**
- Each change names in advance the log line or metric that proves it and the screenshot
  that shows it. Verified within two sessions or given a targeted test; otherwise it is
  marked unverified in DECISIONS.
- Sessions run from the working tree: a change applies at the next session, so edits land
  between sessions or with a deliberate STOP.

**6.3 The A/B protocol**
- Arms alternate every two sessions (30 min) in the same area and level band; at least
  four blocks per arm. Primary metric: XP points per hour. Secondary: deaths/h,
  stuck/h, loot rate, qualified motor examples/h, tutor calls/h.
- Adoption rule (unless the operator sets another): hybrid replaces teach if its XP/h is
  ≥1.4x teach's, deaths/h are not worse, and it keeps ≥40% of teach's qualified
  examples/h. Otherwise teach stays and the finding is recorded.

**6.4 Safety and stop rules**
- W1 pauses on any human input. Three deaths within 20 minutes, three quick session
  exits, or one repeated failure class across sessions: stop the loop, diagnose, fix,
  resume. A disconnected or hung client: the restart procedure in memory
  (graceful close, wait for Char.log's Logout, PowerShell launch, `tools/login.py`).
- A stopped loop leaves the character standing in the world: restart promptly or park
  it out of reach first.

**6.5 Cadence**
- Debrief every 90 minutes: metrics table, a STATUS entry, commit and push.
- Every design decision gets a DECISIONS row with its measured reason; every live
  verification updates that row's status.

7. Risks
--------

| Risk | Mitigation |
|---|---|
| Hybrid starves learning | 1-in-4 sampling; qualified examples/h is an adoption criterion |
| A/B confounded by level/zone drift | Short alternating blocks; XP points, not levels |
| Student canary misbehaves | D3 gates; 10% fraction; automatic rollback on deaths/stalls/XP |
| First flight or new inn fails far from help | Failure walks on (V127); bind and discover are one try a step |
| Murloc packs | Traced per death; shared engage/heal fixes; the rib ladder moves on meanwhile |
| Client instability after addon changes | No schema change planned; restart procedure rehearsed today |

8. Backlog if time remains
--------------------------
- Trainer purchase priority (needs the trainer list painted: a schema change).
- 12-20 guide order: two Westfall-Redridge crossings instead of five.
- Acquire as plate choice (which plate, not which pixel) for the motor learner.
- Bags: buy 6-slot pouches once training for the level is paid for.
