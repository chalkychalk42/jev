# Execution evidence

Roadmap package 1 is **partially implemented**. The shared recorder now preserves nested
body operations and their causal observations. Reliable targeting, complete action/frame
provenance and several outcome predicates remain unfinished; this is not an unattended
readiness or learning-promotion claim.

## Attribution and streams

`ClientRuntime` assigns an `arm_id` when it creates an arm. The ID survives an accepted
decision that agrees with the existing arm; another arm receives another ID. Tick and
top-level skill rows carry that optional field. Existing recordings without it remain
readable.

At worker dispatch, the evidence binding freezes run, client, arm, decision, controller,
original guide step and situation. Tracker movement cannot relabel a fight or loot tail
that is still finishing. Each body thread has its own context.

Nested observations go to **`executions.jsonl`**, separate from `ticks.jsonl`,
`decisions.jsonl` and `skills.jsonl`. They allocate no ticks, decisions or top-level skill
results. A recoverable failed pull inside a successful hunt therefore cannot accidentally
disqualify the parent through the strategic skill-result stream. The grader and dataset
continue to use their existing applied-decision and observed-outcome gates.

| Fields | Meaning |
|---|---|
| `run_id`, `client_id`, `arm_id`, `decision_id`, `armed_by`, `step_id`, `situation_key` | Frozen dispatch attribution; missing legacy identity is not invented. |
| `operation_id`, `parent_operation_id` | A nested operation and its enclosing operation. Begin/end rows share an operation ID. |
| `event_id`, `phase`, `operation` | Unique row identity; `begin`, `event` or `end`; semantic operation name. |
| `t`, `duration_s`, `tick_id` | Wall time, monotonic elapsed duration and the latest recorded tick marker. The marker is not an atomic radio/image snapshot. |
| `code`, `detail`, `data` | Native return/exception information and structured observations, not reward labels. |

The single `Recorder` serializes writes, flushes each JSONL row and rejects writes after
close. Parquet conversion includes the new stream; old runs need no fabricated execution
rows. A missing end after a crash or storage failure stays missing, not successful.

## Primitive API and lifecycle

```python
from jev.run.evidence import event, operation, traced

with operation("target.acquire") as span:
    event("selection.observed", data={"observed_name_id": observed_name_id})
    span.finish(code="confirmed", data={"point": point})
```

`@traced("fight")` records a method's native enum/boolean/None result and detail.
Exceptions record their class and message. A return of `true` means the method returned
true; it is not an inferred hit or completed quest. Outside a bound worker the helpers
do nothing. Expensive optional metadata can be guarded with `span.enabled`.

The root `worker` end row carries the final `Result` after input release, including any
cleanup failure. Recording failures remain visible and cannot prevent the worker from
signalling completion. Supervisor cleanup still owns cancellation, joining and the
top-level skill outcome. This does not add a second controller.

## Observations now recorded

- Hunt objective counters and completion flags, station approaches and fight summaries.
- Fight acquisition, plate candidates, proposed click geometry, radio target identity,
  target/player HP, combat state, approach requests, ability requests and heal observations.
- Interact selection identity between the plate and model clicks, proposed positions and
  observed quest/gossip/vendor/loot windows.
- Loot ring proposals, objective/money/bag observations and the signal that changed.
- Rest vitals and consume requests; recovery life/corpse observations and popup proposals.
- Repair durability observations; vendor sale/purchase requests and verified exact
  money/inventory changes.
- HID click requests/returns and input refusals. Movement accounting now checks Windows'
  accepted event count instead of counting planned cursor segments as sent.

These primitive events use existing reads. Event screenshots are available through
`Screenshots.capture_event`; automatic pre-click image links are not yet attached to
every production action. The requested 1 Hz monitoring remains a separate visual stream.

## Remaining evidence limits

Input delivery does not establish an effect in the game. Existing engagement returns do
not prove facing; the shared locator can still select grass. The [hover measurement](HOVER.md)
establishes selected-unit ownership but also matches native nameplates. Production use
of the new hover primitive remains disabled pending a sufficient body-click contract.

`Looted.NOTHING` means no observed change after clicking, not a confirmed empty corpse.
Fight's existing kill inference can return `KILLED` without a last HP observation.
Its heal counter can treat a slot becoming unavailable as a landed heal; the new events
retain that signal separately from HP rising. Repair/recovery popup methods still have
paths that ignore the input return value. Their native codes remain diagnostic evidence,
not stronger claims supplied by the recorder.

Package 1 still needs consistent pre-action frame and camera/config references, complete
radio-error attribution, corrected outcome predicates and bounded local no-progress
handling. Its acceptance gate remains one recorded attempt explaining selection,
attempted action, actual result and termination without duplicate strategic credit.

Offline integration tests exercise real primitive composition with fake client readings,
original attribution, cancellation, cleanup/logging failure, concurrent producers, old
corpus compatibility and execution parquet round trips. Live gameplay acceptance remains
a separate gate.
