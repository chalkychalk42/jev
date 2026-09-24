Jev: plan from live acceptance to sustained improvement
======================================================

**Current implementation:** [the teaching loop](TEACHING_LOOP.md) now provides visual
teacher actions, independent outcome recording and gradual local capability handover.
It supersedes the historical assessment below that learning only reproduces the guide.
The live acceptance gates and shared-mechanism rules remain binding. Offline coverage
does not establish wolf engagement or a soak; transport readiness and its current quota
blocker are documented in [TEACHING_TRANSPORT.md](TEACHING_TRANSPORT.md).

Reviewed 22 September 2026 against `97c4829`, the source, all 34 recorded runs and the
latest Windows acceptance evidence. This document sets the execution order for the next
work. `ARCHITECTURE.md` remains the architectural authority; `docs/PLAN.md` retains the
long-term vision. Acceptance gates below are targets, not claims of completed work.

**End goal.** One character makes sustained, real quest/XP progress, services itself,
recovers from death and interruptions, and resumes after restart. Its recorded outcomes
improve reusable decisions and tested capabilities, reducing unresolved problems and
teacher cost without worsening progress or survival. Prove the Human/paladin early route
first, extend through level brackets toward 70, then replicate the working client.

**Objective assessment.** The runtime is built enough to test. The physical action loop
still fails at wolves. Learning infrastructure runs, but the current learned policy is
restricted to reproducing the scripted guide action. It cannot yet select a better
alternative, and promotion does not reduce the production teacher-request path. More
run data is necessary; those missing behavior paths must also be built.

| Area | What is established | What is not established |
|---|---|---|
| Execution | Shared tracker/body/recorder; measured navigation and earlier quest/combat/loot/recovery methods | Current composition repeatedly completing the wolf quest or an hours-long route |
| Services | Actual repair from 0 to 100%; ten waters bought; travel back to wolves | Full-bag sale, depleted supplies, repeated service cycles without intervention |
| Supervision | 1 Hz screenshots, cooperative stop and released inputs; initial reconnect | Sustained focus/disconnect/crash/recovery behavior in the current composition |
| Learning | Windows worker processes 34 runs without errors; grading, shadow, registry, canary and rollback infrastructure | Any eligible training examples, promoted model, useful alternative learned action or measured improvement |
| Teacher | Subscription transport returned a valid reply; budgeted asynchronous bridge exists | A real gameplay escalation/application, verified requested model routing, reusable artifact installation |
| Content | Source: 143 nodes/45 quest chains. Supported derivative: 99 nodes/31 chains, including two grind ribs | That route actually reaching level 12; character training/equipment progression; continuation to 70 |

**Working rules.** Keep the screen/HID interface and paint-only addon. Generate shared
facts from this server's DB. Preserve measured navigation, corpse-click composition and
the role-based combat engine unless a reproduced failure requires a bounded correction.
Fix the common cause across all callers. A universal pixel drop, extra unstick headings
or a new threshold must not substitute for evidence. Every live test records screenshots
each second, with sequence review, explicit stop control and an accountable input owner.
No pass is earned by skipping a quest, moving the playhead, changing a model label or
letting the watchdog stop a failed loop.

**Mandatory design gate for every package (V37).** Each fix must solve the underlying
failure class through a shared mechanism that scales across applicable tasks. A wolf,
merchant or quest supplies a reproduction and acceptance case, never an identity-specific
branch in the execution code.

- Identify the failing contract, its owning component and every affected caller before
  choosing the correction. Implement the correction at that owner and use it consistently.
- Keep target identities, quest requirements, routes, prices and class profiles in
  validated data. Do not disguise a one-off workaround as another configuration field.
- Compose distinct observed behaviors from shared primitives. Corpse looting and living
  NPC interaction can differ by contract; neither needs a per-NPC implementation.
- Replace obsolete paths instead of accumulating fallback branches, duplicate helpers
  or independent interpretations of success. Keep bounded failure when evidence is absent.
- Verify representative positive, negative and regression cases across affected callers.
  One successful quest proves that case; wider support needs its own evidence.
- Use the smallest coherent shared solution. Global scope does not require a monolith,
  speculative framework or support for every future case before fixing today's failure.

Each implementation review must state the failure class, shared owner, affected callers,
task data involved, verification evidence and any superseded code removed. A proposed fix
that cannot meet this gate remains an identified limitation, not an accepted local patch.

**Execution order.** Packages 1–2 restore productive gameplay. Packages 3–6 establish a
credible single-client soak. Packages 7–8 make early leveling sustainable. Packages 9–11
complete meaningful self-improvement. Package 12 extends and scales the proven result.
Evidence/reporting work can run alongside the body work; adaptive control waits for its
own gates. Do not make all later content or learning features prerequisites to the first
successful wolf turn-in.

1. **Make action evidence useful before changing another aim rule.**

   **Partial implementation:** shared arm/operation attribution and causal primitive
   records are documented in [Execution evidence](EXECUTION_EVIDENCE.md). The remaining
   gaps and the acceptance gate below still apply.

   Record nested acquisition, engagement, fight, loot, service and recovery events through
   the shared recorder. Link child events to the original run, arm and decision; a pull
   inside one hunt is not automatically a new or independent coach decision. Include the
   observed target, attempted click, pre-click frame reference, camera/config identity,
   input accepted/refused, radio error, HP/counter changes, timings and termination cause.
   Keep the 1 Hz sequence and add event captures where it misses the actual click.

   Add specific bounded outcomes for repeated selected-target/no-damage approaches,
   failed corpse acquisition, empty camp, occlusion and refused input. Preserve the last
   failure sequence. The 15-minute global no-progress watchdog remains a final bound;
   it must not be the first detector of a repeated local failure. Correct stale comments
   and skill success predicates that claim facing, healing or completion without evidence.

   **Pass:** one recorded attempt can explain selection → attempted action → actual
   outcome, including why it stopped. No unchanged failed action loops indefinitely,
   and child records do not duplicate decision credit.

2. **Fix shared selected-unit localization and verify what the click actually does.**

   Build a labeled reference set from real merchant, wolf, kobold and corpse frames,
   including grass, partial rings, red selection flashes, overlapping/dim plates,
   moving targets, camera changes and targets outside the view. Keep separate frames
   for evaluating a proposed method, rather than tuning and judging on the same image.

   Carry confirmed target identity through fresh observations. Solve the common locator
   used by Interact, Fight and Loot; a Fight-only repair would leave the grass bug in
   corpse handling. Measure the selection-to-body association and the effect of a
   right-click on facing/engagement at relevant distances. Replace unsupported largest-ring
   and fixed-drop fallbacks with the shared target-evidence contract; insufficient evidence
   produces a bounded refusal.
   Distinguish successful input delivery from an actual hit or opened interaction.

   Select the mechanism from evidence. Brighter masks, nearest-pair ranking and simple
   width/overlap gates already failed on the saved wolf frames. If additional visible
   selection/hover feedback is necessary, first measure whether the client exposes the
   needed evidence; do not assume it exists or build a new search skill speculatively.

   **Pass:** known false grass matches are rejected, accepted positions agree with
   annotated body regions on reference/evaluation captures, and legitimate refusals are
   counted separately. Only live clicks prove interaction: supervised acquisition must
   produce actual damage on the intended target across multiple views/distances, with
   merchant repair/restocking still passing.

3. **Complete the wolf quest through the production loop.**

   Exercise acquire → engage → kill → corpse acquisition → loot → objective update →
   continue → turn in. Keep empty corpses legitimate, but distinguish an observed empty
   interaction from a click that never acquired a corpse. Use observed inventory/quest
   changes for item progress. Preserve top-up before the next pull and loot before service.

   **Pass:** quest 33 reaches its observed 8/8 requirement, the turn-in is confirmed and
   the next guide step starts without manual game input. Collect at least 20 complete
   supervised kill/loot cycles across representative views as the route continues; never
   delay a completed quest's turn-in to fill that quota. Report failures and retries,
   not just the successes. Retain usable observation windows after decisions for grading.

4. **Prove service, survival and route continuity together.**

   Exercise full bags → merchant → sale of allowed non-quest junk → return to the same
   objective; partial/full repair; food/water consumption and restocking; low copper;
   unavailable stock; and bags containing no saleable junk. Preserve quest items, reserve
   money for required services and prevent repeated trips that cannot change the result.
   Define the explicit outcome when the character cannot reach or afford supplies.

   Exercise death and auto-release → corpse route → resurrection → same incomplete
   objective, including restart while ghost. Verify combat interruption during travel,
   rest and service. Measure Holy Light's completion under pushback: slot cooldown or
   unavailability alone must not be reported as a landed heal. Test moving NPCs within
   the same bounded interaction contract. Measure one real obstruction only after
   separating it from unfocused/refused input; preserve the existing recovery repertoire.

   **Pass:** each interruption/service scenario resumes the correct quest with completed
   facts intact, or produces one specific bounded failure. No false completion, repeated
   unaffordable service trip, idle heal loop or uncontrolled corpse-run loop.

5. **Make sustained operation observable and recoverable.**

   Use one definition of progress across tracker, watchdog, grades and the existing text
   eval board. The board currently counts step-ID changes as progress and joins some
   records by decision ID alone: fix those semantics before using its freeze verdict.
   Qualify identities by run/client and distinguish true completion from skip/retry/rejoin.
   Report real quest/XP progress, failed target acquisition, no-hit approaches, loot
   outcomes, service cost, death/recovery time, intervention count, observation gaps,
   unresolved/h, actual teacher calls, eligible examples and controller/model attribution.
   Refresh the existing board/status; a new dashboard framework is unnecessary.

   Validate environment/profile fingerprints at startup: client/addon schema, resolution,
   UI/pointer/camera assumptions, graph revision and action bars. Live-test focus loss,
   idle and network disconnects, stop during reconnect, stale radio, process termination,
   sidecar failure and restart. Reuse the bounded Session/focus paths. Add external process
   supervision where process death cannot clean up its own inputs, with bounded restarts
   and a terminal failure reason rather than endless relaunch. A new input owner must not
   race the old one. Demonstrate teacher and learner outages leave the body responsive.

   **Pass:** 40 minutes of real progress without human input, then the two-hour bracket
   gate using corrected metrics: deaths/h <2, no stuck episode >15 seconds, teacher <3/h,
   and genuine progress. Here, stuck means accepted movement failing to produce movement;
   intentional casting, rest, service, reconnect and recovery waits are separate states.
   Report interventions, service/recovery coverage and all stops;
   an uneventful run does not prove an unexercised recovery path.

6. **Keep the corpus usable through long sessions and restarts.**

   Add bounded recording segments, durable session/end markers, resumable incremental
   grading and explicit archival/retention. Preserve decision windows across segment
   boundaries and the real parent-session identity. File rollover must not manufacture
   independent training runs or place the same trajectory in training and evaluation.
   Keep representative older evidence available when the newest-run scan limit is reached.

   Preserve required 1 Hz screenshots during live tests. Budget their storage, retain
   failure sequences and manifest links, and expose disk/capture failures promptly.
   Define active/archive retention before deletion is enabled. Back up playhead, registry,
   configuration and evidence together so a restart retains their provenance.

   **Why now:** the learner rejects an entire run above 256 MiB of JSONL and scans at
   most 500 runs. The latest 106.8-second sample extrapolates to about 18.8 MiB/hour of
   JSONL and 12.2 GB/hour of screenshots. The JSONL limit would be reached around 13.6
   hours at that rate. These are short-run estimates, not measured soak rates.

   **Pass:** rollover, restart and disk-pressure tests preserve joins and mature grades;
   an overnight soak stays within the planned budget and continues producing usable
   evidence. A segmented overnight run still counts as one independent session.

7. **Build early character progression, not just starting-bar combat.**

   Generate the correct class trainer and available ranks from this server's facts.
   Current trainer nodes use generic NPC flags; simply adding a trainer click would
   not choose the right trainer. Observe offered/learned spells, buy intended ranks,
   verify the resulting action bars and use the learned ranks through the existing
   role-based combat engine. Detect stale/missing slots instead of silently pressing them.

   Add observed quest-reward selection, bounded equipment upgrades, safe equip/bag
   expansion, appropriate supply tiers and purchase/reserve rules. Derive level/class
   requirements and costs from the server. Support talents, resource differences and
   class-specific supplies as the selected character actually reaches those needs.
   The 52 generated profiles currently describe fresh-character bars, not progression.

   **Pass:** the current class levels through the early bracket, learns and uses required
   ranks, equips the intended items, retains quest items and keeps enough money for
   planned services. Verify the complete service transaction and resulting behavior.

8. **Prove level coverage and add objective types only where needed.**

   Audit prerequisite order, quest eligibility, attainable XP, travel costs, camp levels,
   respawns and the two heuristic grind ribs. Add explicit level goals and reachable
   fallback camps where exclusions leave a gap. Completing the current graph is not
   equivalent to reaching level 12; install a real continuation instead of exiting or
   replaying already completed quests.

   Current exclusions are concrete (24 September): Wanted: "Hogger" (176), whose target is
   an elite; quest 147's missing earlier prerequisite 123; and out-of-region quest 109.
   Exploration (V77), quest objects gathered or opened by their tooltip name (V86, V87)
   returned 62/76/239, 3904/3905/5545 and 37/45/71/39/59, none yet completed live. Seven
   service nodes still lack executors: four training, two hearth and one flight. Keep
   these explicit. Add quest-item use,
   spell/event objectives, concurrent prerequisites and wider radio counters only when
   required by selected content. Do not describe all catalog names as independent gaps:
   some are already internal compositions of the 14 live executors.

   **Pass:** the chosen Human/class route reaches level 12, with earned completions
   surviving restart. Each added objective type completes a representative real quest,
   refuses unknown evidence correctly, and reduces the exclusion manifest for its
   stated reason. Unsupported content remains excluded without reward.

9. **Produce training evidence and validate the real teacher path.**

   Expose counts and reasons at each stage: decisions recorded → actually applied →
   mature observed windows → positive outcomes → eligible examples → independent
   training/evaluation coverage. Current audit: 27 decisions, 14 applied, three mature
   grades, all reward zero; 13 old unlinked decisions and 11 short applied tails have no
   grade. Successful repair/restock is local skill evidence, not automatic quest-progress
   reward. Never shorten the window or loosen evidence requirements to make a candidate.

   Prove real applied choices acquire complete +60-second outcomes after productive play.
   Keep nested execution evidence separate from strategic choices and prevent duplicate
   credit across long hunts, retries, interruptions and recording segments. Treat missing
   observations and implausible unexplained state jumps as missing/untrusted evidence.

   Exercise a genuine unresolved case through the production asynchronous teacher queue:
   deduplication, request budget, actual dispatched model, response, verifier, accepted or
   stale/rejected outcome and cancellation. The transport probe requested Sonnet but
   reported Haiku; resolve the routing and retain actual model/request identity through
   application. Zero teacher requests during healthy scripted play is desirable, not
   a reason to manufacture calls. Verify outage and exhausted budget do not stall play.

   **Pass:** inspect several correct positive and negative joins from real sessions,
   produce a representative dataset, and demonstrate one real production escalation
   with honest provenance. Existing default training minimum remains 200 examples from
   at least three independent training runs plus held-out runs.

10. **Give the learned policy meaningful, bounded authority and prove improvement.**

    First choose one useful strategic decision point where two currently executable
    alternatives exist—for example continuing the current camp versus a validated
    alternative after repeated failures. Enumerate allowed choices from current facts;
    record the offered set, selected choice, concrete parameters, controller/version
    and eventual outcome. Extend grounding beyond equality with `_guide` while preserving
    emergency, combat, service and recovery precedence. Do not give the model raw keys,
    arbitrary coordinates or invented capabilities.

    Consult competent local policy/cache before unnecessary teacher requests; confidence
    and coverage must be calibrated from outcomes. Wire the per-bracket reduction in
    teacher requests into the actual runtime. Merely changing `armed_by` is not learning.

    The current promotion evaluator measures progress, deaths and stalls, not teacher
    savings. Extend its evidence and comparison rule before cost-only improvements can
    promote: require measured call/token savings with no regression in progress,
    survival or stalls, under comparable encounters and teacher availability/budgets.
    Keep gameplay quality and cost results separate so an outage cannot masquerade as
    learned efficiency.

    Use existing candidate/shadow/canary/registry/rollback components. Fix the trial unit
    before enabling adaptive control: assignment currently hashes a changing situation
    key, while +60-second outcome eligibility requires one controller throughout.
    Choose a stable, attributable trial boundary and handle required scripted preempts
    honestly; never suppress safety or count mixed control as pure candidate evidence.

    **Pass:** a candidate makes a useful legal choice different from the baseline;
    independent, matched outcomes establish improvement in progress or teacher cost
    without worse deaths/stalls. Existing canary evidence minimums include 30 independent
    windows, three runs and 1,800 observed seconds plus coverage/support checks. Demonstrate
    candidate → shadow → bounded live trial → promotion, and separately corruption,
    timeout, regression and restart rollback. Agreement alone is not the acceptance metric.

11. **Make teacher improvements reusable, with a tested artifact lifecycle.**

    Distinguish strategic policy learning from improvements to perception, graph facts,
    skills and combat profiles. The current classifier does not learn a better corpse
    click. Teacher artifacts are saved proposals; no production path currently validates,
    trials and installs them. Existing skill/promotion ledgers are not a wired lifecycle.

    Define typed/versioned proposals for supported changes, with source revision,
    reproduction evidence, tests and expected effect. Validate references/contracts;
    replay the failure and regression set; run a bounded live trial; promote a tested
    version or reject it; restore the prior version on regression. Integrate or retire
    dormant lifecycle components so there is one authority. Keep raw generated code out
    of the running input loop. Any later learning of visual/motor behavior needs its own
    trustworthy screen-position/action evidence; radio state is not automatically a
    correct click-location label.

    **Pass:** one repeated real failure produces a reusable candidate through validation,
    trial, rejection/rollback and eventual promotion. Single-client trials can prove the
    pipeline, but stable skill promotion retains the architecture's two clients × three
    runs requirement; complete that gate after two-client isolation in package 12.
    Do not confuse it with policy-model promotion, which has its own independent-run
    gates. The baseline still completes its supported route with the teacher disabled.

12. **Extend by proven brackets, then scale.**

    Generate and verify each next bracket's primary route, alternative camp, class
    progression, suppliers, recovery and level/XP budget. Add graph handoff and cross-zone
    coordinate continuity. Build hearth binding/use, flight discovery/use and boats only
    where the selected route needs them. Add riding/mount ownership and use, later supply
    and money requirements, and the Outland transition from exact server facts.

    Pass the same progression/service/recovery/soak gate per bracket before treating it
    as reliable or reducing teacher involvement. Do not infer level-70 coverage from
    having a database of quests. Choose a grind-first bypass when it is verified and
    cheaper than supporting an optional complex quest.

    Once one client is productive, prove two isolated Windows input/capture sessions,
    client-specific memory, a shared budgeted teacher and correctly joined shared corpus.
    Then consider six to ten clients and additional starts/classes. Process death or
    failure in one client must not affect another. Addon-off play remains a separate,
    explicit capability branch; the current blind-stop behavior does not implement it.
    Dungeons, playerbots, broad class expansion and a Fight rewrite are not on the
    current critical path.

**Immediate build package:** shared child action evidence and failure reasons → labeled
unit/corpse reference corpus → measured locator/engagement correction across all callers
→ wolf meat turn-in through the unchanged production loop. In parallel, correct metrics
and design session-safe recording rollover. Use the first successful runs to validate
labels; do not wait for new models before establishing the playable baseline.

**Evidence and source map.** Live results and current limitations: `STATUS.md`, final
22 September entry; run `20260922T131407-487841` and its screenshots. Shared locator and
fallbacks: `jev/perceive/units.py`, `jev/clients/fight.py`, `jev/clients/loot.py`. Nested
recording: `jev/run/body.py` and `jev/run/hunt.py`. Learned authority and canary assignment:
`jev/learn/registry.py`; teacher ordering: `jev/orch/runtime.py`; outcome attribution:
`jev/learn/evidence.py`; storage bounds: `jev/learn/worker.py`, `jev/learn/episode.py`.
Metric differences: `jev/eval/counters.py`. Content coverage: `jev/guide/route.py`,
`jev/guide/generate.py`; starting profiles: `jev/world/combat.py`. Live procedure and
platform stores: `docs/OPERATING.md`. No client input or new live test was performed
for this planning audit.
