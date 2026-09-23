# Jev — architecture

**Current contract, 22 September 2026:** [the visual teaching loop](docs/TEACHING_LOOP.md)
supersedes this document's original rare-teacher/whole-skill-only restriction. In teaching
mode Jev chooses bounded motor actions from screenshots and observed outcomes, using the
guide and existing routines as support. Evaluated local capabilities progressively take
over through outcome-qualified training, independent evaluation, canaries and rollback.
The older scripted mode and its strategic learner remain available. The historical
sections below describe that earlier mode; zero teacher dependency is a goal earned per
capability, not an invariant imposed on an untrained motor controller.

**Engagement and tutor contract, 23 September 2026 (DECISIONS V40-V44):** a unit is faced
by turning until its own hover-proved nameplate is on the screen's centre line (a
right-click never turns the character); melee auto-attack is pressed only when the radio
observes it off; kills are proved by zero health or experience, and a corpse the client
deselected is found by a dead hover of the killed unit's name. The tutor replies with one
action name from a state-built menu; a tutor that cannot answer or stalls hands its
objective to the scripted routine, which is the zero-teacher floor again.

`docs/PLAN.md` is the vision. This file is the buildable form of it, and overrides the
plan wherever the two disagree. Every deviation is recorded with its reason so nobody
re-derives it. Decisions live in `DECISIONS.md`.

---

## 0. The shape of the thing

Five clock domains. Nothing above System 1 is allowed on the GCD.

| Layer | Clock | Cost | Role |
|---|---|---|---|
| **System 1** | 33–50 ms | free | move, face, swing, loot, eat, hard preempts |
| **Tracker** | 250 ms | free | step predicates, off-route, timeouts, `on_fail` edges |
| **Coach** | 0.2–2 Hz | free | arm a skill, move the playhead — *the distilled policy* |
| **Teacher** | queued, rare | expensive | resolve ambiguity, write skills, patch the graph |
| **Learner** | nightly / idle | free | grade, retrain, promote |

The teacher is **Claude over the subscription** (`claude -p`), with GLM as a cheaper bulk
rung. Neither is ever on the hot path.

### The invariant that makes this survive a subscription

> **The system makes forward progress with zero teacher calls.**

The scripted GOAP priority (PLAN §8.2) is the day-0 coach: always valid, always free. The
teacher is an *improvement engine*, never a dependency. This is a testable invariant and
it gets a test the moment the scripted coach lands; until then it is an unenforced claim.

This inverts the plan's ordering. PLAN Gate C treats weaning as a later milestone; on a
subscription it is the **critical path from day one**. We do not run expensive and get
cheap — we run cheap by default and spend the teacher deliberately.

---

## 1. Intelligence is a measured budget

The coach picks from seven intents:

```
advance | skip | rejoin | grind_rib | service | escalate | wait
```

Most are already computed elsewhere. "Is the step done" is a predicate. "Are bags full" is
a number. "Am I off route" is a distance. The coach only earns its cost on genuine
ambiguity:

- the objective counter is not ticking, but position says we are in the right place
- died twice here — too hard, or variance?
- gold is 40 and the mount costs 90 — grind now, or push three levels first?
- the route says north and there is a level 30 elite standing on it

So the headline number is not tokens/hour:

> **`unresolved/h` — decisions per hour that no rule could settle.**

At 1% of ticks the project is cheap. At 30% the problem is *perception*, not intelligence,
and a bigger model will not fix it. First counter on the eval board, and the one that must
fall. PLAN §19 names "bigger model on the mouse" as a risk that looks like intelligence;
`unresolved/h` is how you catch yourself doing it.

---

## 2. Learn from outcomes, not from authorship

Cloning a teacher caps the student at the teacher's error rate. We have ground truth — the
step advanced or it did not — so labels come from **what worked**, not who said it.

```
ambiguous state S  →  choice C  →  [60 s]  →  grade(step advanced? xp? died? stuck?)
                                                 └── train only on C that worked
```

1. **A wrong teacher call costs nothing.** It never becomes training data.
2. **The student can exceed the teacher.** Where the teacher was uncertain, different
   clients try different choices on the same situation and reward picks the winner — a
   contextual bandit over the choice set. PLAN §12.1 lists this as optional; it is not.
3. **Grading is asynchronous** — decisions are written immediately, graded later by a join.

### `situation_key` — the join key, and why it is load-bearing

A canonical bucket for "this circumstance": `(graph_id, step_id, coarse-binned features)`.
It is in the schema from the first commit because it is impossible to retrofit, and it
carries four jobs at once:

| Job | How it uses the key |
|---|---|
| Teacher dedup | three clients stuck on one step is **one question**, not three |
| Counterfactual bandit | group differing choices on one situation, compare outcomes |
| Agreement metric | policy prediction vs teacher choice, same bucket |
| Answer cache | a recent answer for this bucket is reusable without asking |

Binning is deliberately coarse: too fine and nothing matches, too coarse and distinct
situations collide. Bin widths live in `jev/coach/situation.py`, versioned separately —
changing them invalidates the cache, not the corpus.

---

## 3. The teacher writes artifacts, not answers

A decision helps one client once. A skill or an `on_fail` edge helps forever. On a
rate-limited teacher that difference compounds, so the escalation contract inverts
PLAN §9.2:

```
preferred:  skill draft  |  graph patch  |  on_fail edge  |  combat profile change
fallback:   an immediate action, for this client, this once
```

Skill promotion is measured, never asserted (PLAN §10): `proposed` → 2 clients × 3 runs →
`stable`; success rate < 0.4 over 20 → `retired`. Retired skills are not retrievable
without the teacher in the loop.

### Calling Claude on a subscription

`claude -p --output-format json`, one **single-worker** queue for the whole farm:

- bounded depth; overflow drops rather than queueing forever
- **deduplicated by `situation_key`** before dispatch
- timeout and retry, with abstention and transport failure told apart from a decision (§6)
- every request and response persisted to the decision stream, always

Latency is seconds and the rate limit is shared across all clients. Any design needing the
teacher to be fast or frequent is wrong by construction.

---

## 4. The episode store — three streams, joined later

Written from the first run, or distillation is impossible later.

| Stream | Rate | Contents |
|---|---|---|
| **ticks** | 2 Hz + events | `state_v1`, armed skill, `armed_by`, keys pressed, **shadow prediction**, `situation_key` |
| **decisions** | per coach/teacher call | request, response, verifier verdict, latency, model, tokens, `situation_key`, the tick it applies to |
| **grades** | written later | joins a decision to its outcome at +60 s |

Four rules, each unrecoverable if it lands late:

1. **`armed_by` on every tick** ∈ `{s1_preempt, tracker, policy, teacher, human}`. Without
   it you cannot tell whose decision you are cloning, and the corpus is dead weight.
2. **The policy shadow-predicts on every tick**, including ticks it is not driving. That
   is the agreement metric; Gate C's 90% is only measurable if measured throughout.
3. **Outcome attribution at +60 s**, by join, not inline.
4. **The policy's own graded-good trajectories feed back into training.** A policy trained
   only on teacher-driven states meets different states once it drives — distribution
   shift. This is the DAgger correction, and it is free if the store is right.

Promotion is **per bracket, never global**: a band freezes when agreement ≥ 90% and
deaths/h holds flat, and the teacher heartbeat drops 10× for that band alone.

---

## 5. The radio labels the vision heads

Every tick where `addon_ok`, log `(vision_estimate, radio_truth)` for every field both can
produce. That single habit yields:

- a live calibration signal, continuously
- a free regression test on every reader
- a **measured** fusion confidence instead of a guessed constant
- a known error distribution for addon-off mode

**Addon-off therefore stops being a failover we hope works and becomes one we have been
validating the entire time the addon was on.** It is also the better demo (PLAN §17.3).

Fusion (PLAN §5.3): numbers from the radio, windows from vision. An xy jump across the map
in one tick is a loading screen or a bad decode — freeze S1 movement 2 s rather than
believe it.

---

## 6. Invariants

Not style. Each of these is a class of silent failure that costs a run, not a test.

> **Absence of an answer is not an answer.**
> Transport failure, timeout, abstention and silence are four different things, and none
> of them is a decision. A model that returned nothing has not told you to stop. A `stop`
> with an empty history has seen nothing to stop for — that is abstention; retry it.

> **Never send a toggle without confirming the state it toggles from, on the frame in
> hand.** Selectors are asynchronous, so the frame an answer was computed from is already
> several ticks old. Pressing Escape on a stale reading *opens* the menu it meant to close,
> and the verification then fails on a menu the operation itself created. Read, then act,
> on the same frame.

> **Unknown is not a negative fact.** `None` means unobserved. Rendering it as `False`
> invents an observation nobody made.

> **A capability nothing calls does not exist.** A reader that is correct, tested, and has
> no production caller is not a feature. Wire it or do not claim it.

> **Generate both sides from one source.** Anything two components must agree on — field
> layouts, zone tables, enum values — is generated from a single definition. Agreement by
> convention decays silently; agreement by construction cannot.

> **Fix the failure class in its shared owner.** Every correction must provide a reusable
> mechanism for all affected callers. A quest, NPC or captured scene may reproduce and
> verify a failure; its identity must not become an executable exception that hides it.

Task-specific facts belong in validated, generated data: targets, requirements, routes,
prices and class profiles. Moving an ad hoc workaround into configuration does not make
it general. Separate behaviors when their observation/action contracts actually differ,
such as a living-unit interaction and corpse looting, while sharing their common
mechanisms. Keep one maintained implementation per responsibility and remove superseded
paths when replacing it. Verify representative successes, failures and affected callers.
Unknown cases remain explicit; a global design is not a claim that every case works.
Use the smallest shared design supported by evidence, without speculative frameworks.

> **Angles:** radians, 0 = +X increasing toward +Y. WoW world axes are +X north, +Y west,
> so that reads as 0 = north, increasing north → west → south → east. Confirmed against
> this server's own source, `mangos-tbc/src/game/Entities/Object.cpp`: `GetNearPoint2dAt`
> advances `x += d*cos(a)`, `y += d*sin(a)`, and `GetAngle` is `atan2(dy, dx)` with no
> negation. **Do not negate dy.**

---

## 7. JevRadio — pixel telemetry

- **One source of truth.** `jev/perceive/fields.py` defines the field table;
  `tools/gen_addon_fields.py` generates `addons/JevRadio/Fields.lua` from it. Hand-editing
  the Lua is a build error.
- **4 bits per channel, 12 bits per cell.** Not 8. Sixteen levels tolerate ±8 levels of
  channel error, absorbing gamma, capture colour transforms and compression. Throughput is
  not the constraint; a bad decode is.
- **A calibration row** of known colours, painted every frame. The decoder solves the
  observed transform and inverts it, so the strip reads correctly under any capture
  pipeline rather than only the one it was authored against.
- **Anchored to `UIParent` at a fixed offset, pixel-snapped.** Never to the minimap, whose
  detected circle is unstable between frames. The ROI must be a pure function of window
  size and UI scale; cells are sized through `GetEffectiveScale()` so they land on exact
  pixel boundaries.
- **A sequence counter and a checksum answer different questions.** Bad checksum = misread.
  Frozen sequence = a live strip that stopped updating, i.e. a hung addon. Both set
  `addon_ok=false`; the postmortems differ.
- **Paint only** — stock TBC Lua reads rendered to textures. No `UseAction`, no movement,
  no CVars (PLAN §2.2).

The client rewrites `WTF/` on exit, so client-side config must be written with the client
closed or the edit is silently discarded.

---

## 8. The GuideGraph is generated, then verified — not hand-authored

PLAN §7.6 assumes a human walks 1–20 marking nodes by hotkey. That is the right instinct
(*author against this server, not a retail guide*) with the wrong mechanism, because we
have something strictly better: **this server's own world database**, mirrored to
`data/knowledge/tbc-243.sqlite` alongside all 184 client DBC tables.

A quest joins to its giver and to exact spawn coordinates in one query:

```sql
world_quest_template          -- 6,599 quests: objectives, levels, prereq/next chains
  ⋈ world_creature_questrelation      -- who offers it
  ⋈ world_creature_involvedrelation   -- who takes it back
  ⋈ world_creature                    -- 109,358 spawns with map + x/y/z
```

Everything the spine needs is already in there, exact and complete:

| Node kind | Source table |
|---|---|
| quest accept / turn-in | `world_creature_questrelation`, `world_creature_involvedrelation` |
| quest objectives | `world_quest_template` (`ReqCreatureOrGOId`, `ReqItemId`, counts) |
| vendor / repair | `world_npc_vendor`, `world_npc_vendor_template` |
| trainer | `world_npc_trainer`, `world_npc_trainer_template` |
| graveyard / spirit | `world_game_graveyard_zone`, `world_world_safe_locs` |
| flight | `world_taxi_shortcuts` + DBC taxi tables |
| grind rib | `world_creature` spawn density by level band and area |
| ding gate | `world_player_xp_for_level` |

So the split is:

- **Generated from the world DB:** node positions, quest chains, objectives, services,
  graveyards, level gates. Exact for *this* server, zero human hours, regenerable when the
  DB changes.
- **Recorded by walking:** the polylines *between* nodes, terrain hazards, water, grind-box
  shapes. The DB knows where things are; it does not know how to walk there. Navmesh
  (`mmaps/`, already extracted) may close part of this gap automatically.

This preserves the intent of `DECISIONS.md` V4 — authored against this server — and
strengthens it: generated *from* this server beats recorded *on* it, and the recorder pass
shrinks to what only walking can answer.

Retail packs in `data/sources/` (pfQuest, RestedXP, TomTom, Guidelime) are kept as a
**cross-check, never a source**: diffing them against the world DB is how server
customisations announce themselves.

---

## 9. Process boundaries — so 1 → 10 clients is config

Windows has **one system cursor per session**. No user-mode input method lets ten clients
share one desktop; that needs separate sessions, VMs, background input or dongles. Deferred
(`DECISIONS.md` V5), and must stay deferrable:

- every client is its own process, talking to the orchestrator over a bus
- **no shared state between clients** — none, not even a cache, except through the bus
- all input through one `hid` interface with swappable backends
- stagger client ticks by `client_id * 7 ms` so ten bots are not a chorus (PLAN §8.5)

For the learning goal, one client running clean for hours beats ten running badly. Ten
multiplies data and nothing else.

---

## 10. What would make this fail

| Failure | Guard |
|---|---|
| Perception drift | radio-as-labeller (§5), checksum + sequence, confidence gates |
| Corrupt training set | outcome-filtered labels, `situation_key`, schema versioning |
| Teacher unavailable / rate-limited | zero-teacher-call progress invariant (§0), tested |
| Distribution shift on handover | shadow mode, per-bracket promotion, DAgger feedback (§4) |
| Reward hacking | step-advance primary; a policy that never moves never dies |
| Learning a server exploit | supervisor pauses on absurd gold/XP rate; do not distil it |
| Re-litigating settled calls | `DECISIONS.md`, superseded rows kept |

---

## 11. First milestone

Not "build twelve modules". One vertical slice, fully instrumented:

> **Accept a quest, kill ten mobs, turn it in** — with the record / grade / shadow
> pipeline running throughout.

Everything after that is content.

### Current composition and evidence boundary — 22 Sep 2026

`jev.orch.runtime.ClientRuntime` owns the tracker, guide enrichment, situation key,
verification, accepted decisions, teacher artifacts and shadow records. Both simulation
and live execution use that runtime. `tools/probe_slice.py` delegates to `jev.run.cli`;
there is no second policy loop in the probe.

`jev.run.supervisor.Supervisor` samples/tracks on a 250 ms target, chooses at up to 2 Hz
when the body is free, and records at 2 Hz plus decision/progress/death events. These are
scheduling targets, not a measured live throughput claim. One worker owns the existing
body methods and all input; the next worker starts only after the previous one releases
its keys and mouse buttons. Cancellation is checked at input and perception boundaries,
and sidecar reads have real deadlines. The teacher queue is optional and never awaited.
Blind perception releases input and waits for fresh evidence. Unsupported actions and
exhausted attempts end with a named reason.

`jev.run.body.LiveBody` composes the existing Travel, Interact, ChooseListLine,
AdvanceQuestFrame, Fight, Hunt, Rest, Loot, Repair and Recover. Its executor catalog is
the runtime verifier's capability set. Graph schema 2 supplies typed target names/kinds
from the server DB; human prose is not a targeting interface. Object interaction and
later objectives without their own generated target remain explicit capability gaps.
Geometry and the proven navigation/interaction constants are preserved by this work.

Every accepted arm retains its decision ID, originating step/key and start time through
execution. Outcomes link back to that arm. Tracker completion is distinct from failed
edges, skips and timed rib rejoins. `jev.learn.grade` joins applied decisions to observed
+60-second windows; gaps, unread windows and short closed tails cannot earn positive
labels. JSONL is authoritative, grades are replaceable derivatives, and dataset joins
include the run ID. Synthetic/replay observations are excluded by default. Teacher
artifacts are recorded for review, never auto-installed by the live body.

The responsive synthetic slice exercises this whole recording/grading seam with zero
teacher calls. It proves integration offline. It does not prove the new composition on
Windows, real input/capture latency, model promotion, or unattended levelling.
