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
