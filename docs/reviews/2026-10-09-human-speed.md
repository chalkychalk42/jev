# Leveling speed and live transfer — 9 October 2026

The largest opportunity is productive time: choosing fights before entering their aggro,
finishing several quests on each trip, and preventing repeated actions that accomplish nothing.
Faster key presses alone cannot close the gap. Three shared-code fixes are built in
`bet-speed-transfer`: rolling quest confirmation, planned flight selection, and cross-zone
food purchases. Class-kit and safe-pull/quest-sweep candidates remain separate experiments.

## What the fresh measurements say

Window: **9 October, 18:00–22:00 BST**. A deterministic 400-run selection contributed
335 runs, 171 characters and **76.9 played hours**. Different classes, levels and experiment
arms are mixed here; this is a bottleneck survey, not a causal comparison or a 1–20 timing.
Most sampled time was at levels 11–20. Source: JevHive `hive.timebudget`, one worker,
300-second timeout; summaries streamed from existing records.

| Measure | Observed |
|---|---:|
| XP per played hour | 2,155 |
| Kills per hour | 26.6 |
| Deaths per hour | 3.61 |
| Quests handed in per hour | 0.30 |
| Quest share of XP | 9.2% |
| Time on grind steps | 69.4% |
| Kills yielding no XP | 13% (10% grey, 2.5% already tapped, 0.5% otherwise unpaid) |
| Deaths beginning in defensive fights | 88% |

| Exclusive time category | Share |
|---|---:|
| Fighting | 19.7% |
| Approaching targets/stations | 22.2% |
| Route travel | 4.9% |
| Searching | 6.3% |
| Resting | 11.7% |
| Dead/ghost | 7.9% |
| Idle | 17.9% |
| Services | 6.1% |
| Loot | 2.4% |
| Session overhead | 0.8% |

Idle includes **10.9% of all sampled time waiting out resurrection sickness**. Preventing
deaths therefore recovers more than corpse-running time. Of approach operation time, 45%
ended with a combat interruption; those interrupted approaches averaged 32.6 seconds and
197 yards. This is where combat selection and transport meet.

Median successful fights lasted roughly 16–24 seconds by class. Kill cycles averaged
58–92 seconds by class. The full cycle includes travel, search, recovery and loot, and the
cycle-only rate excludes other time: it must not be reported as overall XP/hour.

As arithmetic illustrations, with everything else fixed: reducing fight time by 25% saves
about 4.9% of total time, or about **5.2% throughput**. Halving travel plus approaches would
save 13.6%, or about **15.7% throughput**. These are sensitivity calculations, not measured
gains, and assume the same XP and no shifted costs. They must not be added to overlapping
death-prevention estimates.

## Changes built from this review

### Quest evidence survives retries

The shared body reset the quest-log assembler before every accept/turn-in. The server skill
could answer success from its complete raw log while the tracker still saw an incomplete
rolling log. Repeated interactions discarded each partial assembly. V455's success fallback
did not repair this observation contract; the parity shard had disabled quest parity again.

The body now retains the assembly, lets the painted count/hash invalidate it, and avoids
reopening an accept already confirmed in the assembled log. The button skill checks once
between progress/reward pages, then waits for delayed confirmation after closing. A changed
membership cannot revive an old whole log on the next counter change; count changes also
invalidate under a hash collision, and unidentified slots cannot complete a log.

The companion hive adapter confirms from the rolling assembly when quest parity is enabled.
Server acceptance alone is insufficient. No new addon schema or privileged live sensing is
required. Tests cover a real shared body with a slow ten-slot reader, stale post-click frames,
membership changes and partial turn-ins.

This candidate declares quest and flight parity as mandatory. The hive combines those with
its optional parity settings when the candidate farm starts, even if the general switch is
off. Older control code keeps its existing settings. The speed comparison therefore judges
the complete candidate under its stricter observation contract; it cannot attribute a gain
or loss to one individual fix.

### Food purchases use the zone the character is actually in

Recorded example: hive-889, run `20261009T201539-96481b`, level 11 draenei warrior. It stood in
Bloodmyst using an Azuremyst coordinate frame. The policy found Little Azimi nearby, but the
executor's food catalog contained only Azuremyst food IDs. The first two recorded attempts
chose Caregiver Topher Loaal, a 954-yard walk; the next **1,747** reported no supplier.
Another sampled character repeated the same failure **1,738** times in one session.

Policy and execution now use the same union of guide-frame and observed-zone shops. The
cache includes both complete map boxes; a zone on another continent is excluded. A saved
state regression now selects Little Azimi without performing any input. This restores a
usable purchase, instead of merely slowing the loop with a longer backoff.

### Flights compete with planned walks

Routine flights previously compared straight-line distances. The unreachable-walk fallback
checked actual ground legs but stopped at the first reachable landing; ordinary flight
failures were excluded from its cooldown. The hive also limited flights to night elves and
usually supplied server-known nodes, while the live body used remembered nodes.

The candidate compares a complete planned walk with up to three origins and three landings
per origin. Both ground legs, ride estimate and interaction overhead count. A reachable walk
must be beaten by at least 25%; an unreachable walk still needs reachable access and exit
legs. Every candidate landing is compared and every failed pair is remembered for retry
cooldown. Short local approaches request no flight plans; the planner has at most 13 walk
queries per eligible trip.

The companion hive adapter uses the candidate's shared planner and remembered/starting
nodes for every race. The observed taxi map, affordability and actual landing still decide
execution. Airborne duration remains an estimate. Boats, zeppelins, tram routes and unseen
lift schedules are not implemented by this change.

## Combat and questing overhaul sequence

| Priority | Work and present state | Evidence to require |
|---|---|---|
| 1 | Class kit, passives, weapons and off-hand handling: corrected current-base candidate already playing | Per-class deaths, successful damage, weapon use, interrupted casts; overall speed guard |
| 2 | Safe approach/pull planning and grouped quest objectives: rebuilt `bet-ownpull-current` next | Fewer fights joined during walks, fewer multi-attacker deaths, more hand-ins; total journey time including interruptions |
| 3 | The three shared fixes above: `bet-speed-transfer` | Repeated accepts/no-supplier loops disappear; route completion and total XP/hour improve without death/idle regressions |
| 4 | Quest objective expansion from the interrupted `quests3` work | Counter identity, cancellation/death tests, item credit, purchases and exploration verified before new routes are activated |
| 5 | Recovery and station choice tuned after safer pulls | Rest seconds per paid kill, dry stations, corpse versus healer costs by level; shared learned inputs |

The existing **1.9-second cast guard is retained**. Its regression records show live-client
casts refused when shortened. Spell response time should be reduced using measured action
acknowledgements and rejection rates, not by removing a guard which compensates for observed
radio/client timing. The class-kit and safe-pull trials address more valuable combat losses.

The quest-expansion artifacts compile **3,929/5,017 route-quest slots**, compared with
3,370/5,017 before, across 52 routes. These are repeated route placements, not unique quests
or proven successful completions. That work is preserved, not installed. Review found that
mixed equal-count objective families can be ambiguous, item-use credit paths need stronger
cancellation tests, and the server exploration bridge change still needs its own build and
rollout. Route files must be versioned with a trial; replacing shared routes during an A/B
would change its control too.

## What “human speed” will mean

There is no matched human baseline in the inspected records. Earlier estimates of 10–14
hours to level 20 are not sufficient evidence for a ratio. Compare manual and bot sessions
on the same realm, class, level band, equipment, route and rested-XP conditions. Record:

1. Total played time and XP, including deaths, services and transitions.
2. Time from one paid kill to the next, alongside the fight itself.
3. Quest accepts/hand-ins, abandoned objectives and XP per trip.
4. Door-to-door journeys, including discovery, walking to transport, waiting and landing.
5. No-progress repeated actions, empty searches, rejected casts and recovery time.

First engineering targets (not measured outcomes): hand-ins above 1/hour at 11+, halve
combat-interrupted approach time and sickness time, and kill cycles below 50 seconds without
increasing deaths. A later target is several complementary quests completed per trip. Test
these by class and band; the overall average can conceal a broken hunter or draenei route.

## Live-transfer contract and remaining gaps

| Layer | Shared/validated here | Still requires actual-client evidence |
|---|---|---|
| Decisions and quest tracking | Same body, route tracker, assembler and objective logic | Addon reads under real capture latency and UI transitions |
| Combat | Same rotation, cooldown safeguards, resource and target rules | Visual acquisition, facing, rejected inputs and latency tails |
| Travel selection | Same planner, node memory, reachable ground legs and failure cooldown | HID follower, doorway/corner movement, real flight-map interaction |
| Recovery and learning | Shared policy and explicit recorded outcomes | Hive crowd/respawn rates and exploration settings differ from live |
| Actuation | Hive adapters exercise the same skill contracts | Hive still has server movement/coordinates, ID-based actions and padded timing |

“All parity parts” is useful but **does not make the hive identical to the real client**.
Before deployment, replay the exact candidate with rolling quest and flight parity enabled;
then require a supervised client slice covering multiple held quests, cross-zone provisioning,
a combat interruption, a known flight and a refused flight. Compare both outcomes and time.
No Windows client, live session loop or live realm was started for this review.

## Experiment integrity and rollout

The separate canary ancestry audit found six rejected trials missing improvements in their
controls. Their rejections do not isolate the features. Current candidates must contain the
actual control and pass their own suite after rebuilding. Keep the active class-kit control
frozen; allow it to finish before the next candidate. Require matched class/level comparisons
and guards for deaths, idle time and stuck sessions.

Keep holiday coordinator automation stopped. Use the existing hive canary and deployment
gates. Live deployment remains a later gated action; the new code is a candidate, and no
human-speed gain is claimed before its gameplay measurements.

Validation: the first full shared-code suite passed 3,268 tests with four skips; the hive
adapter suite passed 336 with 59 skips against both the control and candidate. Four new
regressions were separately run against old production code and all failed as intended.
Final suites are rerun after adding the mandatory-parity contract; their reports are kept
with the candidate and the companion hive change.
