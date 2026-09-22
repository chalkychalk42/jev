# Shared targeting: first production evaluation

Run `20260922T192105-3d5fcf` exercised the shared target click gate through the production
Hunt/Fight composition on 22 September 2026. It produced usable body-location evidence
and one accepted terrain point, with no damage, kill, loot or quest progress in the
reviewed sequence. These are separate findings: a click can be on the visible model
without a confirmed game effect, and matching hover can still accompany a terrain point.

The [evaluation manifest](../tests/fixtures/target-localization-evaluation.json) preserves
four original PNG frames, manual region observations, exact action coordinates, decoded
radio facts and execution attribution. The original files were copied without cropping,
recompression or any pixel changes. All file and decoded-frame SHA-256 hashes were checked.
The four PNG sources total **13,742,350 bytes**.

This set has one independence group, `production-evaluation-20260922T192105-3d5fcf`, which
is distinct from the [historical development corpus](TARGET_LOCALIZATION_CORPUS.md).
Frames within this new engagement are not separate independent trials. If these results
inform a later correction, a further independent run is needed to evaluate that correction.

## Observed locations

The game client was 1611×906 pixels at desktop origin `(1821,35)`. Frame coordinates
below subtract that origin from the actual recorded desktop point. Rectangles in the
manifest are approximate visual bounds, not hitboxes or pixel masks.

| Original frame | Recorded frame point | Independent visual observation | Recorded action |
| --- | --- | --- | --- |
| 79 | None | The selected wolf's body, yellow name text and selection ring are visible. Its world health-bar surface is absent. | No proposal and no body click; a valid missing-anchor abstention. |
| 94 | `(830,434)` | Terrain just above/right of the wolf, despite exact selected-unit hover and accepted current geometry. | Right-click delivered. This remains an observed false body location. |
| 99 | `(674,482)` | Visibly on the wolf's back, below the health bar. | Right-click delivered; game effect unconfirmed. |
| 102 | `(652,493)` | Visibly on the wolf's back, below the health bar. | Right-click delivered; game effect unconfirmed. |

Frame 94 is deliberately retained as a limitation. A broad body rectangle contains
background around an irregular model; the point must not be relabeled as a body hit
because it falls inside that rectangle. Fresh hover and current plate-to-ring geometry
do not fully distinguish visible model pixels from the client's larger or delayed pick
region. The precise reason for that client's ownership report remains unmeasured.

Frames 99 and 102 establish that the accepted point can also lie on the visible model.
Their paired follow-up frames, 100 and 103, retain full target health and report no
combat or target attack. Both before and after report melee range. This supports further
measurement of engagement effects; it does not establish a kill, successful facing or
reliable attack control.

The first delivered click, in frames 89→90, was also reviewed: its point `(784,422)` lies
on the wolf's left fur edge and the radio reports out of melee range. It adds no distinct
failure class beyond the retained examples and is not copied into this minimum set.

Earlier no-proposal frames 17 and 26 show no selected wolf in the world view. Frames 59,
68 and 79 show a body/name without a health bar. Keeping those reasons separate prevents
the honest missing-anchor refusal from being mistaken for a failed body detector.

## Evidence and replay limits

The manifest's `reviewed_after` records preserve original follow-up paths, timestamps,
file/frame hashes and decoded facts. Those follow-up PNG files remain in the local run;
only the four primary reference PNGs are included in the portable set. The run's complete
`executions.jsonl` remains the original source of action/arm/decision attribution.

The run used uncommitted targeting changes and did not retain a complete implementation
snapshot. Its Git HEAD alone must not be presented as an exact tested implementation.
No corpse was observed, so this set supplies no Loot acceptance evidence.

The integrity checks in [test_target_corpus.py](../tests/test_target_corpus.py) replay the
original bytes and radio, validate coordinate provenance and preserve independence from
the development group. They do not invoke the locator to manufacture a passing result
for frame 94, and they do not equate body placement with observed damage.

```sh
.venv/bin/pytest tests/test_target_corpus.py
```
