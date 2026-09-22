# Target localization reference corpus

The [reference manifest](../tests/fixtures/target-localization.json) records twelve
manually reviewed real views. Its purpose is to reject false target locations across
shared perception callers, including Interact, Fight and Loot. It contains no locator
output as a desired answer and no quest-specific detection rule.

Each source has its original file SHA-256, decoded frame SHA-256, image dimensions,
archive array key where needed, and an independence group. Run screenshots also retain
their original run path, manifest timestamp and index. Frames from one sequence remain one group;
adjacent images cannot become supposedly independent training and validation examples.

## What the annotations mean

Coordinates use original-image pixels, with the origin at the top left. Rectangles are
`[left, top, right, bottom]`, minimum inclusive and maximum exclusive. Boundaries are
manual estimates from full images and enlarged coordinate-grid crops. No thresholds,
component centroids or solver results supplied the reference rectangles.

- `body_extent` bounds the visible rendered target. Being outside it is a localization
  failure. Being inside it is necessary but insufficient: holes between legs, foreground
  grass and interface occlusion can fall inside a bounding rectangle.
- `visible_body_cores` identifies conservative areas visibly on the model. These are
  observations, not game hitboxes or proof that an actual click connected.
- `visible_ring_extent` bounds the observed selection arc. It does not claim a complete
  ellipse or infer the centre of an occluded/clipped ring.
- `health_bar_extent` refers to the surface of the target's world health bar. Unit name
  text and the player/target interface bars are different observations.
- `bracket` means that both target anchors are visibly available. `abstain` means that
  the existing living-body bracket contract lacks a required anchor or the selected
  target is out of view. `bracket_or_abstain` keeps difficult occlusion cases available
  without turning a plausible guess into a required click.

Radio facts are decoded from each original image and retained separately from visual
identity. The current decoder refuses the two historical name-only NPC captures with a
checksum fault; their visual/historical identity must not be presented as newly decoded
radio evidence. The wolf and current merchant sources decode successfully.

## Coverage

| Reference | What it exercises |
| --- | --- |
| `merchant_selected` | Green merchant ring and bar among stall geometry, overlapping labels and a one-pixel yellow border. |
| `merchant_thin_ring` | Thin green front arc partly hidden by the stall and player. |
| `merchant_other_bars_faded` | Correct selected identity among dimmed neighboring bars; player occludes the target's legs and ring. |
| `friendly_name_without_health_bar` | Visible humanoid and ring, with name text but no world health bar. |
| `distant_target_other_health_bars` | Selected distant kobold has no health bar; unrelated nearer kobolds do. |
| `selected_target_not_in_view` | Radio still targets the merchant after travel; the world view contains terrain and distant wolves. |
| `wolf_distant_grass_distractor` | Small oblique ring and body above large yellow terrain components. |
| `wolf_side_pose_grass_distractor` | Sideways walking body and thin ring amid taller foreground grass. |
| `wolf_ring_and_body_split_by_grass` | A blade cluster splits both rendered body and visible ring. |
| `wolf_red_ring_yellow_health_bar` | Red/orange arc while the health bar stays yellow; radio still reports full target HP and no combat. |
| `wolf_near_front_pose` | Much larger plate-to-feet separation and a frontal body near the right edge. |
| `wolf_clipped_by_frame_and_tooltip` | Target body and ring continue beyond the right edge; tooltip and action bars cover the lower arc. |

The existing `live-willem-targeted.npz` remains a useful original locator test, but is
not included as evidence of terrain discrimination: most of its world image is blacked
out around a small target region. The merchant fixtures in this manifest preserve their
full original world pixels and distractors.

## Baseline failures observed on 22 September 2026

This table describes the locator before the shared targeting correction, not a reference
label to preserve.
All points are outside the independently annotated target body.

| Run screenshot index | Returned torso | Observed mistake |
| --- | --- | --- |
| 61 | `(1134, 511)` | Yellow terrain well below the distant wolf. |
| 64 | `(1288, 495)` | Yellow terrain below and right of the walking wolf. |
| 90 | `(1430, 343)` | Unrelated yellow component beside the red-ringed wolf. |
| 100 | `(1579, 469)` | A component near the health bar is paired back to that bar; the body begins around y=580. |

The existing merchant positives and the three definite abstention cases behave as
expected. Wolf frames 67 and 96 produce points within the observed body and ring
extents. That establishes coarse localization only; no successful click or hit is
inferred from these saved images.

## Coverage if every existing pair is returned

An offline audit enumerated every pair produced by the existing masks and pairing rule,
instead of keeping only the largest ring component. A reference match below requires
both the proposed torso and ring centroid to fall inside the independently reviewed
body and visible-ring extents. This is coarse localization evidence, not a hitbox test.
The table covers the six labeled wolf cases; they are all from the same historical run.

| Screenshot index | Default pair count | Reference matches | What limits the existing proposals |
| --- | ---: | --- | --- |
| 61 | 36 | None | Actual yellow arc fragments have only 40 and 31 pixels; both fall below the existing minimum component area of 60. Adding red candidates produces no additional match. |
| 64 | 11 | One, at area rank 2 | The actual ring gives torso `(1183, 333)`. A larger terrain component currently wins. |
| 67 | 14 | Two, at area ranks 1 and 4 | Separate left/right ring fragments give coarse body proposals. Foreground grass still occludes the model. |
| 90 | 2 | None by default | An explicit red-candidate experiment adds the actual ring and torso `(1302, 364)`; it is one of three total pairs. This does not validate red as a global detection default. |
| 96 | 5 | One, at area rank 1 | The current yellow proposal already lies within the observed body/ring extents. Explicit red adds a second coarse match. |
| 100 | 2 | None | One proposal pairs a badge/border component with a spurious 38-pixel bar. The other uses a far-left visible arc and places the torso outside the body. The large red arc crosses the lower interface exclusion and is not a default candidate. |

Returning all pairs is therefore useful but insufficient. It recovers a valid proposal
that area ranking discarded in frame 64. It cannot recover a component rejected before
pairing in frame 61 or establish a full target position from the clipped/covered ring in
frame 100. No production locator changes were made for this audit.

## Hover evidence and the shared contract

The schema 8 diagnostic `captures/targeting/wolf-hover-1/results.jsonl` adds real cursor
ownership observations. Its event images were inspected independently of their planned
point labels:

- The initial point `(1250, 481)` is visibly inside the selected wolf's health-bar
  surface. It returns `match` with `cursor.has`, `cursor.is_target` and `cursor.world`
  all true. Hover ownership alone therefore cannot authorize a living-body click.
- The early stationary points `(1250, 561)` and `(1218, 545)` lie visibly on the wolf's
  model and also return `match`.
- The point labeled `ground`, `(1200, 600)`, lies inside the selection ring near the
  wolf's legs and foreground stems. Its `match` is not a terrain-negative example.
- The later point `(1205, 560)` is visibly terrain after the wolf moves left and returns
  `ground`. The point label `below-plate` does not describe the surface at observation time.
- The final `sighting` point `(1064, 592)` lies on the wolf's back in the before image,
  `screenshots/000026-20260922T155312.840876Z.png`, which decodes sequence 90. In the after
  image, `screenshots/000028-20260922T155313.545505Z.png`, the wolf has moved left/down and
  the requested point is visibly terrain above/right of the rendered body. That image
  decodes sequence 97, the same sequence retained in the `match` result. A larger pick
  volume, retained mouseover state or another client behavior could explain this; the
  diagnostic does not distinguish them. This final result is not a visually confirmed
  body point.

Those image paths are relative to the diagnostic directory. Event images and the
separately sampled radio observations remain sequential captures, not a claim of an
atomic input/vision transaction.

**Implemented and tested offline; live acceptance remains separate:**

1. Expose a bounded set of ring/plate proposals from shared perception. Treat each as a
   hypothesis; area ranking cannot establish target identity.
2. Retain measured component bounds instead of inventing another pixel drop. For a
   living-body proposal require vertical separation between the bar and ring, place the
   point below the bar's measured surface, and keep it within the paired plate's
   horizontal span. Other overlapping plate surfaces must also be excluded. These are
   necessary geometry checks, not sufficient proof of a body. The colour mask does not
   establish the full input region of a nameplate's native adornments.
3. Require fresh exact selected-unit hover ownership at the proposed point, then recheck
   current geometry before interaction. If the point is no longer in the current target
   bracket, refuse that stale proposal. The moving-wolf example prevents treating an
   earlier ownership match as a durable click authorization.
4. Record input delivery and the observed interaction, engagement or loot outcome
   separately. Neither geometry nor hover can claim that an interaction worked.

`Plate`, `Ring` and the underlying components now retain actual pixel bounds. Acquisition
keeps the previously measured masks; the new proposal API additionally includes red as
an untrusted hypothesis. Interact, Fight and Loot share one `Targeting` instance in the
runtime. Its bounded attempt moves to a proposal, observes a new paint after arrival,
then obtains geometry and radio from one new captured frame. The physical cursor must
remain at its measured endpoint, including absolute-input rounding, through verification.
The final frame cannot predate the hover paint and expires after 0.5 seconds. The final
click sends the button without moving the pointer again.

The living contract requires a separated ring/bar bracket and excludes observed bar
surfaces. The corpse contract proposes the measured prone point on a ring and requires
dead selected-unit hover. Neither contract adds a fixed plate drop, centre-screen click
or per-NPC exception. Already captured verification pixels are persisted with their
original capture time, including when cancellation occurs after input delivery.

Replay retains all three merchant positives and all three definite abstentions. It
supplies useful wolf proposals in frames 64, 67, 90 and 96 while rejecting the clipped
frame 100. Frame 61 still has no valid ring proposal: its visible fragments remain below
the previously measured component-area limit. Returning terrain hypotheses in that case
is not detection success; fresh ownership and geometry must reject them before a click.
All six wolf frames remain one historical independence group.

Outcome tests distinguish delivery, observation and cleanup: refused selection cannot
reuse a pre-existing target; target disappearance or low nonzero HP cannot claim a kill;
missing post-loot radio cannot claim no change; an observed take survives separately in
evidence when UI closure fails. These tests establish caller contracts, not live success.
Real corpse evidence and a new independent engagement remain acceptance requirements.

## Portable replay

All twelve manifest sources are available in the repository: five existing NPZ archives
and seven lossless original PNG files in
[`tests/fixtures/target-localization`](../tests/fixtures/target-localization). The seven
PNG files total **23,599,930 bytes**. They were copied byte for byte from the ignored run
directory; both their original file SHA-256 and decoded RGB frame SHA-256 were verified
before copying and the copied bytes were checked against the originals. No image was
cropped, recompressed or altered.

Each PNG source uses its repository path for replay and keeps the former path in
`original_path`. The timestamp, screenshot index, hashes and independence group are
unchanged. Vendoring these files makes the existing reference replayable from a fresh
checkout; it adds no new session, corpse observation or independent evaluation example.

[`tests/test_target_corpus.py`](../tests/test_target_corpus.py) checks all twelve file
and frame hashes, replays their recorded radio facts, validates annotation bounds and
checks that the seven historical run observations remain one independence group. Missing
fixtures fail instead of silently skipping the replay. Run this integrity gate with
`.venv/bin/pytest tests/test_target_corpus.py`.

## Remaining evidence work

No real corpse capture was found in the existing fixture set or reviewed capture
directory. A corpse reference remains required before claiming the correction is
validated for Loot. Additional independent sessions must cover camera pitch changes,
indoor/outdoor terrain, overlapping same-colour targets, clipping and alternate body
sizes. The twelve current frames cannot establish those broader conditions by
themselves.

Evaluation should report false returned locations, correct abstentions and usable
localizations separately. Refusing every frame cannot count as a successful locator.
Visible region checks are an offline gate; live acceptance must additionally observe
the intended interaction, engagement or loot result without changing target identity.
