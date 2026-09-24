# Status log

Append-only, newest at the bottom. **DID** what landed · **NOW** what is in hand ·
**NEXT** what is blocked or chosen. Read this when you start.

---

## 2026-09-20 — scaffold and the four frozen seams
DID: repo, architecture and the seams that are expensive to change later.
- **`ARCHITECTURE.md`** — the plan reshaped around a rate-limited teacher. The headline
  metric is `unresolved/h`, not tokens/h. Labels come from outcomes, not authorship, so a
  wrong teacher call costs nothing. `situation_key` carries four jobs at once. The teacher
  writes artifacts before actions. The radio labels the vision heads.
- **`state_v1`** — 57 fields, tri-state throughout: unknown is never rendered as False.
- **JevRadio wire format** — 372 payload bits in 33 cells on a 12x4 grid (72x24 px at 6 px
  cells). 4 bits per channel, not 8: sixteen levels absorb gamma and capture transforms.
  Calibration row doubles as a locator, so the strip survives the window moving. Checksum
  and sequence counter answer different questions — misread vs hung addon.
- **`situation_key`** — teacher dedup, counterfactual join, agreement bucket and cache, one
  key. Versioned inside the key so changing bins invalidates the cache, not the corpus.
- **Episode store** — ticks / decisions / grades, JSONL on the hot path because a run
  normally ends in a crash and parquet buffers.
- **Verifier** — 7 named rules. A refusal is never a reason to stop.
- 108 tests, ruff clean. Nothing needs the game to run.

Decisions: `DECISIONS.md` V1–V13. The significant one is **V5** — the GuideGraph is
*generated from this server's own world DB* (6,599 quests, 109,358 spawns with exact
coordinates), then verified by walking. The recorder pass shrinks to routes and hazards,
which is all the DB cannot answer.

NOW: the scaffold is complete and the seams are frozen. Nothing drives the game yet.

NEXT, in order:
1. `guide/generate.py` — emit the 1–12 spine from the world DB, one zone, with `on_fail`
   edges. This is now a query, not a walk.
2. `JevRadio.lua` — the painter, against the generated `Fields.lua`.
3. `radio_frame.py` — locate the strip from its markers, sample cells, decode.
4. The scripted GOAP coach, so the zero-teacher-call invariant becomes enforceable.
5. First vertical slice: accept a quest, kill ten mobs, turn it in, fully recorded.

Open: **V7**, the multi-client input path. Not blocking until Gate B.

## 2026-09-20 — three parallel streams, and the schema gaps they exposed
DID: JevRadio (addon + frame decoder), the teacher queue, the eval board and
distillation, plus the guide generator, tracker, skill catalog, scripted coach, client
runtime and headless simulator. **365 tests, ruff clean, none need the game.**

The GuideGraph is generated from the server's own world DB, not walked. All eight
starting spines build; Human 1–12 is 132 nodes / 47 quests / 127 positioned, starting at
"A Threat Within" in Northshire Abbey with the Kobold objective at Echo Ridge Mine, and
ribs on real spawn clusters (111 Stonetusk Boar at 43,81).

**What the work found, rather than what it built:**
- `unresolved/h` was 45% of ticks. `fight.no_target` was marked uncertain, so 42% of a
  run escalated to ask the teacher to pick a target. It fell to 3%, and the remainder is
  exactly the blind ticks — the only ticks needing help are the ones that cannot see.
- Ribs were one-way; a rib is shared by every step in its zone so only the caller knows
  the way back. And a 1–12 rib exiting at "level 12" is not a detour, it is the rest of
  the game.
- `QUEST_MISSING` on accept nodes fires on arrival and skips every quest in the graph.
- Entry facts read during a perception outage stayed `None` forever, silently degrading
  a step's exit condition.
- The markers-are-unique claim in `fields.py` was false; the decoder locked onto
  payload-derived pairs until it scored the calibration row instead.
- `bars.ready` at 12 bits made "all twelve off cooldown" — the ordinary out-of-combat
  state — indistinguishable from unknown.
- Nine skills wrote `success=lambda s: False` because the tracker judges them. That is
  indistinguishable from "tried and failed", and PLAN §10 would have retired the whole
  travel and questing half of the catalog.
- The runtime counted `unresolved` in memory and wrote nothing, so the board read zero
  for a run full of them.

Schema changes forced by the above: `armed_intent` on ticks (agreement was measurable on
a minority of the run and called the whole of it), `escalated_from` on decisions,
`GradeRow.t`, `artifacts` on decisions, a fourth `skills` stream, and `state.quests` as
tri-state — `None` unread, `()` read-and-empty, which the dataset now learns from.

NOW: the brain runs end to end headless. `python -m jev.clients.sim --ticks 500` walks
the spine, dies, recovers, grinds and rejoins; `python -m jev.eval.board runs/<id>` reads
it back.

NEXT: capture and HID are the only untouched layer, and they are the ones that need the
Windows side. Then a live run.

BLOCKED: **the Claude subscription is out of usage credits** (429 on the primary model;
`--model haiku` works). Not blocking the build — the loop ran 900 ticks to level 16 with
zero teacher calls, which is the invariant doing its job — but no live teaching until it
resets or the GLM rung is wired.

## 2026-09-20 — the body, and the addon proven by running it
DID: scope cut to the reviewer's line — Windows capture + HID + one live 1–12 slice,
nothing else. README no longer claims a stale test count.

- **`jev/clients/win32.py`, `hid.py`, `capture.py`, `live.py`** — `ctypes` only, no new
  dependency. `SendInput` with **scan codes** (games read the DirectInput path; a virtual
  key alone is a press the game may never see, and it fails silently), extended-key flag
  on the arrows, Bezier mouse paths, per-event drawn delays and a per-client stagger.
  Input **refuses when the window is not focused** — a bot pressing `1` into the wrong
  window has not failed to attack, it has typed into somebody's chat.
- Capture is GDI via `BitBlt`, with `PrintWindow` available; **a black frame raises**
  rather than returning zeros, because silent black feeds a decoder that finds no strip
  and a vision head that sees no windows, three layers from the cause.
- `LiveSource` satisfies the same `Source` protocol as the simulator and never raises:
  a lost window, a black frame, an unlocatable strip and a failed checksum each return a
  state that says so. **Vision is not written, so windows are `None`, not `False`** — an
  addon-off client is blind rather than confidently wrong.

**The addon is no longer unproven.** Installed `lua5.1` and built a stubbed 2.4.3 client
(`tests/lua/`), then ran the real `Helpers.lua` / `Fields.lua` / `JevRadio.lua` under it,
drove the addon's own `OnUpdate`, and fed the painted cells back through the Python
decoder. Values set on the stub come back out unchanged. Three real defects found by
executing it that no amount of reading would have caught: a missing `SetToplevel`, frames
not registered as globals by name (so `paint()` never ran at all and every payload cell
was black), and `nil`-versus-`false` in the stub quietly testing the opposite of what it
claimed. `luac -p` says all three files are valid; my hand-rolled block-balance checker
had said one was not, and it was wrong.

Copied to `C:\Games\WoW243\Interface\AddOns\JevRadio`.

NOW: 400 tests. The brain is simulated, the addon is proven in a harness, capture and HID
are written and unexercised — nothing has yet driven the running client.

NEXT: the client must be restarted for a new addon to load. Then, in order: confirm the
strip is on screen and decodes from a real capture; bind and focus; one key press; walk to
a node; then the 1–12 slice — accept, kill, turn in.

NOT NEXT, deliberately: more teacher/GLM/distill/promote, a second class, the Horde spine,
extra vision heads, ten-client orchestration. None of it until one character has accepted
a quest, killed, and turned it in.

## 2026-09-20 — first contact: the loop is closed
DID: the addon loaded on the real client and the whole chain was exercised against it.

**Perception works live.** A 1600x900 capture decodes the strip at 10 Hz — schema 1, seq
advancing, level 3, hp 1.00 of 104, 13 free bags, no target, out of combat, at map
(0.476, 0.423), which is Northshire a few yards from Marshal McBride. The frame is kept
as `tests/fixtures/live-northshire-1600x900.npy`.

Getting there needed one real fix. `locate()` reported the strip absent while it was on
screen, painting correctly and decoding perfectly when sampled by hand: a WoW screen holds
**3,798 cyan pixels** against the marker's 196, and ranking candidates by area put five
57x7 slivers of interface ahead of the real marker, which a six-candidate cut then dropped.
Filter on shape before size — a marker is a square filled cell and interface text is not.

**Input works live, and so does the guard.** First attempt refused to press anything
because `SetForegroundWindow` had failed and the window was not focused — exactly right,
and the alternative is typing into somebody's chat. With `AttachThreadInput` the window
raises, and then: `tap("space")` → `flags.falling` true across seven consecutive paints.
Input reached the game, the game changed, the addon painted it, the decoder read it back.
No vision involved anywhere in that loop.

**There is no facing API in this client** (V17, superseded by V29 — the minimap arrow is a `Model` and `Model:GetFacing()` reads it). `GetPlayerFacing` is 3.0+, so at the time heading had
to be measured from movement rather than read. Closed-loop turning needs no absolute
facing at all, which is the better shape anyway.

NOW: capture, decode and input are each verified against the live client and against each
other, not in isolation. 402 tests.

NEXT: movement. Closed-loop turn-and-walk to a node, measured on this terrain — turn rate,
arrival tolerance, and what stuck actually looks like are all things to measure rather
than design. Then vision heads for loot/gossip/quest frames, a combat profile for this
character, and the slice.

## 2026-09-20 — TRAVEL_TO walks 60 yards; McBride is behind a wall
DID: closed-loop travel, measured on live Northshire dirt rather than designed.

**Measured, not chosen:** walk 0.00227 map units/s; turn ~134 deg/s seed, converging to
60–100 deg/s in practice; heading spread 0.2 degrees over a 1.6 s sample; capture and
decode at 19–20 Hz; per-sample movement 0.19 yards against a quantisation floor of 0.12.

**Open ground works.** 59.7 yards to a known-walkable spot: **arrived**, 4.9 yards
remaining, 29.2 s, 44 turns, 2 stuck events (both freed by jump-forward), 2 detours.

**McBride does not, and that is the real finding.** The node is his spawn coordinate and
Northshire Abbey is between us and it. Best run reached 16.0 yards and then could not get
round. The loop now says so in as many words — *"7 detours did not get around it (closest
16.0 yards); this node needs a recorded route"* — which is the honest failure, not a bug.
`DECISIONS.md` V5 already says the DB knows where things are and not how to walk there;
this is the first time that bill came due.

Five things the live client corrected, none of which reading would have caught:
- **A and D turn; they do not strafe.** Q/E strafe. An unstick built on "strafe with D"
  turned in place and reported a failed recovery. Measured: forward 0.00 yards, back 3.91,
  turn-left 0.00, turn-right 0.00, **jump-forward 3.39** — so jump-forward leads the
  recovery order now.
- **A single torn frame is not a lost position.** `_unstick` skipped an attempt whenever
  its "before" read came back None, so one bad decode skipped *all five* attempts in about
  no time and reported failure having tried nothing. 3.7 s = 1.5 s detect + 0 s of doing
  nothing.
- **Measuring and moving are the same activity.** The first loop stopped to take a heading
  before and after each turn, dragging the character nine yards per correction: 11 turns,
  17 stuck events, timeout. Forward is now held throughout and turns are pulsed into it.
- **Map space is stretched 1.5:1**, so angles taken in map fractions are wrong by up to
  eleven degrees and the first turn-rate measurement was contaminated by it. Everything is
  in yards now.
- **Connectivity is the wrong primitive for finding the strip.** A marker merges with a
  same-coloured payload neighbour: once into five slivers of UI ranked above it by area,
  once into a 46x48 sprawl at 0.56 fill that a solidity filter discarded with the marker
  sitting in the middle of it. Detection is from **row runs** now, which cannot merge.

NOW: 412 tests. Travel works; one node needs a route.

NEXT: this is the point the plan predicted — recorded routes. The graph supplies
destinations; the paths between them have to be walked once. Everything else on the NOT
NEXT list stays there.

## 2026-09-20 — arrived at Marshal McBride
DID: the planner `Travel` was missing. Not vision, not more detour heuristics.

**`tools/jevpath`** — a standalone Detour sidecar over the server's own mmaps (V19). The
tiles are how CMaNGOS walks its own NPCs around Northshire Abbey, already extracted,
already compiled as `libDetour.a`. 513 tiles for map 0, 2.2 s and 577 MB to load,
microseconds to query, so `MmapQuery` keeps one process per map.

**`jev/guide/path.py`** — `PathQuery(map_id, start, end) -> Path`, with `MmapQuery` first
and `RecordedQuery` second as a seam with nothing behind it yet. `Travel.follow(path)` is
sequencing only; the follower is unchanged and still knows nothing about geometry.

**Live: arrived.** Courtyard to McBride, the leg that defeated straight-line travel nine
times, now routes around the Abbey: 37.2 s, 4.8 yards remaining — inside interaction
range — 50 turns, 3 stuck events all freed by jump-forward, 3 detours.

Three things worth keeping:
- **`-DDT_POLYREF64` is mandatory.** CMaNGOS builds Detour with 64-bit polyrefs, so every
  symbol fails to link with a signature that differs only in `unsigned int` versus
  `unsigned long`.
- **The Detour axes are not the game's**: `detour = {world.y, world.z, world.x}`, read out
  of `PathFinder.cpp` rather than remembered.
- **Tight arrival on intermediate waypoints** (V20). The loose radius that seems obviously
  right skipped most of the route and cut the corner back into the wall.

NOW: 423 tests. Travel plans and follows. Vision, combat and the teacher are still
untouched, deliberately.

NEXT: the rest of the slice — loot/gossip templates, one combat profile, accept/kill/turn-in.

## 2026-09-20 — the weave was the controller
DID: one controller fix, no new heuristics, planner untouched.

The follower was arguing with its own heading estimate. A pulse arcs the character, and
the 0.6 s heading window included the arc, so the reading overshot, the error flipped
sign, and the next tick corrected back. An oscillator, not a router.

Three changes, all in `to()`:
- **A heading is only taken from unturned motion** — samples from before a pulse ended are
  excluded, and a partial window gives no answer at all rather than a partial arc.
- **Cruise deadband 22 degrees**, tightening to 10 on approach. Ten is for docking; with
  0.12-yard quantisation and 1.5 yards of travel behind a heading, it is inside the noise
  on a long leg and every tick finds a reason to twitch.
- **No `_detour` on a planned leg** — a blocked leg re-queries the mesh from where the
  character actually is. Unstick still runs.

Live, courtyard to McBride, same three-point path:

| | before | after |
|---|---|---|
| elapsed | 37.2 s | **11.6 s** |
| turns | 50 | **8** |
| stuck events | 3 | **0** |
| detours | 3 | **0** |

Look-ahead steering along the polyline was specified as optional and is **not built** —
the weave is gone without it, and it would be cosmetics now.

NOW: 428 tests. Travel plans, follows, and walks straight.

NEXT: the slice, and nothing else. Interact McBride, accept, walk to the kobolds, combat
profile, turn in.

## 2026-09-20 — the playhead can finally see the quest log
DID: the slice was blocked on something I had assumed worked.

**The radio was not painting quests.** It carried `log_hash` and one *watched* quest, and
nothing in 2.4.3 sets a watch — so `watched_id` and all six objective fields read `None`
on a live client, and `to_state` was collapsing that to `()`, an empty log. The tracker
would have concluded a quest was missing from a log it never saw, and `QUEST_MISSING`
would have skipped a good step. Schema 2 cycles one log entry per paint instead (V23).

Live, after `/console reloadui`: `schema 2`, `count 0`, `State.quests -> ()` — a
**positively empty** log on a fresh paladin, which is exactly what puts the playhead on
`graph.entry` rather than on whoever is standing nearby.

`graph.entry` was already right: `alli_human_1_12_783_a_threat_within_accept`, **Deputy
Willem** at (-8933.5, -136.5, 83.4), because `PrevQuestId 783 -> 7` put him ahead of
McBride in the topological sort. The generator was correct; I had hand-picked
`--to-npc 197` and walked to the wrong NPC. `nodes[0]` being a grind rib is array order.

Three other things, all silent failures:
- **`/` was not in the key table**, so `slash("/reload")` typed `reload`. The addon was
  never reloaded and the character said "reload" out loud in Northshire (V24).
- **`/reload` does not work in this build; `/console reloadui` does** — which is what the
  V1 addon's own TOC comment said.
- **Quest 7961 sits at spine position two with no giver in the database at all.** At the
  default timeout that is eight minutes of a fresh character standing still before the
  escape edge fires. Unpositioned nodes are now `skippable` with a five-second timeout.
- `-`, `=` and the rest of the slash-command punctuation added to the key table. Action
  slots 11 and 12 are where a fresh character's food and water sit.

Paladin, read not remembered: `world_playercreateinfo_action` gives the default bar for a
Human Paladin — `1` Attack, `2` Seal of Righteousness, `3` Holy Light, `-` water, `=` food.

NOW: 434 tests. The bot can read its own quest log.

NEXT: the slice, from the entry node. Empty log -> Willem -> accept -> McBride -> turn in.

## 2026-09-20 — login works unattended; interact does not yet
DID: the client disconnected with nobody at the keyboard, so the bot now logs itself in.

**`jev/clients/session.py`** — four stages, three read from the frame and one honest
"elsewhere". `IN_WORLD` is the only certain one, because the addon only paints in the
world. Login and character select both show an interface-red plate and are told apart by
*where* it is: Enter World measured (98, 23, 1) while the login button's position on that
same frame was (80, 73, 66). Verified live, login through to Testvii in the courtyard.

It closed the client twice before working, both times for one reason, now a rule:
**nothing is pressed at a screen that has not been measured.** The first version pressed
Escape and Enter at anything it did not recognise — twenty-five presses found the Quit
button. The second pressed Escape once, inside the credential step, to dismiss a dialog
that was not there; at the login screen with no dialog, Escape opens the *quit* prompt and
the Enter that submits credentials confirms it. An unrecognised screen now stops the run
and keeps the frame, which is exactly how the character-select stage came to be measured.

**`jev/perceive/units.py`** — one correction worth keeping. Ring aspect was a tight
discriminator and should never have been one: a selection ring is a circle seen in
perspective, so its flatness is a function of camera pitch, which nothing here controls.
Measured 1.79 on one frame and 4.29 on another, both unambiguous rings, and the tight
range rejected the second while the unit stood centred in plain sight. **Fill is the
invariant** — hollow ring against solid bar — and aspect now only excludes shapes no
ellipse can be.

NOW: 476 tests. Login is unattended. The interact skill finds the unit, walks toward it
and loses it on the way in — and the last hour of that was iterated with a probe that
moves the character, which is why the position kept drifting between runs.

NEXT: interact, with a probe that does **not** reposition between attempts. The failure is
in `_walk_in` losing the sighting, and it needs to be watched from a fixed start rather
than chased.

## 2026-09-20 — the six-step skill fails closed; gossip is not open
DID: the skill now matches the specified shape, and fails closed rather than wandering.

    /target + name hash -> find() once -> one yaw -> hold W -> find() again -> click -> confirm

**The walk no longer consults the locator.** Re-finding every tick turned a snapshot into
a homing missile: the walk changes pitch, distance and which pixels the ring occupies, so
requiring a fresh sighting per frame fails on the first one that blinks — and the miss
tolerances that follow are the sprawl the module was rewritten to remove. Aim once, walk,
look again.

**It fails closed, and it no longer walks a hundred yards to do it.** A timeout-only bound
let one run walk the full twenty seconds. Bounded at twelve yards now, which is generous
for a unit that was visible and centred when it was aimed at.

**Stopping against a fence is not a reason to click.** `in_melee` cannot answer "can I
gossip" — duel range, about eleven yards — but it is reliable as a *negative*: not within
eleven yards means we certainly did not walk into the target. A live run stopped against a
wooden fence with Willem back in the courtyard and clicked the screen centre hopefully.
That is `APPROACH_FAILED` now.

**Locator: the nameplate is a different green from the ring.** Measured, the bar is
(72, 219, 48) and the ring (95, 200, 5), and a blue cap of 40 taken from the ring silently
excluded every bar. Widening it to 70 is safe because the *gap* discriminates — the bar
clears 171 where Northshire grass manages 78.

NOW: 487 tests. Gossip does **not** open. The skill finds the unit, yaws to it, walks
twelve yards and never collides — so walking at a centred unit is not walking at the unit,
and the camera/character explanation for that was tried and did not fix it.

Two physical constraints measured on the way, both real:
- At under four yards the player model occludes the selection ring, so the locator sees a
  solid sliver rather than a hollow ellipse and refuses — correctly.
- At nine yards the blind walk-in covers ground the planner routed around, and finds
  fences the mesh already knew about.

NEXT: why a centred, yawed unit is not reached by walking forward. That is one question,
and it wants instrumenting from a fixed pose rather than another skill change.

## 2026-09-20 — the quest frame opens
DID: split the skill from the walk, and it worked on the first live run.

    --- 3. stand on him, with the mesh ---
    --- 4. interact with Deputy Willem ---
      complete: 2 waypoints, 12.1 yards
      arrived, 2.9 yards left, 2 turns, 0 stuck
      saw it: ring (765,494) torso (765, 418)
      clicked=(781, 474)
      quest

`Interact` no longer walks. `_walk_in` and `_yaw_toward` are deleted and a test asserts
they stay deleted. The reason they had to go is that `W` follows **character** facing
while `find()` reports **camera** pixels, so a turn computed from a pixel error does not
point the body at anything — twelve yards in the wrong direction, and every fix was a
better pulse for a problem that was never the pulse.

The two halves belong apart and were already built:

    the mesh     how to stand there   jev.guide.path + Travel.follow
    the locator  where to click       jev.perceive.units.find

`approach` is injected, so the skill knows nothing about navmeshes. A mesh that cannot
stand on an NPC is a recorded-route question (V5), not a locator one.

It opened `quest` rather than `gossip`: Willem has exactly one quest, so a right-click
goes straight to the detail frame.

NOW: 483 tests. The frame is open and Accept is the next named gap — still no
deterministic way to press it, and a sweep over the quest frame is the same flail one step
later, so it is not here.

NEXT: Accept, then kill, then turn in.

---

## V25 — Accept, and the button says where it is

Quest 783 is in the log, accepted by the bot:

    --- 5. accept quest 783 ---
      advance button painted at (0.0450, 0.6940)
      clicked (88, 681)  -> accepted
      log now: (783,)

The frame opens where the client decides, not where a constant says, so the addon paints
where it put the button and the click reads it back. Two new fields on schema 3:

    ui.advance_x   11 bits, fraction across UIParent
    ui.advance_y   11 bits, fraction up   UIParent

`ADVANCE_BUTTON(axis)` returns whichever of Accept / Complete / Continue is currently
shown, so one pair of fields covers accepting a quest, completing one, and stepping
through a multi-page gossip — the same button in three costumes, not three skills.

**Fractions of UIParent, not pixels.** The first version painted `GetCenter()` scaled by
the button's own effective scale and landed exactly 100 px high: a button's scale is not
the UI's scale, and mixing them is an error that is invisible until it is a miss. A
fraction has no units to get wrong, and the decoder multiplies it by the frame it actually
captured, so it survives a resolution change for free.

`jev/clients/advance.py` holds the caps the reviewer set:

    zero clicks       unless the radio says the frame is open
    one click         at the painted fraction times the captured window size
    one confirmation  quest 783 present in the **assembled** log, not one frame

The last line is the one that matters. The log paints one slot per cycle, so a single
frame showing 783 proves only that a slot was painted — the assembled log is the claim,
and `AdvanceQuestFrame` will not report success on anything less.

The grid is now exactly full: 62 fields, 408 payload bits, 36 cells in 12x4, 8 spare bits.
The next field costs a row.

### The step-back caught one before McBride did

The rule is to re-read the plan after every push, and it earned its keep. The docstring
claimed Accept, Complete and Continue were one intent; `run()` pressed **once** and
confirmed `quest_id in ids`. Both halves are wrong for a turn-in, which needs two presses
— Continue moves the progress page to the reward page, Complete Quest closes it — and the
opposite confirmation, the quest *leaving* the log.

That would not have shown up as a bug. It would have shown up as "the turn-in skill",
which is precisely the per-case module the global-engine rule exists to prevent. So the
cap became the goal rather than a count:

    Goal.HELD      the quest is in the log      accepting
    Goal.CLEARED   the quest is gone from it    turning in

and `run` loops, re-reading the radio before **every** press. That is not the sweep the
caps were written against: each press is justified by a button the client is painting at
that moment, the loop stops the instant the log reaches the goal, and the frame closing is
a terminal success rather than `NO_FRAME`. Accept still costs exactly one click.

`CLEARED` is the dangerous direction and gets the same partial-cycle rule: mid-cycle the
strip has painted no slots, so every quest looks absent, and a turn-in would confirm on a
log nobody finished reading.

Named as **not** covered, rather than discovered later: a reward page offering a *choice*
of items. Complete Quest is pressable there and does nothing until an item is picked, so
this fails honestly with the log unchanged. Choosing is a decision, not a button, and
multi-option gossip is the same shape — a list.

Four ruff findings that predated this went with it, including an `objectives` tuple in
`radio_frame` orphaned when the log moved to `QuestLog`.

NOW: 501 tests, ruff clean. Willem gives 783 unattended, start to finish: login, plan,
walk, click, accept.

NEXT: McBride turn-in on the same two skills, then Echo Ridge.

---

## V26 — the playhead drives, and the distance was lying

`probe_slice` named the NPC and the quest on the command line. That was scaffolding for
the first accept, and it hid the fact that nothing was reading the chain — the graph had
Willem-accept -> McBride-turnin in it the whole time.

`Tracker.resume(graph, state)` walks from the entry and stops at the first step the world
does not already satisfy, reusing `enter`/`tick` rather than comparing afresh. That reuse
is the point: a turn-in is only complete if the quest was *seen* in the log first, and a
second rule written at the call site forgets that and walks past every turn-in in the
guide. Two seams it needed — `QuestLog.complete` made public, and `to_state(quests=)`, so
the assembled log reaches the tracker that `to_state` is right to refuse to invent.

### "3.1 yards left" was 46 yards

The first live turn-in attempt reported `stuck, 3.1 yards left`. It was 46 yards from
Marshal McBride. `follow()` was returning `remaining_yards` measured against the **leg it
happened to be on**, and 3.1 yards was all that remained of a waypoint in the middle of
the courtyard.

`to()` measures the leg because that is what it is steering at. A caller of `follow()`
asks one question — did we get there — so `_short_by` measures `legs[-1]`. A wrong
distance is worse than no distance: it is the number you use to decide whether the
approach or the click is at fault, and it sent an hour at the wrong one.

### A node is a spawn point, and a unit is solid

`arrival_yards=3.0` encoded "stand on the point". A path planned to a unit ends *inside*
its collision capsule, so the last couple of yards are unwalkable by construction — and
asking for 3 got four stuck events against McBride himself, which is arrival reported as
failure. `GOSSIP_YARDS = 5.0` is what the requirement actually was: close enough to talk.
Every NPC in the game has this shape.

With both fixed, live:

    --- step 1: alli_human_1_12_783_a_threat_within_turnin ---
      quest_turnin quest 783 at Marshal McBride
      complete: 4 waypoints, 65.5 yards
      arrived, 4.6 yards left, 5 turns, 1 stuck — re-planned at leg 1
      gossip
      cleared: pressed [] -> no_button — frame open but no advance button painted

### The gap this leaves, named rather than patched

McBride opens **gossip**, not a quest frame: a list with one line, `A Threat Within`.
There is no Accept/Continue/Complete button to paint, so `AdvanceQuestFrame` correctly
refuses. Clicking the line needs the line's position *and its identity*, because a gossip
with two active quests has no "the" line.

The shape is already in the codebase twice: paint it, hash it, match it. The addon paints
each `GossipTitleButton`'s y-fraction and a 16-bit hash of its text, and the bot matches
against the quest title the graph already carries (`objectives: ["turn in A Threat
Within"]`). Identical to `target.name_id`, and global for every gossip, quest and vendor
in the game. It costs a fifth grid row — the 12x4 is exactly full — which is what rows
are for.

NOW: 501 tests, ruff clean. The bot walks itself to the right NPC for the right step and
opens him. Both quest frames it can read, it can advance.

NEXT: gossip line selection, then Echo Ridge kobolds.

---

## V27 — the bot stops talking, and the quest gets handed in

    --- step 1: alli_human_1_12_783_a_threat_within_turnin ---
      arrived, 3.4 yards left, 0 turns, 0 stuck
      gossip
      chose 'A Threat Within': chose at (219, 362)
      cleared: pressed [(112, 681)] -> done
      log now: ()

Accept and turn-in both work, start to finish, unattended. And nothing types.

### It said it out loud

`Interact` used to acquire with `/target <name>` typed into chat. With **Caps Lock on** —
machine state no part of the bot could see, and which outlives the client and this process
— `M` is sent as shift+m and the OS delivers `m`. Every letter inverted, and
`/target Marshal McBride` left the client as the public sentence
`?target mARSHAL mCbRIDE`.

Every keystroke reported success. There was nothing to detect, and `slash` compounded it
by **discarding `type_text`'s return value** and pressing Enter regardless — so a mangled
line was sent rather than discarded.

Three changes, in increasing order of how much they matter:

    type_text   clears Caps Lock, refuses on a held modifier
    slash       Escape rather than Enter when the line did not type cleanly
    Interact    does not type at all

The third is the only one that is a guarantee rather than a mitigation. A bot that can
talk is a bot that can say the wrong thing.

### Targeting by sight, confirmed after the fact

    left-click the nameplate     selects; a label is clickable at any range
    read the radio               `target.name_id` says who actually answered
    `units.find`                 ring and plate now bracket the model
    right-click the torso        the measured point, not a guess from the label

Identity is checked **between** the two clicks, so a wrong plate costs a selection rather
than an action. A nameplate bar is five pixels tall and at three yards it can sit under
the frame or off the top, so there is a fallback: right-click the middle of the screen
when standing on the spawn — identity-checked the same way, because arriving somewhere is
not evidence about who is there.

### The line a camera cannot read

A gossip is a list, and `ADVANCE_BUTTON`'s "whichever is showing" has no meaning for one.
Two quest hand-ins draw two lines identical in every respect a pixel can see. So schema 4
paints five lines as position **plus identity** — `fnv1a16` of the text, the same hash
that names a target — and `ChooseListLine` matches against the title the guide carries.
Two of the same name is `AMBIGUOUS` and refuses; a click there would be a coin toss that
opens the wrong quest.

`Node.title` is now a field. It was always known at generation time and was being
formatted into `"turn in A Threat Within"` and thrown away.

### The bug that would have waited weeks

The first live match failed: the line hashed to 64617, the title to 3064. Measured off the
strip rather than guessed — 27 characters, first byte `|`, last byte `r` — the client
draws `|cXXXXXXXXA Threat Within|r`.

The colour is the quest's **difficulty relative to the character's level**. Hashing the
raw text would have matched nothing today, and — had the colour happened to be baked in —
would have started failing weeks later on a quest that used to work, at whatever level the
quest turned green. `plain()` strips colours, textures and hyperlinks before hashing.

### Two smaller things

`gen_addon_fields.py --install` copies the addon into the client. The Lua is generated
from `fields.py` so the two cannot disagree, and then it was copied across by hand — which
puts the drift straight back, silently, as a checksum failure on a client running last
week's schema.

The grid is 12x5 now: 73 fields, 554 payload bits, 48 cells, 6 spare.

NOW: 524 tests, ruff clean. Accept and turn-in are one code path over two skills, driven
by the playhead, with no keyboard involved.

NEXT: Echo Ridge kobolds — the first step whose skill does not exist yet.

---

## V28 — the playhead remembers, and the loop runs on a quest nobody tuned it for

    --- step 1: alli_human_1_12_7_kobold_camp_cleanup_accept ---
      arrived, 5.0 yards left, 7 turns, 0 stuck
      gossip
      chose 'Kobold Camp Cleanup': chose at (219, 362)
      held: pressed [(88, 681)] -> done
      log now: (7,)

A different quest and a different gossip from the one the skills were built against, with
no code that knows about either.

### The log cannot say a quest is finished

Straight after handing in 783 the bot walked back to Deputy Willem and asked for it again.
`Tracker.resume` scans for the first step the world does not satisfy, and a quest **handed
in** and a quest **never taken** are both simply absent from the log. 2.4.3 cannot settle
it either: `GetQuestsCompleted` arrived in 3.0.

So the fact is carried rather than derived. `var/playhead.json` holds the completed quest
ids, written the moment a turn-in is witnessed, because that is the only moment the
information exists — the log forgets a quest the instant it is handed in.

A step id is remembered too, but it is the weaker half and it proved it: dropping two
unplaceable quests deleted the node it named, the position was lost, and the scan fell all
the way back to the entry. Quest ids are facts about the *character*, so they survive
regeneration, re-ordering, and a different guide entirely — and `load` keeps them across a
graph mismatch for exactly that reason.

### A quest with no giver blocks everything behind it

Waskily Wabbits (7961) is a Noblegarden quest whose giver spawns only during the event. It
sat between A Threat Within and the whole rest of Northshire, and the playhead stopped
there forever — not failing, just never satisfied.

The generator now drops a quest it cannot place, and says so. The guide should describe
what this server can actually do; a runtime skip would re-derive that judgement on every
pass and leave the dead node in the file to confuse the next reader. Two dropped, 132
nodes to 127.

NOW: 530 tests, ruff clean. Accept, walk, gossip and turn in are one path, driven by the
playhead, with no keyboard and no per-NPC code.

NEXT: Echo Ridge kobolds — `quest_objective` is the first node kind with no skill behind
it, and it needs combat.

---

## V29 — a ring is a colour, not a reaction

Combat needs to see a mob, and the rules for that were guesses. Measuring them corrected
the *model*, not just the numbers. (The V29 commit message says 556 tests; it is **535** —
written before the last run.)

The bot walked 160 yards to the kobold camp on its own and Tab-targeted one. The radio
reported `target.reaction` **hostile**. The client drew a bright **yellow** ring.

That is not a bug in either. WoW colours unfriendly and neutral identically, and a camera
cannot tell them apart. What it means is that the old names were a lie: finding a ring
never told you what a unit *was*. So the rules are named by colour — `GREEN`, `YELLOW`,
`RED` — and answer one question, *where is the unit on screen*. Identity comes from
`target.name_id` after the click, the same act-then-verify split that made `Interact` safe.
A ring matched by the "wrong" colour now costs nothing, because nothing believes it.

The rule *shape* was wrong too, not merely mis-thresholded: it named a single bright
channel, so yellow — R and G both high — was unrepresentable, and the `NEUTRAL` rule
written by symmetry demanded G >> R and would never have matched anything at all.

### The numbers are the argument

    friendly ring   Deputy Willem   ( 95, 200,   5)   min(G) - max(R,B) = 105
    friendly plate  Deputy Willem   ( 72, 219,  48)                     = 147
    yellow   ring   Kobold Vermin   (211, 173,   8)   min(R,G) - B      = 165
    yellow   plate  Kobold Vermin   (130, 117,   3)                     = 114
    Northshire dirt                 (104,  87,  20)                     =  67

Yellow's brightness floor is 105, not 150, and that is the whole lesson of the session in
one number: **the plate is much darker than the ring**, so a threshold set by the ring
silently excluded every nameplate. Dirt is not separated by brightness either — 87 against
the plate's 117 — it is separated by the gap.

### Three shape corrections, each a measured false positive

    minimap sun   a yellow disc passing every ring test, which outranked a real ring by
                  area; UI exclusion covered only the top-left and now covers the minimap,
                  our own strip and the action bars
    ring aspect   a ceiling of 12 let a nameplate *plus its name text* in at 11.4
    bar fill      0.8 came from Willem's flat 0.99; a kobold's bar is a gradient at 0.72

`find()` still returns `None` on the hostile frame, and that is the honest answer: that
kobold is far enough away that the client draws its name and no health bar, and without
the bar there is nothing to bracket the model against. The nearer plates belong to other
kobolds and are correctly not borrowed.

### One extraction

`jev/run/client.py` — window, readers, assembled log, and the single composed action,
*stand on a world point*. `probe_slice` was the only thing that had this and the
measurement probe needed it; copying ninety lines of wiring is how two clients start
disagreeing about what "read the strip" means. `probe_slice` drops from 291 lines to 191.

NOW: 535 tests, ruff clean. The bot can see a mob.

NEXT: fight one. `Tab` already selects and the radio already confirms what answered, so
the open questions are closing to melee and knowing when the thing is dead — not finding
it.

---

## V30 — it kills one, and then the trouble starts

    progress: 0/10
      kill 1: killed (pressed [1, 2, 2, 2, 2, 2, 2, 1, 2], closed 4, last hp 0.17)
    progress: 1/10

The objective counter moved. That number is the server's own tally, not an inference from
swings the bot thinks it landed, which is why it is the thing worth reporting.

Getting there corrected four guesses, and one of them cost an hour.

### Tab selects things you cannot fight

`Tab` was the obvious way to find a mob and it is the wrong one: it selects by distance in
the *world*, so it picks a kobold thirty yards off through a tent. The first live fight
spent ninety seconds pressing abilities at a unit with `in_melee` false, no sighting, and
full health throughout.

Nameplates instead, exactly as `Interact` does it — a plate on screen is by construction a
unit the client is drawing near enough to fight. `Tab` stays as the fallback for an
occluded plate. Identity still comes from `target.name_id` after the click.

### There is no facing API, so clicking is how you aim

`GetPlayerFacing` arrived in 3.0 and `pos.facing` read `None` on every live frame (superseded by V29: it paints off the minimap arrow). A
right-click on the model targets, **turns the character**, and starts auto-attack in one
action, and `W` then walks along that heading. So closing to melee is a right-click
followed by bursts of `W`, watching the target's **health** rather than `target.in_melee`
— which is `CheckInteractDistance` index 3, about eleven yards, while a paladin swings at
five.

### A frozen addon is indistinguishable from a pinned character

Both look like a position that never changes. A Lua error stopped the strip painting,
every read after that was the same stale image, and the follower duly concluded the
character was stuck — four times, on a character that was fine. An hour went into terrain
that was never the problem.

`radio_frame.read` has taken `prev_seq` and returned `SenseFault.STALE` since the
beginning. `jev/run/client.py`, which every probe actually goes through, never passed it.
It does now, and returns `None` once the sequence has held for four seconds, because every
caller already treats `None` as "cannot see".

### A wedged character can only leave along one heading

Every unstick attempt — jump-forward, back, both strafes — acts along the current facing,
and the recovery only ever tried one. A ghost pinned against a tree survived two full
corpse runs, a relog, and every attempt at its original heading; it came free on the
**third** heading, on jump-forward. The same run then completed 207 yards to the corpse
with **0 stuck events**.

### Dying is a Tuesday

    dead=True ghost=False hp=0.0
    complete: 13 waypoints, 193.7 yards
    arrived, 5.9 yards left, 16 turns, 0 stuck
    alive

Read the corpse position *before* releasing — afterwards the ghost is at the graveyard and
2.4.3 cannot say where the body is, and a character left dead long enough auto-releases
and loses it for good. The popup button is the existing `ui.advance_x/y`: "Release Spirit"
and "Accept" are the same intent, so there is no second mechanism to keep working.

### Three ways to stop dying, all measured

    isolated plates   a pull in the middle of a camp killed it twice; a plate with no
                      neighbour is the best evidence available that a mob has none either
    health guards     do not *start* a fight below 55%, break one off below 30% — but the
                      guard is about picking fights, not surviving one already under way,
                      or the character stands at 49% declining to swing back
    self-defence      in combat the name filter comes off; a Kobold **Worker** beat this
                      character to 27% while every attempt refused to fight anything but a
                      Kobold Vermin and reported "not visible" twenty times running

One bug worth naming: the isolated-plate ordering silently did nothing, because
`list.sort` empties the list while computing keys — so the key function read an empty list,
every plate looked isolated, and the ordering collapsed back to plain centrality.

NOW: 555 tests, ruff clean. One kobold down, nine to go, and the character is alive.

### V31 — three engines, and the profiles stop being hand-written

The review's brief: hunt the radius, food is a vendor node, healing is a role. Two of the
three are in.

**Profiles are generated from the world database.** `world_playercreateinfo_action` is
what the game itself puts on a fresh bar, and each button's role is derived from what it
*does* — first effect 10 is a heal, 6 applies an aura, 78 is melee auto-attack — while a
consumable's aura says what it restores.

    Darnassian Bleu          aura 84   food    slot 12
    Refreshing Spring Water  aura 85   drink   slot 11

Both are item class 0, subclass 5, and neither says which it is in its name. `Rest` had a
constant saying food was slot 11, so it pressed the water and reported the character was
out of food with a wheel of cheese in the bar.

`every_s` is the spell's own duration less a margin: Seal of Righteousness is
`DurationIndex` 9, thirty seconds. 52 race/class profiles, none of them typed in.

**Healing is a role.** No `if paladin`, no `PaladinHeal.py`: the engine asks for a row with
`role=heal` and presses it if the bars say it is ready. A warrior is the same list with one
fewer row. Mana is checked against the spell's own cost through `vitals.power_max`, and
out of mana falls through to swinging — there is no drinking inside a fight. A heal is
confirmed by **health rising or the slot going unready**, never by having tapped the key,
because a press the client ignored is indistinguishable from one that worked and what it
hides is a picker predicate that never fires.

The data also caught a live bug: slot 1 is spell 6603, which is a **toggle**. Pressing it
while already swinging *stops* the swing, and the first rotation pressed
`[1, 2, 2, ..., 1, 2, ...]` — turning the character's attack on and off all fight.

**Hunt is the disk, not the pin.** `jev/run/hunt.py` walks stations on the radius the node
already carries, looks, fights, and moves on after two empty looks. Every kill quest in
the game is that loop; there is no camp-shaped special case and no second pathfinder. A
station it cannot stand on is not a dead end, which is already earning its keep — the
first live run could not reach the centre and moved out to the ring by itself.

### V32 — a ghost could not see its own strip, and the server was kicking us

The hunt ran and reported `unreachable: 1/10 after 0 kills over 9 stations`. Neither
number was about hunting.

**The server was kicking the session every ten to twenty minutes.**

    WARDEN: Account - 205 get opcode 00 - Load module failed or module is missing
    WARDEN: Account 205 ip 172.30.112.1 timeout

Warden cannot load its module into this client, waits thirty seconds, and drops the
connection. Three disconnects in one afternoon, and the one that ended the hunt. Disabled
on the dev server, with the timeout raised as well because this build initialises Warden
regardless of `Warden.Enable`.

**And a ghost is blind.** Dying applies a full-screen desaturation shader that turns the
world blue-green. The marker masks were hue margins and nothing else, and Elwynn ground
reads `(100, 140, 153)`, which clears both of them comfortably. On a live ghost frame that
put **293,555 pixels** in the cyan mask; the candidate search drowned and `locate`
returned `None` — while both markers sat on screen pixel-exact and all ten calibration
swatches matched to the byte.

So the bot went blind exactly when it was a ghost and needed to find its corpse.

The fix is a saturation test, and deliberately a **ratio** rather than a brightness floor.
The first attempt was a floor at 150 and it broke a test immediately: this decoder already
promises to read a capture at a gain of 0.25, where a marker painted `(0, 255, 255)`
arrives as `(0, 64, 64)`. Gain scales every channel together, so the *shape* of a colour
survives it and its brightness does not. A marker's low channel is 0; the shaded world's
is two thirds of its high ones. Half is a clean gap on both sides.

The new fixture keeps a slab of the shaded world in it on purpose, and the row-run premise
test measures against that frame now — the other live fixture is a strip-only crop with no
scenery left to be confused by.

### V33 — it walked at the kobold and never swung

With a working client and the server no longer kicking, the hunt finally produced clean
evidence, and it was damning:

    unreachable (1/10) pressed [] closed 8 — closed 8 times and landed nothing
    losing      (1/10) pressed [] closed 0 — broke off at 29% health

`pressed []`. It walked at a kobold, and later stood being beaten to 29% health, and never
once pressed anything at either.

Closing was a **gate in front of the fight**: walk until the target takes damage, *then*
start the rotation. Damage comes from swinging, swinging is the rotation, and the rotation
was behind the gate. Nothing could ever open it.

That is the same shape as a heal gated on the pull rather than on health — a predicate
hung on the wrong fact — except it was the attack, which is why the character could not
even defend itself.

`close_in()` is gone. The loop reads, rotates, and takes one burst of `W` when nothing is
landing yet, so the character walks and swings at the same time. Not while casting:
movement cancels a cast, and the only thing being cast is the heal keeping it alive.
`UNREACHABLE` now means out of bursts **and** nothing pressed **and** nothing landed —
an answer, rather than a consequence of never having tried.

## GATE A IS DONE

Quest 7, Kobold Camp Cleanup, start to finish and unattended: accepted from Marshal
McBride, ten Kobold Vermin killed at Echo Ridge, turned back in. Same engine that took
and returned 783.

    chose 'Kobold Camp Cleanup': chose at (219, 362)
    cleared: pressed [(112, 681)] -> done
    log now: ()

The final run:

    level  o0_have  killed  died  heals_ooc   heals_ic  NO_FOOD  where
    2      10/10    2       0     4/4 landed  0/2       no       Echo Ridge camp

Both kills at one station, no deaths, and the playhead has moved itself on to
`alli_human_1_12_5261_eagan_peltskinner_accept` — Eagan Peltskinner, from Deputy Willem.
Completed quests remembered: 7 and 783.

### What actually fixed it

Five bugs, in the order they were found, each with a trace. None of them was the
character being undergeared, and the operator was right to refuse the grind.

**The heal was never called out of combat.** Only the in-combat line existed, at 40%,
which a fight rarely reaches before it ends. And in combat Holy Light cannot complete: it
is a 2.5 second cast and pushback stops it — `heals 0/4`, `0/5`, `0/5` across three runs.
Out of combat it completes every time. `top up: 4/4 landed`.

**It was topped up after the wrong things.** The heal hung off the fight's *outcome*, so a
`timeout` fell through and the next mob was pulled at whatever health the last fight left
— one run logged `top up 0` and died without a kill. It belongs before the next plate, so
`_ready_to_pull` is the whole contract in one place: in combat nothing to decide, else
heal, then food, then refuse.

**The fight was gated behind its own output.** `close_in()` walked until the target took
damage and only then started the rotation — but damage comes from swinging, and swinging
was behind the gate. Live: `pressed []` while being beaten to 29%.

**It walked past them.** Closing ended only when health came off the target, so a slightly
wrong facing meant walking through the kobold and out the other side, still holding `W`.
Then the fix for that used `target.in_melee` to *stop* — which is `CheckInteractDistance`
index 3, about eleven yards, where a swing needs five — and the character parked three
quarters of the way there and stood still. It now shortens the stride instead of ending
it.

**It would not turn.** `engage()` gave up whenever `find()` returned `None`, and `find` is
right to refuse a ring with no nameplate above it. But refusing to *turn* left the
character facing the wrong way with the target in plain sight. A unit stands on its own
ring, so a click just above the ring lands on the model. Both final kills used it:
`no nameplate; aimed just above the ring to face it`.

### Named gaps, not worked around

    wedging            a character wedged in world geometry is freed by sweeping headings
                       and jumping. `Travel._unstick` sweeps four headings and gives up
                       where the same sweep by hand frees it on the first. Every corpse
                       run and the McBride turn-in needed manual passes.
    focus REFUSED      `Travel` correctly reports "the game window lost focus; nothing was
                       pressed" and `Client.approach` retries once. One `win32.focus()`
                       call is not enough — a run lost twelve stations to it.
    distant selection  `acquire` can fall through to Tab and pick a unit far enough away
                       that the client draws its name and **no health bar at all**. Not a
                       threshold: the bar is absent, not dim. Measured, swept 100 to 70,
                       and deliberately not tuned.
    in-combat heal     unusable at this level and capped at two attempts rather than
                       spammed. That is the game, not a predicate.
    idle disconnect    the server drops an idle session; Warden was disabled separately
                       and is no longer the cause.
    NPCs wander        a node is a **spawn point**, and the NPC may not be on it. Two runs
                       at 5261 arrived 4.0 yards from Deputy Willem's node, found only
                       Marshal McBride's nameplate, and correctly refused to interact with
                       the wrong NPC - `tried 1 nameplate(s); none was 22283: [57507]`.
                       `Interact` has no step for "he is not here, look around", and the
                       centre-click fallback is gated on standing on the node, which is
                       true but does not help when the unit has walked off. Named rather
                       than patched: the fix is a search, and a search is a skill.
    interact has no    `Fight.engage` will now turn using a ring alone, because facing
    ring-only aim      only needs the model. `Interact` still refuses - it needs a torso
                       to land a right-click that opens a window, and a ring gives feet.
                       Live: `not_visible - selected the right unit, but no ring and plate
                       to aim at` on Deputy Willem. Two skills, one primitive, different
                       precision requirements; not obviously one fix.

NOW: 608 tests, ruff clean. Gate A done.

NEXT: the playhead is already on 5261. Nothing here needs a new skill to attempt it.

---

## THE RUNTIME RECORDS, AND PLAYS WITHOUT A HUMAN

`jev/learn/episode.py` had a complete store from the beginning and nothing ever called
it, so the first successful slice — accept, ten kills, turn-in — was recorded nowhere;
a run can be repeated but its corpus cannot be recovered, so that was the only
irreversible miss on the board. `jev/run/journal.py` now puts it on the loop that
actually plays: ticks with full state, `situation_key` and an honest `armed_by: tracker`,
plus a skill row per `FIGHT`, `INTERACT`, `CHOOSE_LINE` and `ADVANCE_*`. `--steps 1` is
gone as a mode — the loop resumes, arms, records and moves on until the clock runs out or
one node fails `--retries` times — and taking the window back after a `REFUSED` now backs
off in short doubling waits rather than twenty flat two-second ones. Running it
unattended for twenty minutes took **nine steps across three quests**: Eagan Peltskinner
accepted on the fourth attempt and turned in, Wolves Across the Border accepted, and its
objective attempted three times — **74 ticks and 72 skill outcomes on disk**, completed
quests now `[7, 783, 5261]`. Two bugs in that new wiring were caught by running it: a
splice dropped `if __name__ == "__main__"` so the probe printed nothing and exited zero,
and the first skill row claimed a duration of fifty-six years because the caller passed
`time.monotonic()` and the journal subtracted it from `time.time()`.

### Named gaps, still not worked around

    unstick            untouched on purpose. The hand sweep that frees a wedge runs right
                       after an explicit `focused()` call, so it may work because of focus
                       rather than extra headings, and a fifth heading will not beat that.
    interact ring-aim  `Fight.engage` turns using a ring alone; `Interact` refuses,
                       because an interact click needs a torso and a ring gives feet.
                       This is what made Willem take four attempts.
    loot               quest 33 wants Tough Wolf Meat. There is no loot skill, so the
                       objective cannot finish however many wolves die.
    wandering NPCs     a node is a spawn point and the NPC may be elsewhere.
    idle disconnect    the server drops an idle session; this cost two logins tonight. When nothing is in reach it stands still, so a
`quest_objective` needs to **search its own radius** rather than treat the spawn point as
a spot. And it is out of food — `Rest` says so honestly instead of pressing a blank
button, which makes the `vendor` node kinds the graph already carries the next real step.

## The evening the screen answered every question

Six recovery passes reported `still_ghost` while arriving within 2.5 yards of the guess
every time, which ruled out wedging and pointed at the guess. `GetCorpseMapPosition()`
exists in 2.4.3 — `recover.py` said it did not — so schema 6 paints `pos.corpse_mx/my`
and the first pass with it walked 199 yards and got up. The body was **240 yards** from
the node the probe had been guessing. Then a ghost wedged on a fence and re-planned into
the identical 199.0-yard path five times, because a re-plan is only new information if its
answer changes; when the fresh route still starts by walking into the same place, the leg
now goes to the wall heuristic once and the planner takes it back after. Same fence:
arrived, 7 turns, 2 stuck.

Three screenshots did what a night of logs could not. The first showed a
`Couldn't load Blizzard_TimeManager` box left over from a UI reload. The second showed a
level 2 paladin with **`Your skill in Unarmed has increased`** in the chat log and a red
border on the main-hand slot: the weapon was at zero durability, so every pull had been
fists at 4-5 damage, every fight ran the full 45 seconds, and every death took another
10% off everything else. `bags.durability_min` had been painting **0.0** all along and
nothing read it. `Fought.BROKEN` now refuses to start and says so. The third showed the
camera pitched at the character's feet from directly above — which is why `not_visible`
and "no ring and nameplate to click" were *true* every time they were reported. The bot
could stand 4.6 yards from a targeted merchant, with his nameplate on screen, and not
click him. Mouse-look is relative but it clamps, so `Camera.level` drags into the bottom
stop and comes back a measured 400 pixels; the same frame went from bare ground to three
merchants under an awning.

`Repair` is the engine that follows from that, and it is the other half of dying.
`MerchantRepairAllButton` joins the addon's `ADVANCE_BUTTONS`, so Repair All arrives as
`ui.advance_x/y` like Accept and Release Spirit do and no field was added. The generator
already emitted `StepKind.REPAIR` and kept one per zone — a lottery that sent a character
stood in Northshire Abbey to Goldshire, 556 yards past three repairers 108 yards away —
so it keeps six and the runtime picks the nearest. End to end, live: camera levelled,
walked to Dermot Johns, merchant frame opened, Repair All pressed, and
`too_poor - the worst item is still 0%; not enough money`. Honest, and the last word on
the evening: **27 copper**.

### Named gaps, still not worked around

    money              the character is broken *and* broke, and `Fought.BROKEN` makes
                       that a deadlock: no fight, so no loot, so no copper, so no repair.
                       The graph already carries `StepKind.VENDOR` nodes; a sell skill
                       needs item quality on the strip to know what is junk.
    broken means weapon `bags.durability_min` is the worst of every slot, so broken boots
                       would stop the bot fighting. Correct action either way — go and
                       repair — but a main-hand field is the honest version.
    corpse run spin    the first recovery pass burns its whole 180s at ~180 turns and
                       ~12 yards. The second arrives. Nothing here explains why yet.
    money in copper    `bags.money_silver` cannot see 27 copper, which is exactly the
                       range a repair decision at level 2 lives in.

## Audit: capability claims, and fields nothing reads

Two sweeps asked for after corpse position and facing both turned out to be wrong in the
same direction — a real API written off as absent.

**Claims of the form "2.4.3 has no X".** Five distinct ones, in DECISIONS, STATUS and
docstrings.

    GetPlayerFacing is 3.0         TRUE about the API, FALSE as written. The conclusion
                                   drawn from it - "there is no facing" - was wrong for
                                   four days. Superseded by V29; the five docstrings
                                   that repeated it are corrected.
    InteractUnit is 3.0            unverified in-game. Believed true.
    INTERACTTARGET is 3.0          unverified in-game. `GetBindingKey("INTERACTTARGET")`
                                   settles it in one line and has never been run.
    GetQuestsCompleted is 3.0      unverified in-game. Load-bearing: the playhead cannot
                                   tell a finished quest from an untaken one, and a
                                   restart re-offers a handed-in quest because of it.
    GetQuestLogQuestID absent      unverified in-game. Worked around via the hyperlink,
                                   which does work, so the cost of being wrong is low.
    /follow refuses NPCs           unverified in-game.

Every "unverified" above is one `/script` away and none has been run. That is the same
posture that produced the facing error, so they are listed rather than trusted.

**Radio fields with no reader.** 75 fields; 13 are read nowhere outside `perceive/` and
16 only by tests. Most are benign — `ui.list_*` is consumed through `list_lines()`, which
lives in `perceive/` by design. Three are not:

    quests.o1_*, quests.o2_*   painted, assembled into `QuestLogEntry.objectives`, and
                               then dropped: `progress()` reads `objectives[0]` and
                               nothing else. A quest with two counters - kill eight of
                               these *and* six of those - can never be seen to finish.
    ui.error_id                the client's own reason for a failure: out of range, not
                               facing, can't do that yet. Painted since schema 1, read
                               by nothing. Several of this week's mysteries would have
                               announced themselves.
    flags.swimming             nothing reacts to being in water.

`bags.durability_min` was on this list until tonight and cost an evening, which is the
argument for keeping the sweep rather than the finding.

## 2026-09-22 — shared live runtime and outcome pipeline, verified offline

DID: continued the audits in @thread:thr_qpngriby9g and @thread:thr_vgjejbyrey with the
client deliberately unopened. The confirmed body methods remain the execution layer.

- `jev.run.cli` and `Supervisor` now connect the real Client readers and LiveBody to
  `ClientRuntime`. The old probe entry point delegates to this composition. Tracking,
  coaching and recording have separate schedules; one worker owns all input. Exceptions,
  blindness, modal panels, death, travel interrupted by combat and catalog timeouts take
  named paths through cancellation and input release. Unimplemented capabilities and
  exhausted attempts stop explicitly. A timeout counts as a failed attempt; no-food
  during travel preparation cannot turn into an endless preemption loop.
- LiveBody reuses Travel/Detour, Interact, ChooseListLine, AdvanceQuestFrame, Fight, Hunt,
  Rest, Loot, Repair and Recover. Combat uses Fight's full acquisition/closing/rotation
  method. Repair selects the nearest same-map generated creature using world yards and
  remembers an unaffordable purse until observed money growth. Recovery separates
  release from corpse travel and retains an observed corpse position. No new camera,
  turning, locator or hunt-radius calibration was introduced.
- Guide schema 2 adds exact target name and creature/gameobject kind from the local
  server database. Regenerating the 141-node human graph preserved every existing node
  ID, coordinate, radius, quest ID, edge and timeout. Objective hints now request the
  composed hunt skill. Quest progress consumes all painted counters and respects the
  explicit completion flag. An unread quest log cannot complete a turn-in. Skips and
  timed-out rib rejoins cannot claim completed-step reward.
- Ticks carry client identity, guide, situation key, applied decision ID, held keys and
  shadow output (explicit abstention when no model exists). Skill results preserve the
  originating arm's ID, step, key and duration. Rejected/stale/blind/preempted teacher
  proposals cannot be attributed to the fallback; artifact-only replies remain recorded.
  The live CLI leaves teacher calls off and runs the scripted floor.
- `python -m jev.learn.grade <run-dir> --closed` derives +60-second outcome grades with
  applied-decision joins, observation coverage and run/client isolation. Incomplete
  windows, failed executions, deaths and long stuck intervals cannot produce positive
  examples. Unknown XP is not zero XP. Dataset joins include run identity and prefer
  current JSONL over stale parquet; synthetic/replayed states are excluded by default.
  Existing diagnostic runs were not rewritten or relabelled.
- HID tracks confirmed key/button ownership and permits releases through cancellation
  or focus loss. Planner startup/query pipe reads now enforce their configured timeout,
  propagate cooperative cancellation and dispose failed subprocesses. Tests use real
  local subprocesses for silent startup/query/restart and cancellation, plus the actual
  Detour sidecar and extracted mmaps for the established Northshire routes.
- The paint-only addon reports visible stock menu/options panels through existing
  `ui.modal`, and quest completion checks `isComplete == 1` (Lua zero is truthy).
  Radio schema remains 6. Lua → pixels → Python decoder tests cover these changes.

NOW: the responsive offline slice executes accept → kill ten → turn-in through the
shared runtime and worker, then produces three positive synthetic examples through the
grader/dataset. The default live dataset correctly contains none of them. Zero teacher
calls. This is integration evidence, not live success or a promoted policy.

Verification: full suite **733 passed in 78.96 s**, including real navmesh checks; a
subsequent saved-playhead compatibility fix passed the **46-test** runtime/episode/grade
group, including its new regression test. Ruff (`jev tools tests`) and `git diff --check`
are clean. Addon field regeneration leaves the wire layout unchanged. `--check` runs
without attaching capture or input. The existing `camera.py` edit and `look_tmp.py`
scratch file were left as found.

NEXT / named gaps: Windows capture/input scheduling and cooperative stops need live
acceptance when the operator chooses to open the client. The prior camera edit remains
unverified after the options-menu contamination; it was preserved, not recalibrated.
The updated addon must be installed/reloaded before live modal evidence is available.
The graph still places only the first objective target, so a later incomplete objective
stops explicitly. Gameobjects need their own confirmed locator. Training, hearth,
flight, bag selling/making space and restocking lack composed executors; `--check`
reports graph catalog and target gaps without attaching a client. Broken-and-broke
equipment is still a real resource deadlock. Facing remains unknown under V17/V29, and
the earlier corpse-run spin remains unexplained. Live outcome collection and measured
student promotion remain ahead; this work does not claim unattended 1–12 coverage.

## 2026-09-22 — keep the current loop running: service, focus and recovery handoffs

DID: continued within the existing runtime/body scope. No client opened, new live runs,
teacher/learner work, Fight rewrite, wander search or new unstick measurements.

- Long hunts now hand back to the shared service policy between pulls, **after looting
  the kill**. Full bags stop another pull; standalone travel can yield to service too.
  The body and coach share the same observed repair-purse context. An unaffordable
  repair no longer hides a simultaneous full-bag condition. Service requires an observed
  out-of-combat state. The current bag-service executor is still absent, so full bags
  stop with that named gap instead of continuing to kill with no space.
- A full bag with a persistent loot window now closes that observed window through the
  existing close method. Otherwise the high-priority open-loot rule could keep selecting
  loot and never reach service. Corpse aim, settle timing and empty-corpse success remain
  unchanged.
- Focus loss releases the worker before another game action. The supervisor can run the
  existing bounded focus backoff even when another window hides the radio, while it
  continues recording observations. That operation sends no game keys and is not
  reported as successful quest execution. Refused focus stops once with its reason;
  operator stop cancels the backoff. No new unstick heading or facing inference.
- A guide predicate succeeding no longer cancels its own body composition before its
  final confirmation/loot finishes. Replacement input still waits for release. Hard
  preempts, failed guide transitions and skill deadlines still apply. At the terminal
  guide step, the run waits for the worker's result instead of cutting it off immediately.
- Measured death, blindness, interrupted rest and service handoffs are interruptions,
  rather than failed quest attempts. A death can therefore enter release/corpse recovery
  even with a one-attempt quest retry budget, and return to the same step afterward.

NOW: offline integration executes the real Hunt → LiveBody → Supervisor → ClientRuntime
composition through fight → loot → repair → fight → loot, retaining the same objective
and recording the interrupted hunt, successful repair and completed hunt separately.
Another threaded test executes quest → death → release → corpse run → same quest without
consuming its retry budget. Hidden-radio focus tests prove continued recording, no game
skill while unfocused, bounded refusal, and cancellation. Existing live constants and
the pre-existing camera/scratch edits remain untouched.

Review correction: current `Fight.run` marks broken gear and still allows fighting; it
is a preference, not a hard veto. Earlier notes describing a broken-gear veto/deadlock
do not describe the current Fight code. This continuation preserves that behavior and
the measured purse gate, so a refused repair does not force repeated merchant trips.

NEXT: live acceptance remains pending under the instruction to keep the client closed.
Safe bag selling through the existing vendor/Interact composition remains unimplemented;
no item-quality evidence, selling clicks or vendor success have been invented. The named
wandering-NPC, idle-disconnect and in-combat Holy Light measurements remain open.

Verification: **751 tests passed in 79.56 s**, including the real navmesh checks and all
new threaded continuity/focus cases. Ruff (`jev tools tests`) and `git diff --check` pass.

## 2026-09-22 — build the unattended composition and evidence-gated learning

DID: implemented the requested inventory, route, supervision, background learning,
teacher connection and promotion/rollback work. The client remained closed. No addon
installation, external teacher call, live soak or live promotion was performed.

- Schema 7 paints exact money, inventory slots, supply counts, merchant offers/pages,
  stock button coordinates and unique vendor gossip identity. Schema 6 captures still
  decode with new observations unknown. The vendor uses Interact and the existing list
  chooser, sells only generated non-quest junk, buys exact starting food/drink, and
  requires inventory plus copper evidence for transactions. No equipment destruction,
  guessed buttons or action-bar changes. Unaffordable supplies are bounded by observed
  purse changes. Full bags yield service between pulls after looting.
- Schema 3 guide generation retains each objective's source requirement, target and
  verified radio counter. Hunt selects the current incomplete target; the tracker follows
  that target's camp and requires explicit overall completion for structured quests.
  Delivery facts and dependency groups are generated from the local DB. The explicit
  supported derivative contains 99 nodes / 31 quest chains, with 14 quest chains and
  unsupported service branches excluded in a manifest. Exclusions never become rewards.
- Navigation declares its coordinate frame. Player and corpse observations from another
  region are converted through measured WorldMapArea bounds into the graph frame;
  actual region and raw fractions remain recorded. This fixes Elwynn/Stormwind boundary
  interpretation without changing the proven planner/follower. Unknown regions and other
  continents provide no usable position. New State rows declare schema 2; schema 1 corpus
  remains readable. Live guides lacking a declared frame are refused before attachment.
- Atomic playhead replacement, separate non-default client memories, a shared per-user
  input lease, cooperative SIGTERM/interrupt cleanup, and bounded reconnect through the
  measured Session composition. The watchdog measures quest/XP progress, not movement
  or retry/rib playhead churn. Its last permitted reconnect can finish before exhaustion.
  Unknown login screens receive no guessed input; lost account-field confirmation never
  sends a password.
- A periodic learner grades mature windows, splits whole runs, trains immutable versioned
  candidates, evaluates exact graph revisions and brackets, records distinct shadow model
  identity, and supports bounded canaries, evidence-gated promotion and rollback. Unknown
  life/XP, synthetic future windows, duplicate identities and mixed model control cannot
  manufacture positive credit. Death edges and earned tracker advances are counted once.
  Default activation requires independent windows, multiple runs and observed progress;
  full promotion additionally requires fresh model-attributed improvement.
- The optional teacher bridge uses the existing subscription transport off-thread, a
  persistent call budget and the current graph context. All artifact payloads survive
  refusal/staleness in decision records. The body contract verifies actions before arming.
  Cancellation kills and reaps the teacher subprocess. Proposals never install code.
  Optional model loading, registry writes and status persistence run off the supervisor
  thread. Setup failure leaves the scripted floor running; failed models are quarantined
  immediately and queued rollback is drained on shutdown.

NOW: the complete offline composition and operating commands are documented in
`docs/OPERATING.md`; vendor provenance and regeneration are in `docs/VENDOR.md`.
The all-flags `--check` reports 31 supported quests with no missing executor or target
inside that selected derivative, and performs no capture, input, credential read,
training or subscription call. An actual learner pass over all 28 existing run directories
reported no errors and zero eligible examples. A repeated unchanged pass grades zero
runs, publishes no candidate and performs no promotion; missing historical evidence
was not repaired by invented labels.

NEXT / named limits: install the new addon and perform Windows/live acceptance only when
the operator permits opening the client. Observe inventory service, reconnect, graph
continuity and input release during the soak before claiming unattended operation.
Wandering-NPC interaction, focus versus unstick, in-combat Holy Light timing, unsupported
gameobjects/exploration/training/travel services, and depleted-character supply access
remain named limitations. The pre-existing camera calibration edit and `look_tmp.py`
were preserved and excluded from this batch. Measured ongoing improvement remains a
future outcome, never an assumption of the software being wired.

Verification: **927 tests passed in 85.32 s**. Ruff (`jev tools tests`),
`git diff --check`, graph regeneration identity, real Lua addon execution and the
all-flags offline readiness check pass. Regenerated wire: 112 fields / 1035 payload
bits, 12×9 grid. All code verification remained offline.


## 2026-09-22 — Windows setup and supervised service acceptance

DID: the operator authorized opening the client and then requested one screenshot per
second during every live test. Installed the matching schema 7 addon (all five files,
including Supplies.lua), verified Windows learning dependencies with pip check, started
the existing local realm/world servers, and reconnected through the measured Session
composition. A bounded subscription probe returned a valid teacher reply. Sonnet was
requested but CLI usage metadata reported Haiku; transport access is confirmed, model
routing is not inferred from the requested alias.

- Camera stop 2000 / return 500 was repeated live at pointer speed 10 with acceleration
  disabled. Both returns matched; later one-second sequences show the temporary downward
  view during the clamp returning to the same pitch. The preserved camera edit is now
  measured. No navigation, closing-burst or combat-rotation constants were changed.
- `--screenshots` records lossless PNGs every second off the input thread with a timestamp
  manifest. The first four monitored tests recorded 173 images, no missing/skipped slots,
  and a maximum interval of 1.025 seconds. Sequences were reviewed as part of testing.
  `--stop-file` requests the existing cooperative shutdown; the live stop returned 130,
  and Win32 key-state reads confirmed all movement keys and both mouse buttons released.
  Review added startup coverage: screenshots begin before focus/reconnect, STOP is checked
  during login, and interrupted screenshot startup still joins its thread before capture
  handles close. Merchant blindness retains its existing PREEMPTED classification.
- Windows byte-range locks on the WSL UNC learning store reproduced EINVAL. Windows UNC
  checkouts now select a stable native AppData store, printed by the CLI; overrides remain
  explicit and locking remains intact. The native worker processed the actual corpus
  without errors. It has not manufactured eligible examples or promoted a model.
  The checkout identity preserves case-sensitive WSL directories; its corrected store is
  `foreverv2-3c309d13f4025150`. Existing live state was copied with file hashes checked,
  retaining the earlier store as a backup.
  A final native worker pass covered 34 actual runs with no errors or eligible examples.
  The standalone worker and live CLI share the same platform storage resolver and
  checkout root; launching the standalone worker cannot silently return to the UNC default.
- Merchant acquisition now includes measured dimmed green bars and excludes one-pixel
  borders. A confirmed plate anchors the fresh selected-target ring/plate pairing, so
  unrelated larger terrain does not displace the selected merchant. A measured 67x7 ring
  fits the shared aspect bound; ring-only torso guesses remain absent. The existing
  close-range centre fallback is reachable when the player occludes a correctly selected
  NPC's ring; a missing frame is BLIND and cannot activate that fallback. Nested merchant
  failures now retain their actual interaction cause in the run.
- Vendor coordinates are numeric at generation and load boundaries, matching the guide
  generator. All 2,266 vendor spawns were regenerated: numeric values, identities and
  stock are identical; Supplies.lua is byte-identical. Tests exercise SQLite TEXT
  coordinates, legacy JSON strings and actual catalog navigation/ranking.

LIVE EVIDENCE: run `20260922T130724-bfdc92` repaired durability 0 -> 100% for 33 copper
(1027 -> 994). Run `20260922T131407-487841` bought ten water for 46 copper (994 -> 948),
closed the merchant, and walked 76.7 yards to the wolf area with zero stuck events.
Its 209 valid radio ticks had a maximum gap of 0.603 seconds. Two wolf approaches then
landed no hits; the operator stop preserved quest 33 at 0/8, full health and repaired gear.
The failed hunt did not become a successful learning example. Wolf frames expose yellow
grass passing the ring mask and being paired with a real health bar. The actual ring is
sometimes fragmented below the measured component area, or flashes red while its plate
stays yellow. Separate brighter ring masking, closest-pair ranking, and requiring a ring
within the plate's horizontal span/width each still produced false clicks on recorded
frames. Those experimental changes were not applied. Wolf engagement remains a named
blocker; these service passes do not prove a soak.

Verification: **978 tests passed in 86.42 s**, Ruff (`jev tools tests`) and
`git diff --check` pass. The all-flags offline check reports 31 supported quest chains,
zero missing executors and zero unsupported targets within that explicit derivative.
Standalone Windows `python -m jev.learn.worker --once`, with no path overrides, also
processed the same 34 runs without errors, regrading unchanged data or promoting a model.
A final read-only client capture confirmed full health, no combat, 100% durability and
ten waters; Win32 reported all movement keys and both mouse buttons released.

## 2026-09-22 — Nested evidence and measured targeting boundary

DID: continued roadmap package 1 through shared recording/input components. Worker
dispatch now freezes arm/decision/controller/step attribution, and nested Hunt, Fight,
Interact, Loot, Rest, recovery and merchant observations go to `executions.jsonl`.
They create no extra strategic ticks, decisions or skill results. Recorder writes are
serialized; parquet includes the new stream and older recordings remain readable.
Worker completion follows cleanup even if recording fails. See
[execution evidence](docs/EXECUTION_EVIDENCE.md) for the remaining acceptance gaps.

- HID counts only events Windows accepts. Partial pointer movement stops before a click;
  camera calibration propagates failed drags/releases while preserving its measured
  2000/500 geometry. Every cleanup release is attempted despite device/recording errors,
  then failures or remaining owned inputs are surfaced to every caller.
- Schema 8 appends five paint-only cursor fields, preserving the schema 6/7 prefixes and
  the existing 12×9 grid. `Targeting.probe` only moves the pointer and classifies a fresh
  paint after arrival. Shared post-action freshness also serves diagnostic selection.
  It is deliberately not enabled as production body-click permission.
- The diagnostic CLI records bounded probes with a stop file, input lease and 1 Hz
  screenshots. Shared event capture adds explicit pre/post images under the same lock
  and manifest, rather than attributing the nearest periodic image to an action.
- The actual stock client uses `ScriptErrors` for `message()` and script errors;
  `MODAL_UP` had checked nonexistent `ScriptErrorsFrame`. The shared frame-name correction
  is supported by the installed FrameXML and real Lua painter tests. After deployment,
  a supervised reload verified `ui.modal=true` with the visible error dialog; reviewed
  dismissal returned it to false. No error-text-specific dismissal was added.

LIVE EVIDENCE: `captures/targeting/wolf-hover-1` proves the native nameplate and visible
body points can both report `cursor.has/is_target/world=true`. Another MATCH accompanied
a same-sequence image where the wolf had moved and the point was visibly on terrain;
the reason is unproven. `wolf-hover-2` adds an observed empty-world negative and UI
negative, but its other-wolf proposals missed, so same-name negative acceptance remains
unmeasured. These are ownership observations, not evidence of a hit, body click, empty
corpse or reliable engagement. No combat loop or learning promotion was run.

Across the eight bounded startup/diagnostic sessions, 102 images were retained: 70
periodic and 32 explicit event frames, with no missing/skipped slots and a maximum
periodic gap of 1.075 seconds. Screens were reviewed during testing; the initial
unrecognized startup screen and subsequent obstructing dialog stopped dependent work.
Final read-only verification found schema 8, full health, full durability, 948 copper,
no combat/death/ghost/modal, and all movement keys and both mouse buttons physically up.

The annotated [target corpus](docs/TARGET_LOCALIZATION_CORPUS.md) and
[hover measurements](docs/HOVER.md) retain the failed hypotheses. Returning every current
ring/plate pair can recover some frames, but others have no valid proposal because the
ring is fragmented or clipped. Reliable shared body localization remains the blocker;
neither a fixed plate drop nor hover equality alone closes it.

Verification: **1,119 tests passed**, including concurrent/nested recorder lifecycle,
actual primitive composition, old corpus/parquet compatibility, Lua wire execution,
historical schemas, partial input failure, cleanup failure, event capture and freshness.
Ruff (`jev tools tests`), generated-addon identity and `git diff --check` pass.

## 2026-09-22 — Shared targeting composition and retained camera geometry

DID: carried the earlier measured hover work into the shared `Targeting.click_selected`
path used by Fight, Interact and Loot. Fresh post-arrival hover, pointer position and
owned frame geometry govern button delivery. Native window binding checks both executable
identity and the game window class. Shared UI cleanup preserves unread/open-window
failures. Portable annotated corpus images and a separate evaluation set retain false
positive cases for repeatable offline checks.

Camera pitch calibration is retained across compatible calls and invalidated on actual
focus, geometry or reconnect changes. No replacement combat timing or navigation tuning
was introduced. The experimental no-damage cutoff was withdrawn; measured close/reaim
and navigation constants remain intact.

LIVE EVIDENCE BEFORE THE OPERATOR PAUSE: runs `20260922T192105-3d5fcf` and
`20260922T193609-c61a62` retained the one-second and event images. Calibration no longer
repeated on every attempt, but some grass hypotheses still passed targeting, and the
wolf approach produced no verified damage or wolf-meat progress. Quest 33 stayed 0/8.
This did not prove engagement, a soak or an eligible learned capability. The final
read-only check at that pause reported movement keys and mouse buttons released.

## 2026-09-22 — Visual teaching and gradual motor handover

DID: implemented the owner-confirmed architecture in `jev/play`, composed inside the
existing guide, supervisor and single input worker. The full contract and supervised
launcher are in [TEACHING_LOOP.md](docs/TEACHING_LOOP.md); architectural decision V38
supersedes the original whole-skill-only teacher restriction.

- Jev receives an owned screenshot, same-capture radio/state, configured key semantics,
  current objective and recent measured results. It proposes a bounded movement, turn,
  action-slot activation, grounded pointer/click, camera adjustment or existing skill.
  Fresh execution guards and cleanup apply to teacher and student alike. Camera work is
  optional; confirmed scripted routines and navigation constants remain available.
- Knowledge retrieval now includes the existing complete TBC 2.4.3 build 8606 world/DBC
  snapshot, in read-only mode with pinned provenance and hash. Bounded entity lookups
  cover quests, NPCs, items, spells, objects, loot, vendors, trainers and related spawns.
  Missing information and live binding/action-bar uncertainty remain explicit.
- Crash-readable records join requests, accepted actions, actual delivery, independent
  effects and completed episodes. Movement, model confidence and delivered clicks cannot
  manufacture combat, quest or learning success. Composed resupply verifies stable item
  counts; no-take loot remains non-fatal without positive credit; unknown power type
  cannot finish rest. Modal Escape is reachable during combat without bypassing death,
  guide failure or normal combat interruption.
- The continuous local learner fits visual-conditioned action/parameter examples only
  from verified useful episodes. Whole-run evaluation, novel-state abstention, shadow,
  bounded canaries, audits and rollback govern authority per capability. Promotion
  counts all teacher calls in complete episodes, including corrections in other
  capabilities, and requires maintained useful progress. New weights repeat the gates;
  no production model has been trained or promoted from fixtures.
- Teaching and adaptive CLI modes enable one-second screenshots and learning. The
  prepared native Windows launcher defaults to an offline check. Explicit continuous
  collection closes each real session before the next and preserves the playhead/store;
  failure or operator stop never auto-restarts. No client was attached or controlled
  during this implementation turn.

DEPLOYMENT EVIDENCE: native Windows checked every configured path, both saved binding
files, the full world snapshot and the launcher's actual offline entrypoint. It reports
31 supported quest chains and no missing executors. Native readiness reports
`ok=true`, `model_calls=0`, `game_input_executed=false`. The native Claude executable,
required flags and subscription login also pass deployment checks.

TRANSPORT LIMIT: automatic approval review rejected exporting a saved game image and
derived prompt to Claude because explicit export approval was not established. No game
image was sent. A separately approved synthetic-only probe and one diagnostic follow-up
used generated colored rectangles and public-example literals from a neutral directory.
The diagnostic returned HTTP 429: “You've hit your weekly limit · resets 4am
(Europe/London)”. No reset date is inferred; no actual served model/token usage or
successful vision round-trip was established. No further model calls were attempted.
See [the recorded transport evidence](docs/TEACHING_TRANSPORT.md).

NOW: implementation and native offline setup are complete. The first supervised live
acceptance remains approach → observed damage → death/loot → quest-counter progress,
followed by turn-in, sustained service/recovery and eventually measured student handover.
It requires available subscription capacity and resolution of the screenshot-export
approval block. Offline success is not evidence that the next wolf will be engaged.

VERIFICATION: **1,676 tests passed in 93.48 seconds** after the final composition
corrections. Ruff (`jev tools tests`) and `git diff --check` pass. Tests include actual
controller/executor composition with fake input and painted telemetry, continuous learner
handover/rollback, subprocess cleanup, the observed quota error, exact SQLite schema
queries and the modal/combat/loot/service boundaries. Native Windows offline checks passed
separately; no live test or game screenshot export was performed in this build turn.

## 2026-09-22 — GLM vision connected; gameplay export awaiting approval

DID: added an explicit GLM API transport at the owner's request. Provider selection is
shared by the normal launcher and readiness/smoke tool. Existing controls, outcome checks,
input ownership and learning gates are unchanged. The default `glm-4.6v-flash` is listed
as free in official pricing. API calls have bounded input/output sizes and deadlines,
cancel cleanly, never silently retry or change models, reject redirects and retain actual
served-model/token accounting. The supplied credential is only in ignored local dotenv
storage, protected by file permissions; it is absent from code, launch arguments and Git.

CONNECTION EVIDENCE: one native Windows request sent only a generated colored-shapes PNG
and synthetic literals to the official Z.AI API. It returned a valid typed observe reply
from `glm-4.6v-flash` in 2.233 seconds, identified the shapes and reported 1,437 input /
84 output tokens. No game state/image was included and no game action was executed. The
key's Z.AI endpoint compatibility is therefore measured, not assumed.

LOCAL LIVE CHECK: the disconnected client successfully reconnected using the existing
Session under an input lease, stop checkpoint and screenshot recorder. All 21 periodic
frames were visually reviewed; there were no missing/skipped frames and the maximum
interval was 1.019 seconds. Final telemetry: level 2, full health/mana, no combat/death,
13 free bag slots and full durability. The pre-existing disabled Blizzard_TimeManager
popup is visible and `ui.modal=true`. Independent Windows key-state reads found every
movement key and mouse button released. No combat or quest-progress test occurred.

BLOCKER: automatic approval review rejected the saved-game-image GLM smoke request before
execution because the private image/state export to Z.AI needs explicit approval. A
provider-specific question is pending. No gameplay data was sent to GLM; no workaround
was used. Gameplay testing can proceed through the configured launcher after approval.

VERIFICATION: **1,754 tests passed in 95.91 seconds**, including 108 focused provider,
factory, launcher and runtime tests. Ruff and `git diff --check` pass. Native Windows
deployment and the synthetic vision request pass independently.

## 2026-09-22 — Approved supervised attempt; tutor contract blocked gameplay

DID: the owner confirmed the full supervised GLM test, resolving the gameplay-export
approval block above. A saved gameplay frame produced a valid observation from
`glm-4.6v-flash` in 4.407 seconds (5,632 input / 114 output tokens). The configured
180-second live run `20260922T211913-c2c0e3` then stopped after about 10 seconds when its
first tutor reply failed validation. No gameplay action was accepted or executed.

MEASURED: all 12 live frames were visually reviewed (11 periodic plus one event); no
missing/skipped captures, maximum periodic gap 1.008 seconds. The pre-existing modal
remained open, HP/mana stayed full, and quest 33 remained 0/8. The episode is recorded as
aborted, progress zero and unverified. The motor registry contains no models or promoted
capabilities. Final read-only capture showed character selection; an independent Windows
key-state check found every key and mouse button released.

GLOBAL CORRECTIONS: modal action availability is now shared by tutor schema, local reply
validation and executor: observe or tap Escape. Tap durations and capability-purpose
semantics are explicit. Teacher prompts retain the complete PNG, game state and all
configured bindings while omitting the local student's image descriptor and deduplicating
source strings. Original evidence/fingerprints are unchanged. Comparable modal requests
fell from 28,080 to 15,877 input tokens. Rejected GLM replies now report allowlisted field
paths and validation codes without response text or invalid values.

BLOCKER: eight non-executing recorded-scene diagnostics/comparisons confirmed that the
free Flash model still supplied invalid reply fields (including `capability="key"` for
an otherwise valid Escape action). An explicitly selected `glm-4.6v` comparison returned
HTTP 429; the provider-limit cause is unknown. Native function-calling and enabled-thinking
experiments also failed validation and were not adopted into production. No coercion,
automatic retry or additional live input loop bypassed those failures. Claude was not
called. Details are in [TEACHING_TRANSPORT.md](docs/TEACHING_TRANSPORT.md).

NOW: the full gameplay acceptance did **not** complete. Approach, damage, death/loot,
quest progress and useful teaching remain unverified. The next requirement is a tutor
that passes the bounded reply contract on real observations; the bot remains stopped.

VERIFICATION: **1,777 tests passed in 96.20 seconds**. Ruff (`jev tools tests`) and
`git diff --check` pass. Tests cover modal schema/executor agreement, unchanged nonmodal
behavior, preservation of all configured controls, nonmutation of recordings, invalid
capability rejection and sanitized diagnostics. No fixture result is claimed as live
gameplay or learning success.

## 2026-09-23 — Facing, observed attack state, corpse hover and a tutor that can answer

DID: diagnosed why "we confirm the target but never walk up and attack" from the saved
live frames, fixed it in the shared owners, and rebuilt the tutor's reply contract so the
free GLM model can drive. No client input was sent in this build turn.

- **Root cause (V40/V41).** Run `20260922T192105-3d5fcf` frames 99-104: after a verified
  right-click the Attack slot flashes (auto-attack on) while the wolf stands two yards away
  at the character's side at full health. A right-click never turns the character, so Fight
  walked `W` down a heading nothing had set; the rotation then pressed the Attack toggle and
  switched the swing off. `Targeting.face_selected` now turns until the selected unit's own
  (bright) plate is on the centre line, measuring each pulse's actual rate; Fight faces before
  every stride, stops when the Attack action is in range, re-faces on a new "facing the wrong
  way" error or three and a half seconds without damage, and presses the toggle only when
  `bars.attacking` says it is off. The body right-click engagement path is gone.
- **Radio schema 9.** `bars.attacking`, `target.melee_range` and a 1.5 s held UI error with a
  counter; 14 bits inside the existing 12x9 grid. Schemas 6-8 still decode.
- **Corpses (V42).** `Targeting.click_corpse` hovers a bounded grid under the last plate the
  unit had alive, then the centre line, and clicks only where the client reports the selected
  unit dead. Hunt and the body pass Fight's last plate to Loot. `revalidate_corpse` is removed.
- **Tutor (V43).** `jev/play/tutor.py`: a menu of currently available actions built by the
  executor's own rules, a flat reply `{"observation_id","action",<params>,"why"}`, strict
  parsing (one JSON object, no duplicate keys, parameters only for their action), identities
  bound from the observation, and a readable prompt with goal, state, nameplate detections and
  measured results of recent actions. Observations now carry nameplate detections; the judge
  measures new `faced` and `attacking` effects. The GLM transport returns reply text and names
  the provider's numeric error code; 1302/1305 and 5xx carry a retry hint. The teacher retries
  transient failures with 2/4/6 s backoff inside the decision deadline, re-asks once quoting
  the rejection, and an unavailable tutor hands the objective to the guide's scripted routine.
  `FACE_TARGET` is a new body routine the tutor may call.
- **Tooling.** `tools/observe.py` is a read-only live look (frames, radio, held inputs).
  `tools/check_teaching.py --replay-image` makes one non-executing tutor decision on a frame.

MEASURED (non-executing replays through the real `glm-4.6v-flash`, saved frames only):
the login-dialog scene returned `escape` (2,911 input tokens, after one transient retry);
the wolf-beside scene returned `skill:COMBAT_PROFILE` (3,624 input / 67 output tokens, 3.8 s);
the wolf-to-the-right scene met three consecutive "overloaded" (1305) responses. The earlier
attempt spent 28,080 tokens per request and failed validation every time. A text-only probe
showed the key belongs to a GLM Coding Plan: `glm-4.6v` answers on the coding endpoint, but
the plan's terms restrict it to supported coding tools, so the bot stays on the pay-as-you-go
endpoint where only the free Flash model has balance (paid models return 1113).

NOW: deploy the schema 9 addon, enable Blizzard_TimeManager for the character (its disabled
state raises the login dialog), then supervised live acceptance: face -> close -> swing ->
kill -> corpse loot on wolves, scripted and then with Jev choosing.

VERIFICATION: full suite passes; Ruff (`jev tools tests`) and `git diff --check` pass.
New tests cover facing in a simulated world with real plate pixels, toggle state, closing on
range, corpse hover search, every menu rule, strict parsing, re-asks, transient retries and
the scripted fallback. Offline evidence does not establish that a live swing lands.

## 2026-09-23 — First live test of turn-to-face: a wolf killed, and six measured gaps

DID: deployed the schema 9 addon (backup of the schema 8 files kept locally), enabled
Blizzard_TimeManager for the character (its disabled state raised the "Couldn't load"
dialog at every login; the next login showed no dialog), and ran the scripted body on quest
33 for about two and a half minutes (`20260923T084254-eb70bb`, one-second screenshots,
stopped with the stop file; all keys and buttons released afterwards).

LIVE EVIDENCE: facing works. `Targeting.face_selected` centred the wolf's plate in 0-3 turns
each look; Fight pressed the Attack toggle once, strode and re-faced, and the wolf went
100% -> 60% -> 20% while the character took 4%. XP rose 61.8% -> 67.7% ("XP: 53" on screen)
and the corpse lay at the character's feet: the first facing-based kill. Schema 9 decodes
live; `bars.attacking` reads, but `target.melee_range` stays unknown because 2.4.3 gives the
Attack action no range, so closing still ends on damage.

MEASURED GAPS, each fixed in its shared owner:
- The client **cleared the selection at the kill** (wolf at 20% one paint, no target the
  next, XP arriving with it), so Fight reported `lost` and Loot found no corpse. Kills are now
  also proved by experience gained (Fight._settle and the judge's `target_dead`), and
  `Targeting.click_corpse` finds a deselected corpse by a fresh dead hover of the killed
  unit's name, under the last plate it had alive.
- **Selection does not fade other plates**: a rabbit's plate measured as bright (median
  129,121,8) as a selected wolf's. Facing now proves the target's plate by exact hover once
  and then tracks it, with a window that grows with each turn's uncertainty.
- A **health bar's fill shrinks** with health; below about 57% the wolf's plate failed the
  bar shape test. `units.plate_candidates` accepts any fill and centres a partial bar from
  its left edge and the measured full plate width (0.0912 of the client width).
- A **yellow chat line** (40x3 px) passed as a plate; bars under 4 px tall are rejected
  (every measured bar is 5-8 px).
- **Acquisition clicked rabbits**: Fight now hovers each plate and selects only a unit
  whose client-reported name is the wanted one; self-defence keeps the old radio check.
- **Camera calibration took 21 s**: every 10 px step of the drag paid the per-client
  stagger. The stagger is now paid once per gesture (about 4 s expected).

The routine choices Jev makes (COMBAT_PROFILE, LOOT, ...) are now learnable labels for the
local student, so delegating a working routine can itself be handed over.

NOW: the next live window, with the operator's desktop free: repeated kill -> corpse loot ->
Tough Wolf Meat progress with the scripted body, then the same with Jev choosing.

## 2026-09-23 — Scripted spine: kill, loot and quest progress, live and repeated

DID: four supervised scripted runs on quest 33 with one-second screenshots, each gap fixed
in its shared owner and pushed (`75e938f`, `dcfa7e3`, `d510dfb`).

| run | length | kills | loots | Tough Wolf Meat | notes |
|---|---|---|---|---|---|
| `20260923T094917-57131b` | 6 min | 1 | 0 | 0/8 | 28 fights on one stale far selection; stopped in a fence-jump loop |
| `20260923T100704-98a757` | 8 min | 5 | 5 | 0 -> 3 | level 2 -> 3; flower hovers 96 -> 3 |
| `20260923T101745-0798fb` | 8 min | 5 | 5 | 3 -> 7 | look-round acquisition; no lost or unseen fights |

MEASURED GAPS, each fixed in its shared owner:
- **Stale selection.** A Tab pick with no plate stayed selected for five minutes and
  absorbed 28 fights while the hunt walked between spots. A kept selection whose plate is
  not on screen is now dropped for a fresh acquisition.
- **Fence-jump loop.** Every unstick jump read as falling for 0.5-1.06 s and the supervisor
  cancelled the leg mid-air, 17 times in 45 s. Falling now interrupts only after 2.5 s
  (slopes measured up to 2.05 s at the half-second tick).
- **Corpse search order.** All sixteen probes went to grass ring proposals; the grid under
  the last living plate now goes first. Every kill since has been looted.
- **Flower hovers.** Plate candidates whose fill width disagrees with the target's health
  are not hovered (96 wasted hovers -> 3).
- **Paint counter.** 255 is the not-available code, so the sequence decoded as missing once
  every 256 paints and read as blindness. It now wraps at 255, and waiting for paint treats
  one unordered paint as the next baseline.
- **Plates are near.** Nameplates are drawn only within about twenty yards and the camera
  shows about a hundred degrees, so 43 of 52 fights found nothing. Acquisition now looks round
  in quarter turns for a hover-proved plate before Tab (43 -> 29 empty looks, no unseen
  fights).
- **Tab reaches past plates.** Four blind walks at plateless Tab picks found none. The
  ring and name the client draws at selection are now located by the colour that appears
  between frames either side of the key (`units.selection_marks`); the fight turns toward
  that mark before walking, and does not walk without one. Not yet exercised live.

TEACHING: the first teach-mode session (`20260923T102857-75d538`) met four consecutive
"overloaded" replies (1305) from the free `glm-4.6v-flash` and handed the objective to the
scripted routine, as designed. GLM availability, not the loop, is what limits teaching now.

## 2026-09-23 — Jev in charge: five teach sessions, two quests further

DID: five supervised teach-mode sessions (one-second screenshots) with the free
`glm-4.6v-flash` tutor and the scripted routines as fallback; each gap fixed in its shared
owner and pushed (`3bd9c83`, `88306c5`, `2573ad1`, `c4c8139`, `7ba3a3f`, `c15b3d2`).

| session | length | tutor replies | what happened |
|---|---|---|---|
| `20260923T102857-75d538` | 3 min | 0 ok / 3 refused | scripted fallback finished quest 33 (8/8); turn-in failed: ring hidden |
| `20260923T103917-b4cdcc` | 1 min | 2 ok | Jev chose TURNIN_QUEST; frame opened; reward page waited for a choice |
| `20260923T104945-d671ad` | 2 min | 0 ok / 2 refused | **quest 33 handed in** (reward chosen); accept timed out on the walk |
| `20260923T110010-adc4a3` | 15 min | 18 ok / 3 refused | **quest 15 accepted and taken to 10/10**; Jev fought with COMBAT_PROFILE (6), attack_target, clicks and travel; turn-in stuck on the abbey's back ledge |
| `20260923T112542-6d799e` | 1 min | 0 ok / 1 refused | stopped: the operator's desktop became active (browser, Telegram) |

LIVE EVIDENCE: Jev's replies were valid actions every time the provider answered; the
executor refused an imprecise unit click by hover (no wrong click delivered). Quest 7, 33,
783 and 5261 are complete; quest 15 is 10/10 awaiting hand-in. Character level 3 at 78%.

MEASURED GAPS, each fixed in its shared owner:
- **Hidden ring at a quest giver**: plate-anchored body points (V46). Verified live.
- **Reward choice**: schema 10 paints a default; the advance routine and the tutor choose
  it (V47). Verified live; the addon was deployed after a graceful client restart and
  `tools/login.py`.
- **Tutor time billed to the routine**: the body keeps its routine's clock; retries fill
  the tutor's deadline (V48). Verified live.
- **Delegated kill unmeasured**: a fight that selects its own kill is a death on experience
  with a corpse or a counter. Offline.
- **Ledge re-plans through the abbey wall**: re-plans start at the followed route's height
  (V49). Verified against the real navmesh at all five recorded positions; not yet live.
- **Loot missed the completing item**: short-then-complete counts as progress. Offline.

LEARNING: the store holds 20 decision records and 17 episodes over interact, acquire,
approach and travel; every capability is "waiting for more independent successful runs".
No student model has trained yet. Provider availability is the limit: sessions 1-3 got
almost no replies; session 4 got 18 in 15 minutes.

OPEN: `test_actual_controller_corpus_trains_shadows_hands_over_and_rolls_back` fails
intermittently in full-suite runs (canary not promoted); it now reports the failing gate.

NEXT: hand in quest 15 (the route is on an Elwynn grind step after the failed turn-in),
then long teach sessions to give every capability its independent runs.

## 2026-09-23 — Operator review: Holy Light, loot skips, pathing, Claude tutor

The operator was back at the desktop, so no live runs; all of this is offline, pushed
(`f26122b`, `56c3f73`, `237c184`, `d980f89`).

- **Holy Light blocking selection** (V50). Frames 534-543 of `20260923T110010-adc4a3`: the
  between-fights heal, pressed with a looted corpse selected, waited for a target click
  (button lit), and three fights selected nothing. Heals now self-cast; schema 11 paints
  `bars.targeting` and every click path cancels a waiting spell first. Schema 11 files are
  installed in the client and load at the next login.
- **Loot skipped after a kill** (noted, not changed). When another mob joins, the fight can
  end `lost - selected target changed` although its target died (Echo Ridge counter 8 -> 9),
  and only a `killed` fight is followed by looting. NEXT: remember such kills and loot them
  once combat ends.
- **Pathing and unsticking** (V52). Replaying today's stuck positions through the real
  navmesh: the mesh treats a fence, a trunk gap and a ledge as open ground, so no re-plan
  can route round them. `jev.clients.walk_sim` now runs the real `Travel` against simulated
  obstacles; `RouteMemory` learns each blocked spot and the escape that worked, and later
  routes go round it (60-yard fence 42.4 s -> 11.0 s, trunk-and-wall wedge 20.5 s -> 8.8 s
  on the second trip, no stuck events). With this morning's falling grace and re-plan
  height, all four recorded stuck causes have a shared fix.
- **Tutor on the Claude subscription** (V51): `claude-opus-4-7`, medium effort; 5.7 s and
  6.4 s on saved frames. The local teaching launch configuration uses it.

NEXT: with the desktop free - restart the client once (schema 11), hand in quest 15, then
long teach sessions with the Claude tutor; watch `var/route-memory.json` fill as trips repeat.
- **Approach stutter** (V53): reported by the operator ("4 paces, then 4 paces, then a
  couple tiny steps"). Closing is now one steered walk that stops at the first resolved
  swing (schema 12 `combat.swings` from the combat log); reach expires so fleeing units are
  followed. Simulated: 25 yards to a swing in 2.9 s, forward pressed once. Schema 12 files
  are installed in the client (`f422149`).

## 2026-09-23 — Anti-cheat review items 1-3, and a pathing fix they exposed

The operator approved three changes from the anti-cheat review (the event runs a custom
Warden). The Warden test itself waits until the bot is close to fully autonomous. The
addon-ban fallback and the HID-device route are out. Offline only; committed locally
(`e97dc44`, `87f1be9`, `7f1b197`, `6567477`), not pushed.

- **Anonymous addon** (V54). One built file, `StatusStrip`, no globals, no named frames, no
  comments, no project name. The Lua tests run that file. **Installed**: the old `JevRadio`
  folder is in `captures/addon-backup/20260923T131115-JevRadio`, and `StatusStrip` loads
  at the next login. Reinstall with `tools/gen_addon_fields.py --install`.
- **Drawn timing** (V55). Every hold is within 15% and every control-loop wait within 25%.
  The look-round's way, turn sizes and count, and the Tab burst, are drawn per search.
  Waypoint offsets were built, measured and left out.
- **Pathing** (V56). Simulating the drawn timing on the recorded fence failed 5 of 60 first
  trips. The cause was a slide too slow to steer by but not "frozen", plus side-flipping
  detours. Both are fixed: over 40 draws, the fence and trunk-and-wall routes arrived, learned
  and then walked stuck-free every time.

OPEN: the repository is public, so the new addon name is public once pushed. Making it
private is the operator's call. `Humaniser.for_client` seeds from `hash(client_id)`, which
Python randomises per process: timing differs every run, and a replay does not reproduce it
as the docstring claims.

NEXT: unchanged - restart the client (loads `StatusStrip` with schema 12), hand in quest
15, then long Claude-tutor sessions. Watch the first fights for the drawn look-round.

## 2026-09-23 — Live: ten teach sessions, a death spiral found and fixed, quest 15 handed in

The operator handed over the PC. Client restarted; `StatusStrip` (schema 12) loads and
decodes live. Sessions 6-15 (`captures/live-teach-6..15.log`) each found something; every
fix is in its shared owner, tested, and committed locally (not pushed).

| found live | fix | verified |
|---|---|---|
| A fight waited 30 s on the tutor: full health to dead, no swing | reflexes (V57) | live |
| Death popup is modal: tutor pressed Esc five times | reflexes (V57) | live |
| Failed hand-in's rib led past it; playhead lost the way back | retry once, save rejoin (V58) | live resume |
| Step clock counted death, fights and walking | working clock (V59) | offline |
| One rib for 1-12: level 5-6 boars; three deaths at level 3 | ribs per level window (V60) | offline |
| First fight levelled the camera for 5 s at 12% health | level while safe (V61) | live |
| Lost reflex fight stopped the run ("1 failed attempts") | not a step failure (V57) | offline |
| Red plates 5 px: facing never found hostile units | red floor, red search, turn round (V62) | live: 11/11 faced |
| Got up beside the killer, died again, four times | Spirit Healer + hearth (V63) | live |

Heals now land (Holy Light self-cast, V50): four heals kept a resurrected character alive
at 50% against a level 5 caster. Session 15: repaired (durability 0% after the deaths),
handed in quest 15 at Marshal McBride (level 4), accepted quest 21 (12 Kobold Laborers).

OPEN: the character's gear took 25% durability per Spirit Healer rez; the tutor spends
~40 s on map lookups before long walks; `test_real_worker_controller_and_executor_use_one_
input_owner` failed once under load (passes alone and in a quiet full run).

## 2026-09-23 — Echo Ridge Mine: a pit prop stopped the walk and then the fight (V64)

Session 15 (`runs/20260923T184413-a386ff`) walked 366 yards from Marshal McBride toward the
quest 21 kobolds and stopped 95 yards short in the mine's mouth. The follower had spent all
three re-plans on the Abbey's corners, so the pit prop ended the walk without one detour.
The tutor then had the fight walk at a Kobold Laborer in plain view past the same prop:
three six-second walks pressed into it (the position did not move), "never came within
reach", and the watchdog stopped the run at level 4 with 0/12.

Both are fixed (V64). The fight's approach notices a blocked walk or step with travel's
stuck test and strafes off the line, first on a drawn side and then twice as long the
other way. A planned leg blocked with no re-plans left gets the wall heuristic once.
Simulated with the real code: post, post walled on one side, post within melee range, and
four walls on one route all reached. `walk_sim` time now passes for sleeps below a
substep; the first step-mode simulation spun on a float residue.

Session 16 (`runs/20260923T190938-3b2dd7`): the fight's sidesteps fired live (right
0.5 s, left 1 s, right 2 s, left 2 s) and moved the character under a yard. It stood in a
dead-end pocket: the rock face on one side, the log along the mine platform's edge on the
other, the prop ahead. The mesh crosses onto the platform at that log, which the client
does not let a character over. The tutor then spent its episode there and handed over to
the scripted hunt, whose walk got into the mine. Inside, the character stood among
Kobold Laborers and read nothing: the teal cave passes the cyan marker test and ran into
the strip's right marker (V65, fixed and verified against the frame).

OPEN: routes into Echo Ridge Mine can still cross the log (the mesh does not know it);
the teaching watchdog (300 s without progress) always stops a stuck objective before its
own 600 s timeout fails it into a rib.

Session 17 (`runs/20260923T191946-2b79ed`), inside the mine: the first Kobold Laborer
killed and looted (1/12). The tutor then pressed Tab for minutes, the scripted hunt
climbed onto crates in a nook where no strafe moved it, and the watchdog stopped the run.

Fixed offline (V66-V68): blocked spots and plans that avoid them, a standing turn that
ends circling near a point (a real bug the simulation found: 16 of 72 short walks), the
watchdog failing a stalled step into its rib before stopping, and backing off when a
sidestep goes nowhere.

Operator debrief (voice note): every fix for the bigger picture, never a scenario unless it
recurs (caves do); the learner must keep learning from Jev and its Claude fallback, so each
new character reaches level 20 with fewer bugs. Measured against that: the motor learner
held 90 examples from 9 runs and had trained nothing (each capability needs 5 runs and 60
qualified examples; the best had 4); only 14 of 39 tutor episodes succeeded; the policy
learner kept 6 good decisions from 58 runs. `tools/run_summary.py` now reports the learner
every session, and the tutor's two biggest leaks are closed (V69).

Every character now keeps its own playhead (V70): schema 13 names the character on the
strip, the run loads that character's file, and a new character starts from its own quest
log. Installed to the client (loads at the next login); the old save moved to Testvii's
key, `var/playheads/character-26a9640b.json`.

NEXT: restart the client when the PC is free, then a fresh level 1 human paladin.

## 2026-09-24 00:45 — overnight debrief 1: Testvvi, a fresh level 1

The operator made a fresh level 1 human paladin, Testvvi, and handed over the night.
Per-character saves (V70) worked live from the first second: its own file, started at
quest 783, and Testvii's save untouched.

Progress (sessions 1-6, ~1h20): level 1 to 3, quests 783, 7, 5261 and 33 done. Session 1
alone taught the motor learner 29 qualified examples (acquire 15/60, approach 13/60,
interact 11/60 after it). The V68/V69 safeguards fired live: the repeat guard handed
three Tab loops to Jev's routine, and the watchdog failed stalled steps into their grind.

Found and fixed tonight, all general:
- the learner's store lock timed out on Windows (V74, and the live run is now left to its
  own controller, the idle record scan skipped);
- a kill whose selection moves on was reported lost and never looted (V73);
- hunts stood on rings round a centre the mobs were not near: 38 empty looks in one
  session. Hunts now walk the target's own spawn points (V72), from a table beside the
  guide so the guide's bytes, and the learner's corpus, are untouched;
- the motor learner restarted its corpus at every guide change (V71): tied to bindings now;
- the blocked-spot memory (V66) marked Northshire Abbey's halls blocked after single bumps
  and routed round its only doorway for ten minutes. Spots now need two blocks, and the
  four bad ones were cleared;
- two Defias Thugs side by side made every nameplate proof fail and the character died
  not fighting back. An attacker in melee is now fought by the client's own facing errors.

NEXT: keep the loop running; the class trainer (a fresh paladin has no Devotion Aura and
never learns a rank) as the background project.

## 2026-09-24 02:00 — overnight debrief 2: the merchant wall, and six general fixes

Sessions 8-15 (~1h). Testvvi: level 4 at 76%, quest 21 at 7/12, grinding the level 3-5
kobold rib after the quest step timed out (below). Learner: acquire 31/60 (6 runs),
approach 21/60 (5 runs), interact 21/60 (11 runs); the run gates are passed for all three
and they now wait on examples alone.

Sessions 9-14 all stopped at the Northshire merchants within two minutes: the first full
backpack. Four separate faults, each general, each fixed:
- Dermot Johns stands behind his wagon, so his body can never be clicked: the next nearest
  merchant is tried (V78);
- 2.4.3's container gives no quality for grey trade junk, and grey armour was never on the
  sell list: eight grey slots and nothing sellable. The addon now asks the item record
  (installed; the client restarted and logged in), grey weapons and armour sell, a partial
  sale counts, and bags with nothing sellable no longer stop a run (V80). Live: six stacks
  sold, 0 -> 6 free slots;
- the tutor, unable to click a shop's items, pressed Escape into the game menu and out of
  it; Escape is offered only when something can close, trading is routine-only in its
  prompt, and a dialog it leaves open goes back to the policy first (V79, V81);
- from beside the wagons the planner's start snapped onto a wagon (the radio paints no
  height) and five yards of partial path were called arrival: the start height is now the
  ground the last walk ended on, then the destination's, then either side (d0d18ed).

Also general, from the logs: a quest step's clock ran through kills, so quest 21 failed over
at 7/12 while still killing (V75); the tutor could not select a corpse to loot it (V76);
exploration quests are walked into instead of excluded, which returns The Fargodeep Mine,
The Jasperlode Mine and Westbrook Garrison to the route (V77); a kill whose selection moves
on to another unit of the same name was chased as a living target and never looted (in
test).

Checked and not a bug: loot never qualified as a learner example because every loot the
tutor tried was on a corpse the scripted fight had already looted; the effect judge was
right.

Operational: the client came back from its restart on the first-time realm wizard (tick
"Development", Suggest Realm, Accept, then the realm list); `tools/login.py` has no stage
for it yet.

NEXT: keep the loop running; watch quest 21's rejoin and the walk to Goldshire (quest 54).

## 2026-09-24 02:40 — overnight debrief 3: getting around, and quest objects

Sessions 15-19. Testvvi: level 5, quest 21 at 9/12, on the kobold rib beside Echo Ridge
Mine with quest 21 as its way back. Learner: acquire 38/60 (8 runs), approach 25/60,
interact 21/60.

Fixed, all general:
- a selection whose health jumps up is another unit even under the same name: the next
  Kobold Worker after a kill was chased as the same one and the kill never looted (V82);
- the tutor's selections may be proved over a nameplate: 27 of its 40 refused selections
  were aimed at one (f392c3d);
- plans start at the ground the last walk ended on, then heights either side of the
  destination's, to 60 yards: the merchant wagons and Echo Ridge Mine's wooden platform
  had each snapped the start onto the wrong floor (V83; verified live, a 1,197-yard plan
  from the platform);
- Escape is never offered to clear a selection: in a fight it raised 2.4.3's
  "Blizzard_TimeManager has been blocked" popup instead (V84);
- walking closer to the step is progress to the watchdog, and a failed step goes to the
  nearest rib within two levels: a level 5 fail-over went to wolves 1,200 yards away and
  was failed over again on the way (V85);
- a walk's time limit grows with its route (a flat 180 s was the clean time for the
  1,038 yards to Gerard Tiller);
- quest objects are gathered (V86): schema 14 paints the stock tooltip's name for a world
  object under the pointer, and the grind walks the object's spawn points and right-clicks
  only where a fresh hover names it. Milly's Harvest, Grape Manifest and A Bundle of
  Trouble rejoin the route. Installed; first live use will be quest 3904;
- login reads the realm wizard, the realm list and a loading character list (whose red
  sunset sky had passed for the login form). A client restart at 02:28 still needed a
  second restart: the world session never sent the character list, and a fresh client
  logged straight in.

Noted, not changed: 80 motor rows had their own effect verified but sat in episodes that
failed; acquire would pass its gate with them, but 28 are the tutor's futile shop
open/close cycles. The episode gate is a quality filter; worth the operator's view.

NEXT: the loop; quest 21's rejoin, then Goldshire; the first live gather at 3904.

## 2026-09-24 03:55 — overnight debrief 4: quest 21 done, tutor sharpened

Sessions 19-23. Testvvi: level 6. Quest 21's twelve Kobold Laborers are done; its hand-in
timed out beside Marshal McBride (below) and failed into the level 5-7 wolves, with the
hand-in as its way back. Learner: acquire 55/60 (11 runs), approach 41/60 (10 runs),
interact 22/60 - acquire is five examples from its first trained model.

Fixed, all general:
- quests taken or handed in at objects (a wanted poster, a body) are opened by the name
  their tooltip gives, and a quest whose target is an elite is left out of the route:
  Find the Lost Guards and its chain are back, Wanted: "Hogger" is not (V87);
- the tutor has a right-click for quest objects (`use_object`), proved by the tooltip name;
- the strip paints the selected unit's GUID (`target.guid`, schema 14 redefined within the
  hour it existed): a new Kobold Worker is a new selection even at the same name and health,
  to the tutor's judge and to the fight (V88; verified live, three Workers, three GUIDs);
- the tutor's image carries a scale along its edges: it had described a plate at 0.65 and
  sent 0.98, twice (V89);
- Tab is offered only where any enemy will do (3 of 22 presses worked on objective steps);
  a fight says its corpse was looted, so the tutor stops clicking emptied corpses;
- the Escape that closes a merchant with the merchant selected raises 2.4.3's
  "Blizzard_TimeManager has been blocked" popup; one more Escape now dismisses it;
- an NPC interaction looks round in quarter turns before giving up on the nameplate:
  McBride stood out of view while the character faced the Main Hall's wall.

Client restarts: two tonight, each needing a second restart - the client asked for its
character list while the server was still logging the old session out (about 30 s in
combat). Wait for the Logout line in Char.log before relaunching (saved to memory).

The strip's 4-bit schema header is now used up (14 is its last value): the next field
needs a revision number in the header first.

NEXT: the hand-in after the wolves, then Goldshire (54); the first live gather at 3904.

## 2026-09-24 04:40 — overnight debrief 5: a death spiral, and the way out of it

Sessions 23-27. Testvvi, level 6: quest 21 handed in (by the routine, nine seconds after its
step had timed out into a rib, so the tracker never credited it; the next resume did), quest
54 accepted, now walking to Goldshire. Learner: acquire 55/60, approach 42/60, rest 16/60 -
and acquire can now train at all (V90: the held-out split had asked two runs for twenty
examples, which at five a run it could never have).

Ten deaths in sessions 24-26, on the level 5-7 wolf rib the failed hand-in sent it to, each
link a general fault, each fixed:
- a unit directly behind the character stands on the screen's centre line too; the fight
  trusted the plate over the client's "facing the wrong way" and walked away from a wolf
  biting its back (V91);
- ribs have no fail edges and a step's deaths were counted in memory only, restarting every
  session: a rib that has killed the character twice is now left for its way back, and the
  count is kept in the playhead (V92);
- the watchdog counted time dead and running back as a stall, and failed the hand-in over
  into the wolves;
- at the body the 20 s of looking gave up during the reclaim delay; the Spirit Healer
  answered three right-clicks with nothing, twice (unexplained - it worked yesterday); and
  getting up at the body, at half health among the wolves, killed it again. The body is
  now watched for 150 s, and a trap body is reclaimed 32 yards short of it, on the
  graveyard's side (the server's reclaim radius is 39) (V92);
- with its gear broken it lost at full health on the walk to a repairer: broken gear far
  from one now goes home by hearthstone first. Verified live: hearthed, walked 49 yards
  to Godric Rothgar, repaired, accepted quest 54.

Also: gathered-object search steps back and looks again where the character hides what is
underfoot; a kill whose corpse is not found is still a kill; NPC interactions look round
before giving up (McBride out of view); the Escape popup is dismissed by the close that
raised it; an attacker whose plate never settles is fought where it stands.

The real limit under all of it: a level 6 paladin with Seal of Righteousness and Holy Light
alone. The trainer and a way to use trained spells are the operator's call (below).

NEXT: Goldshire (54), then 18; the first live gather at 3904.

## 2026-09-24 05:20 — overnight debrief 6: a pack, a heal, and the Spirit Healer explained

Sessions 28-31. Testvvi: level 6 at 80%, walking to Goldshire for quest 54's hand-in. Nine
deaths in sessions 29-30, all from one chain, each link now fixed and general:
- the hunt's tour began at the spawn nearest the camp's centre - in the level 5-7 wolf camp
  a spawn with three others inside twenty yards. The walk there pulled four Mangy Wolves at
  once. Lone spawns are now walked before packs, and round a pack where possible (V95);
- getting up at the body among the same wolves killed it again, four times: the 32-yard
  short reclaim ran only after a quick second death, which a new session never remembers.
  Every body is now reclaimed short of it (V94; seen working at session 31's start);
- the Spirit Healer's silence is explained. The server drops a right-click from beyond five
  yards without a word (`INTERACTION_DISTANCE`), and when the healer does answer it opens a
  gossip ("Return me to life.", menu 83), not the popup the recovery waited for. The
  interaction now steps in on silence and chooses that line (V94; not yet seen live);
- between two wolves it healed at 44% with its target at 18%, two swings from dead, and
  never swung again: a heal now waits while the target will die well before the character
  would, by the rates the fight has shown (V96).

The failed hand-in's three deaths dropped it to the level 3-5 kobold rib at Northshire, 1000
yards off through wolf country; the rib's own two deaths sent it back to 54.

Learner (read from the live store, nothing written): acquire 55 qualified in 11 runs (26
train / 29 held out; needs 40 train), approach 39, rest 26, interact 25. No model yet. The 44
acquire rows in interrupted episodes are unproductive tutor wandering (Escape, turning,
clicking emptied corpses), so the episode gate is right to leave them out.

The real limit stays: an untrained paladin. No Divine Protection, Judgement or auras, so two
level 6 wolves at once are a loss. See the trainer sketch below.

NEXT: 54's hand-in, then quest 18 back in Northshire; a live Spirit Healer rise; heals held.

### Trainer sketch (for the operator: approve, change or refuse)

Why: every death tonight after the fixes above is two mobs at once on a paladin with Seal of
Righteousness and Holy Light alone. A level 6 paladin should have Devotion Aura, Blessing of
Might, Judgement and Divine Protection. Roughly 3-4 hours with live checks, so not started
unasked. Everything below is HID input; the addon only paints, and nothing is typed.

1. Which trainer: the class's own, from the world database (paladin: Brother Sammuel in
   Northshire, Brother Wilhelm in Goldshire). The route's four `train` nodes are other
   classes' trainers, picked by NPC flags (ROADMAP item 7).
2. Open: right-click, then choose the gossip line by hash - "I would like to train further
   in the ways of the Light." The same gossip also offers "I wish to unlearn my talents.",
   so position is never trusted. `ui.trainer` already paints the frame.
3. Buy: add `ClassTrainerTrainButton` to the addon's advance buttons (no new field). Each
   click buys the selected service and the client selects the next learnable one; stop when
   `bags.money_copper` stops falling. New ranks replace the old ones on the bars by
   themselves.
4. Put new spells on the bar: open the spellbook and drag each unplaced spell onto the next
   empty main-bar slot. Needs the strip to paint unplaced spellbook buttons (and a tab
   holding one) plus the first empty slot. Either the list and advance fields carry them
   while the spellbook alone is open (no new fields), or the schema header gets its
   revision number first (it is used up at 14). **Your call.**
5. Use them: the profile generator adds trained rows in designated slots - an aura kept up,
   a blessing kept up, Judgement on its cooldown, Divine Protection then Holy Light under
   20%. A row whose slot is empty is already skipped. Each drop is checked against the
   slot's usable bit before the next one.

What the trainers offer (world database, `npc_trainer_template` 20 in Northshire, 21 in
Goldshire), against Testvvi's 4 silver 26 copper at level 7:

| Level | Spell | Cost | Needs a bar slot |
|---|---|---|---|
| 1 | Devotion Aura 1 | 10c | yes |
| 4 | Judgement; Blessing of Might 1 | 1s each | yes |
| 6 | Divine Protection 1; Seal of the Crusader 1 | 1s each | yes |
| 6 | Holy Light 2 | 1s | no - replaces rank 1 on the bar |
| 8 | Hammer of Justice 1; Purify | 1s each | yes |
| 8 | Parry | 1s | no - passive |
| 10 | Lay on Hands 1; Blessing of Protection 1 | 3s each | yes |
| 10 | Devotion Aura 2; Seal of Righteousness 2 | 3s each | no - ranks |
| 12 | Blessing of Might 2; Seal of the Crusader 2 | 10s each | no - ranks |
| 14 | Holy Light 3 | 20s | no - rank |

Northshire's Brother Sammuel stops at level 6; Goldshire's Brother Wilhelm goes on. Steps 1-3
alone (no spellbook) would already buy Holy Light 2, Parry and every later rank.

## 2026-09-24 05:55 — overnight debrief 7: the Spirit Healer rises, and the fights that cannot be won

Sessions 31-33. Testvvi: level 7 (quest 54 handed in at Goldshire), repaired, and quest 18
(Brotherhood of Thieves) accepted; now hunting Defias Thugs in the Northshire vineyard.

Verified live since debrief 6:
- the Spirit Healer raises the ghost (V94): "up at the Spirit Healer; hearthstone: home";
- every body reclaimed short of it (V94): session 31 got up 34 yards off and won;
- heals held while the target dies first (V96): ten `heal.held` in session 32, a Forest
  Spider at 6% killed instead of healed at;
- loot onto a stack counted (V97): "took - an item onto a stack";
- looted bags equipped at full bags (e89a814): slots 16 -> 22 -> 28.

Fixed, general (offline-tested, live next):
- a cast now holds the fight's evidence clocks still (V98): each Holy Light had aged the last
  hit past the re-aim window, and the fight stepped, levelled and turned at a wolf already in
  melee - eleven seconds without a swing after one heal;
- a leg whose end comes no closer in 12 s is stuck, moving or not (V99): the character walked
  up a hillside and slid back for 147 s on the way to Deputy Willem, and quest 18's accept
  failed on its clock into the level 5-7 wolves;
- experience is a grind teaching episode's progress (V100): kills went uncounted when the
  client cleared the selection at the kill; one episode ran 872 s through several and ended
  "stalled", and acquire gained 2 examples in six sessions;
- an attacker that bites and cannot be hit is drawn out: back off, then turn round and run
  clear (V101). A Mangy Wolf below a tree's roots, the character up on them, 3D melee reach
  never met: four "unreachable" fights and one death.

Deaths since debrief 6: three, all at the level 5-7 wolf camp - the tree above, and three
attackers at once on the walk to a repairer. Durability went to 0; the hearth and repair
recovered it.

The limit is unchanged and now measured: at 26% health against one level 6 wolf, Holy Light
(rank 1, five to six seconds a cast under pushback) left the wolf untouched for 22 s; the
fight was a stalemate its six heals only just survived. Rank 2 alone doubles each heal.

## 2026-09-24 06:10 — overnight summary, for the operator

Testvvi went from level 3 (15%) at 00:10 to level 7: 32 supervised sessions, all on the
session loop, 73 commits (all local, none pushed). Quests done since the fresh level 1: 783,
7, 15, 33, 5261, 21 (handed in; the playhead missed it) and 54. Now: quest 18 (Brotherhood of
Thieves) in the Northshire vineyard, 2/12 bandanas.

What stopped it, and the general fix for each (DECISIONS V75-V105; debriefs 1-7 above):
- getting around: stairs and start heights, the Abbey's doorways, fences, slopes it slides
  back down (V99), NPCs out of view, the realm and character screens;
- merchants and bags: grey gear sold, placeholder NPCs skipped, looted bags equipped (live);
- quests: objects gathered or opened by tooltip name, exploration, elite quests left out;
- deaths, the largest cost of the night (24 since midnight, 21 of them lost fights):
  - reclaim short of the body, and the Spirit Healer answering once stepped in on (live);
  - broken gear sent home by hearthstone (live);
  - ribs left after two deaths, and chosen among mobs never above the character's level;
  - packs walked last (V95), meals eaten out of the camp's reach (V104);
  - "facing the wrong way" behind the character, casts no longer read as a lost target
    (V98), a nearly dead target finished before a heal (V96, live), an attacker that
    cannot be hit drawn out (V101);
  - a tutor holding the hunt no longer left to decide while something kills the character
    (V105; two deaths in session 34, fixed within the hour);
- learning: grind kills now count as progress (V100) and the tutor's own fights as kills
  (V103, live). Qualified examples: acquire 55, approach 39, rest 35, interact 27; the
  gates want 40 training examples on top of the held-out ones, so still no model.

Decisions waiting for you:
1. **The class trainer.** Every fight this character loses is a Seal of Righteousness and
   Holy Light rank 1 fight. Sketch above ("Trainer sketch"); the open question is how the
   strip paints the spellbook (reuse the list fields, or give the header a revision).
2. **Pushing.** 95 commits are local. The remote is public, and the addon's in-game name
   (StatusStrip) would be published with them: push as is, or make the repo private first?
3. **Connectors.** Gmail and Google Calendar need authorising in claude.ai's connector
   settings before they can be used here.

Operator action, 06:10: quest 18's objective failed on its three-deaths edge into the level
5-7 wolves, 988 yards off, at 6/12 bandanas. Two of the three deaths were V105's bug, fixed
at 06:06. The loop was stopped, the playhead put back on `18_brotherhood_of_thieves_do` with
its deaths cleared (the previous file is at `/tmp/playhead-before-0610.json`), and the loop
restarted at session 37.

## 2026-09-24 06:50 — debrief 8: quests flowing again

Since the summary: quest 18 (twelve bandanas) and 3903 handed in, 3904 (Milly's Harvest)
at 2/8 crates. Two more general fixes from live evidence:
- an object click nothing answers is out of reach, as the Spirit Healer's was: the gather
  turns toward the crate, steps in and clicks again (V106). The tutor, told the same,
  took crates itself;
- a selected bystander in combat is not the fight: a Defias Thug at full health, neither
  biting nor near, stayed selected for 40 s while another killed the character. Defending
  now picks what is attacking first (V107).

## 2026-09-24 07:55 — debrief 9: Milly's Harvest gathered; the Abbey's stairs

Quests 3904 (Milly's Harvest: eight crates, the first object-gathering quest done live) and
3905's accept since debrief 8. Testvvi is level 7 at 85%.

- a tutor looped "arrived" at the objective's point for ten minutes; a repeated arrival
  now counts toward the stall (V108), and the scripted gather finished the crates;
- 3905's hand-in failed on the Abbey's spiral stair to Brother Neals: 16 of 23 legs,
  then out of time and wedged against a barrel. Two wedged walks running now go home by
  hearthstone (V109; verified live, out of the Abbey).

Found and **not** fixed, for your view: the strip paints map x/y only, and the Abbey's
stair has waypoints on different floors at the same x/y. A 2D follower can take an upper
floor's waypoint as reached from below. Brother Neals (and any upstairs NPC) needs height:
a strip field for it, or a follower that walks stair legs by time. The step now fails into
the level 5-7 wolves until level 8, then comes back to him.

Operator action, 08:00: the loop was stopped between sessions and the playhead given
`retried: [3905_grape_manifest_turnin]` (V110): its first failure, in session 43, was only
in that session's memory. The previous file is at `/tmp/playhead-before-0800.json`.
That stop cost a death: session 45 was stopped mid-fight (25% health against a level 6 mob
at 71%), and the character stood idle through the restart and died. Operator stops go
between fights from now on.

Also since debrief 9: retried steps are kept in the playhead, so a step that fails after
its rib is passed over across sessions too (V110), and an operator stop waits out a fight
in progress (V111). Testvvi at 08:05: level 7 at 85%, on the level 5-7 wolf rib with Brother
Neals' hand-in (3905) as its way back; one more failure there passes it over to quest 6.
