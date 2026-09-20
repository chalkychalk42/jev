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
