# Motor learning and teacher handover

**Status, 26 September (V225).** The training, the live evaluation, the canary and the
handover described here went: no student is trained in a session (V174) or acts. The
evidence contract below holds as written (`record`, `finish_episode`, `ingest_run`), and
`predict` still gives the proposal of a student training published before, recorded as
the tutor's shadow. The rest is the record of what was built.

`jev.play.learning.MotorLearner` learns complete bounded actions from measured effects
and successful completed episodes. It complements the existing coach learner: the coach
chooses guide-level work, while this student learns controls such as direction, duration,
action slot, and grounded click proposals. Teacher authorship alone never supplies a
success label.

## Evidence contract

`record(row)` retains every attempt atomically, including failures and interrupted input.
Required fields are `run_id`, `episode_id`, `decision_id`, `capability`, `before`, `after`,
`action`, `expected_effect`, `outcome`, `author`, `controls_fingerprint`,
`knowledge_fingerprint`, and explicit `synthetic`. Observations contain an ID, flat radio
`values`, structured `state`, screenshot identity under `screen.sha256`, numerical visual
`features`, viewport `size`, and semantic `context`. The full source records retain image
references; model artifacts contain numerical features and trace IDs.

The effect judge, outside the model, supplies `outcome.verified`, `success`, `effects`,
and `fatal`. The expected effect must occur in the measured effects. Before and after
must have different observation IDs. Rejected input, unknown outcomes, and synthetic
experience cannot become successful examples.

`finish_episode(episode_id, run_id=..., outcome=...)` finalizes the independent task
result. A training example additionally needs terminal `verified=True`, `success=True`,
positive measured `progress`, and no fatal outcome. Moving the character or changing the
picture alone cannot graduate approach/navigation. These effects only receive episode
credit when the same completed episode achieves a useful objective. This is conservative
sequence credit; it does not claim every preceding action caused the eventual success.

Decisions record actual `cost.teacher_calls`, `input_tokens`, `output_tokens` and
`elapsed_s`. Unknown cost prevents promotion. Calls per observed hour and per decision
are reported alongside the raw counts and observation duration.

Promotion compares completed episodes, including teacher corrections in **all** their
capabilities. It counts episode reward once and uses measured episode duration. The
baseline contains separate episodes with no student actions. A student's zero-call
decision is not called a cost saving if later teacher corrections do the work. Useful
progress per hour must hold while calls per useful progress and per observed hour fall.

`ingest_run(path)` recovers finalized `play-actions.jsonl` and `play-episodes.jsonl`
records after a persistence failure. Requests and accepted-but-unfinished input cannot
be imported as outcomes. Duplicate identical records are idempotent; conflicts and
complete corrupt lines are reported. An incomplete trailing JSONL line is ignored.

## Student and coverage

The first student is an inspectable nearest-neighbour action classifier. It fits structural
action and expected-effect labels, numerical scaling, and coverage radius from successful
examples in independent training runs. The radius is calibrated from cross-run nearest
neighbours and bounded by an explicit evaluation setting. It does not fabricate turns,
durations, or spell slots absent from observed examples.

Continuous parameters such as hold duration, normalized coordinates and camera delta
share support under their structural action (including direction). A locally supported
observed example nearest the parameter median supplies the action; the output never
interpolates coordinates or invents durations. Successful cross-run observations calibrate
parameter-error coverage without hand-picked pixel rounding. Held-out reporting separates
structural agreement, parameter coverage and exact agreement. Live canary outcomes remain
the decisive evidence that a supported parameter choice works.

Visual features form a separate distance term, and only spatial actions carry it: motion,
turning, camera and world-point proposals need an owned screenshot and a familiar scene.
A state decision - a routine choice, a key tap, an action slot, an observe - is matched on
the radio state alone, so running COMBAT_PROFILE with a wolf selected transfers between
trees while a turn learned in one picture does not transfer to another. Missingness, incompatible semantic state, conflicting action support,
unseen scenes, changed control/knowledge fingerprints, and relevant recorded failures
cause abstention. Model matching excludes raw goal text, quest IDs, NPC names and absolute
map positions. The current semantic skill, step kind and viewport geometry remain context.

World clicks store visual-conditioned coordinate hypotheses, not a guarantee that a
pixel belongs to a target. Prediction rebinds expected identity from the current context;
the executor must independently move the pointer and verify fresh matching world hover
before delivering a button. Coordinate-free world clicks are excluded from reusable
labels. UI clicks use the executor's painted semantic controls, not remembered pixels.
A routine the tutor delegates inside an objective (COMBAT_PROFILE, LOOT, FACE_TARGET, ...)
is a whole-action label when it carries no parameters of its own, so choosing the right
routine can be handed over like any control. Arming guide-level skills remains the coach's.

Episodes close at each verified unit of objective progress - a quest counter gain, accept,
turn-in, or a kill toward a level - and the objective continues as a new episode. Progress
earned before an action budget runs out is therefore learnable, not discarded.

Failed attempts remain in the corpus and veto the same learned action in a covered
context. Input refusal and cancellation are retained without pretending the attempted
motor action executed. The model is deliberately small and can abstain frequently. The
16×9 visual descriptor is a first coverage signal, not a general visual understanding
model; teacher assistance remains necessary where it cannot distinguish reliable actions.

## Measured lifecycle

Production defaults are reviewable in `LearningConfig`; changing them changes the model
artifact's configuration, not movement/calibration constants.

1. Fit needs at least 40 successful examples across three training runs. At least 20
   successful examples across two other runs are held out. Explicit encounter IDs link
   runs into indivisible groups. The model must cover at least 50% of held-out examples
   with at least 90% structural action/effect agreement within the calibrated parameter
   coverage. Exact agreement is reported separately and is not silently relabelled.
2. The candidate remains frozen while gathering live shadow evidence. At least 50
   teacher/human decisions across three further runs must reach 90% agreement and have
   independently successful episodes. Neither fitting/evaluation runs nor their linked
   encounters can count again as live evidence.
   Agreement uses actual supported proposals; abstention coverage is reported separately.
   This permits gradual handover of a supported subset while Jev handles novel contexts.
3. A bounded canary receives approximately 10% of eligible decisions, selected
   deterministically by decision ID. It expires after two hours without sufficient
   evidence. Novel situations still go to the teacher.
4. Promotion needs at least 30 actual successful student outcomes across three live
   runs, complete successful episodes, and at least 30 comparable teacher decisions.
   Student task success and useful progress per hour must hold while measured teacher
   calls decrease per useful progress and per observed hour. The promoted capability
   retains 10% teacher audits. A sufficiently observed post-promotion regression in task
   throughput or total teacher cost revokes that capability's authority.
5. An observed student failure immediately revokes that capability's candidate. Unknown
   or interrupted outcomes return it to shadow collection without permanently labelling
   the action bad. Other capabilities keep their own states. Negative completed episodes
   also revoke authority even when individual keypresses produced their expected effects.

New independent successful runs trigger refreshed candidates. New weights start in
shadow and repeat the evidence gates; promotion is never inherited from old weights.
Previously published artifacts remain available for review. A blocked model requires
new successful teacher/human correction from a later independent run before retraining.
The current implementation rolls back authority to the teacher, not automatically to an
older student with possibly overlapping failure modes.

## Durability and boundaries

Corpus records, episode outcomes, models and registry updates use atomic JSON writes.
Models have verified SHA-256 checksums. Training uses a separate lease and an immutable
corpus snapshot. Publication checks the registry generation under a short write lock, so
a concurrent live rollback cannot be overwritten by a trainer finishing later. Fitting
can be cancelled before publication. The model bounds its exemplar count with a
deterministic run-interleaved reservoir; omitted examples remain in the corpus.

Offline tests exercise the complete lifecycle using explicit fixtures. Their passing
does not establish live competence or activate any production capability. Initial live
operation remains teacher-led until real records satisfy these gates.
