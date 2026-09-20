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

**There is no facing API in this client** (V17). `GetPlayerFacing` is 3.0+, so heading has
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
