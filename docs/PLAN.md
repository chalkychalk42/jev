# JEV — TBC Private-Server Leveling Agent

Implementation update, 22 September 2026: the approved
[visual teaching loop](TEACHING_LOOP.md) now defines teacher-led play and gradual learned
handover. Its control and evaluation contract supersedes the high-level-only teacher
split in this original planning document.
## Ultimate build plan (vision + virtual input, guide-directed, multi-client distillation)

Status: planning document for a custom TBC private-server hackathon  
Agent name: **Jev**  
Escalation model: **Claude** (rare)  
Interface constraint: **vision + virtual HID only** on the game process  
Optional: thin telemetry addon (`JevRadio`) that **paints state**, never actuates

This document is the whole system: product gates, architecture, schemas, loops, content, learning, ops, risks, and a demo script.

---

## 1. What you are actually building

You are not building “an LLM that plays WoW.”

You are building a **guided factory**:

1. A **GuideGraph** (Lime/RXP-shaped curriculum you author, not a scraped commercial guide).
2. A **fast body** (System 1) that walks, faces, swings, loots, eats at 20–30 Hz.
3. A **slow coach** (Jev) that holds one active guide step and arms skills at 0.2–2 Hz or on events.
4. A **staff engineer** (Claude) that authors or debugs a skill only when the same step fails repeatedly.
5. A **shared memory** (RAG + episode store) written by 6–10 clients at once.
6. A **wean path**: clone the coach’s *choices* and the body’s *motor skills* so tokens/hour fall while XP/hour holds.

1–70 is a **content + recovery** problem wearing an AI hat. Combat is a priority list. Geography is a graph. Intelligence is “advance the step, or skip, or rejoin, or escalate.”

---

## 2. Non-negotiable constraints

### 2.1 Hackathon / anticheat

- No DLL inject, no memory read/write of `wow.exe`, no packet forge, no speed/teleport.
- Control channel: virtual keyboard/mouse (SendInput or USB HID gadget).
- Sense channel: screen capture of the client window (WGC/DXGI), plus optional on-screen addon pixels.
- Assume the server watches **behavior** as well as binaries: session length, path repeat, reaction variance, gold/XP rate.
- Randomize at the actuator. Do not “look human” in the prompt and then click on a 16 ms grid.

### 2.2 Addon policy

`JevRadio` is legal only if it:

- reads stock TBC Lua APIs (`UnitHealth`, `GetPlayerMapPosition`, quest log, bags, buffs, errors, facing);
- **renders** a compact pixel strip or tiny sidecar;
- never calls `UseAction`, movement, or CVars that play the game.

If judges ban addons mid-event: fusion confidence drops, guide shrinks to grind-heavy nodes, Jev becomes conservative. That failover is a feature you demo.

### 2.3 Copyright

Do **not** vendor RestedXP, Zygor, or Lime text/code. Reuse the *shape* (ordered steps, coords, accept / kill / turn-in / grind / train / fly). Author the graph from:

- public quest IDs + NPC coords (Wowhead/Wowpedia style data you have rights to use);
- a recorder pass you walk yourself on *this* server;
- grind boxes when the server is missing a quest.

### 2.4 Honesty about 1–70

| Claim | Reality |
|---|---|
| 48h hackathon, 1–70 unattended questing | Almost certainly false |
| 48h: 1–12 reliable + factory + story to 70 | Correct target |
| Multi-day: 1–30 grind spine | Achievable |
| Weeks + path pack: 1–70 grind-first | Achievable |
| Vision-only full quest UI 1–70 | Research project |

---

## 3. Success gates (use these, not vibes)

### Gate A — next round demo

- 1 class, 1 faction, 3+ clients.
- 1→12 without a human touching the mouse for 40 minutes.
- Death → corpse → rez → rejoin step.
- Vendor + repair when bags/durability trip.
- Addon-off degrade still walks and fights (uglier, slower).
- Dashboard: XP/h, deaths/h, stuck/h, % ticks by S1 / Jev / Claude / human.

### Gate B — factory

- 6–10 clients writing one replay store.
- Two starting zones or two offsets on the same graph.
- Claude < 6 calls/hour/client.
- At least 8 skills with success_rate logged.

### Gate C — wean begun

- In one bracket, a classifier picks `advance|skip|rejoin|grind` at ≥90% agreement with Jev.
- That bracket can run with Jev heartbeat 10× slower.
- Tokens/hour down, XP/hour flat.

### Gate D — 70 (post-event)

- Spine graph 1–70 with grind ribs.
- Mount 30 / 60 / flying 70 treated as hard money gates, not surprises.
- Outland vendor + flight + hellfire grind loop exists.
- No requirement that every quest in RXP exists.

---

## 4. System map

```
                    ┌─────────────────────────────────────────┐
                    │ Orchestrator (Jev host)                 │
                    │  GuideGraph  RAG  Episode store         │
                    │  Jev loop    Claude queue  Learner      │
                    │  Eval board  Supervisor                 │
                    └───────────────┬─────────────────────────┘
                                    │ Redis/NATS
          ┌─────────────────────────┼─────────────────────────┐
          ▼                         ▼                         ▼
   Client 1..N window          Client 2                  Client N
   capture + HID               ...                       ...
   Perception + S1             Perception + S1           Perception + S1
   Skill runtime               Skill runtime             Skill runtime
   GuideTracker (local)        GuideTracker              GuideTracker
```

**Process split**

- **Game PCs:** WoW windows, capture, HID, System 1, local tracker.
- **Brain PC:** Jev, Claude, RAG, trainer, dashboard.
- Never run 10 YOLO heads and Claude on the same GPU as the clients if you can avoid it.

**Clock domains**

| Domain | Period | Allowed to do |
|---|---|---|
| S1 | 33–50 ms | move, face, swing, loot click, eat start, interrupt |
| Tracker | 200–500 ms | step predicates, off-route, timeouts |
| Jev | event or 0.5–2 s | arm skill, advance/skip/rejoin |
| Claude | minutes, queued | write skill, postmortem, new subgraph |
| Learner | nightly / idle | BC, distill, eval |

If Jev is on the GCD, you already lost.

---

## 5. Perception

### 5.1 Capture

- Per-window WGC or DXGI duplication cropped to that client.
- Lock: 1280×720 (or 1600×900), same UI scale, same gamma, interface scale 1, nameplates on, actionbars 1–6 visible, quest watch on.
- Cap decode at 15–20 FPS even if the game runs 60. S1 does not need 60 FPS video.

### 5.2 Two sensors, one state

**Vision heads (cheap first)**

1. **UI templates:** HP/mana orbs or addon strip, cast bar, loot window, gossip, vendor, dead UI, error banner.
2. **OCR:** quest watch, error text, gold, bag-full, zone text on load.
3. **Nameplates:** hostile present, targeting pip.
4. **World sparkle (optional):** corpse, herb, ore. YOLO only here if templates fail.
5. **Bobber (optional, not on 1–70 critical path).**

**JevRadio (TBC Lua → pixels)**

Encode ~80–120 values into a fixed grid of 4×4 or 5×5 squares (24-bit RGB each):

- map + xy + facing
- zone token / area id if available
- hp, maxhp, mana/energy/rage, max
- combat, dead, ghost, mounted, indoors, swimming, falling, taxi
- target exists, target hp%, reaction, elite
- bag free slots, durability min
- quest log hash + 3 watched objectives (counts)
- last UI error enum
- actionbar usable bits / gcd / casting
- money (capped encoding)
- class / level / xp%

Paint only. If the strip checksum fails, mark `addon_ok=false`.

### 5.3 Fusion

```
state.hp = addon.hp if addon_ok else vision.hp
state.confidence = 1.0 if addon_ok else vision.conf
state.source = fused
```

Rules when sources disagree:

- Trust addon for numbers (hp, xy, quest counts).
- Trust vision for windows (loot/gossip actually on screen).
- If xy jumps across the map in one tick: loading screen or bad decode → freeze S1 movement 2 s.

### 5.4 What you refuse to see

You do not detect every wolf mesh in 3D. Routes put wolves in front of the character. Vision answers **windows and flags**, not the whole simulation.

---

## 6. World state schema (single object, versioned)

`state_v1` — this is the contract. Perception, tracker, Jev, dataset all use it.

```json
{
  "schema": "state_v1",
  "t": 1720000000.12,
  "client_id": "c03",
  "char": {"name": "Jevmule", "class": "MAGE", "faction": "ALLIANCE", "level": 11, "xp_pct": 0.42},
  "pos": {"zone": "Elwynn", "sub": "Goldshire", "x": 0.471, "y": 0.622, "facing": 3.14, "indoors": false},
  "vitals": {"hp": 0.81, "power": 0.60, "power_type": "MANA", "combat": false, "dead": false, "ghost": false},
  "target": {"has": true, "hp": 0.4, "elite": false, "attacking_me": true},
  "bags": {"free": 4, "durability_min": 0.34, "gold": 1840},
  "flags": ["MOUNTED?", "SWIMMING", "FALLING", "ON_TAXI", "AFK_FLAG"],
  "ui": {"loot": false, "gossip": false, "vendor": false, "quest_frame": false, "error": null},
  "quests": [{"id": 176, "title": "Wanted: Hogger", "obj": [{"t": "Kill Hogger", "n": 0, "m": 1}]}],
  "guide": {
    "graph_id": "ally_human_tbc_v3",
    "step_id": "ally_human_012_hogger",
    "kind": "quest_objective",
    "age_s": 173.2,
    "on_route": true,
    "progress": 0.15
  },
  "sense": {"addon_ok": true, "vision_conf": 0.86, "source": "fused"},
  "control": {"armed_skill": "GRIND_UNTIL", "armed_by": "jev", "s1_mode": "combat"}
}
```

If a field cannot be sensed, it is `null` plus lower confidence. Never invent coords.

---

## 7. GuideGraph — the spine

### 7.1 Why

A 1–70 guide is the curriculum. Jev should not search Azeroth. Jev should **service the next node**.

ML reward becomes:

- `+1` step advanced
- `+xp_delta`
- `-1` death
- `-1` stuck_timeout
- `-0.2` off_route_s / 10
- `0` for flavor quests you marked skippable

### 7.2 Node kinds

`travel | quest_accept | quest_objective | quest_turnin | grind | train | vendor | repair | flight | boat | hearth | buy | skippable_gate | money_gate | ding_gate`

`ding_gate`: “reach level 12 before next zone.”  
`money_gate`: “60g for mount; if gold < 60, run grind rib.”

### 7.3 Node schema

```json
{
  "id": "ally_human_012_hogger",
  "level": [8, 12],
  "zone": "Elwynn",
  "kind": "quest_objective",
  "pos": [0.25, 0.60],
  "r": 0.04,
  "quest_id": 176,
  "objectives": ["Kill Hogger"],
  "requires": ["ally_human_011_westbrook_turnin"],
  "next": ["ally_human_012_hogger_turnin"],
  "on_fail": [
    {"if": "timeout_s > 240", "goto": "ally_human_012_grind_east"},
    {"if": "quest_missing", "goto": "ally_human_012_grind_east"},
    {"if": "deaths_on_step >= 3", "goto": "ally_human_013_skip_hogger"}
  ],
  "skills": ["TRAVEL_TO", "COMBAT_PROFILE:mage_single", "LOOT"],
  "xp_est": 1100,
  "notes": "elite pack possible; skip if on_fail"
}
```

### 7.4 Graph layers

- **Spine:** ordered 1–70 brackets (the Lime idea).
- **Ribs:** grind loops hanging off a bracket (`*_grind_*`).
- **Recovery:** spirit healer + corpse per zone.
- **Service:** vendor, trainer, flight, inn hearth bind.
- **Gates:** mount 30/60, spells that cost money, Outland flying later.

### 7.5 Tracker

Local to each client.

```
every 250ms:
  if dead: event DEATH (do not advance)
  if predicate(step): ADVANCE → next[0] or choose next by level
  if dist(step.pos) > R for T seconds and not travel skill: OFF_ROUTE
  if step.age > timeout: FAIL → apply on_fail
  if quest_id required and not in log and not completable: quest_missing
```

Predicates:

- accept: quest id in log
- objective: counts >= need
- turnin: quest not in log and was
- grind: level >= N or xp_pct >= T
- travel: hypot(xy - pos) < r
- vendor: free slots >= K and durability > 0.4 (after skill)
- train: (best-effort) spell rank present or timeout-and-skip

### 7.6 Authoring the graph on THIS server

Day-0 recorder (human):

- Walk 1–20 once.
- Hotkeys: mark accept / turnin / vendor / grind box / spirit / trainer / skip.
- Dump nodes with current xy + quest log snapshot.
- Diff against public TBC quest list; mark missing as skippable.

Do not generate 1–70 from a retail guide and hope. Custom servers delete, rename, and relevel quests.

### 7.7 1–70 spine (Alliance sketch — Horde is a parallel file)

Brackets, grind-first, quests as toppers:

| Band | Zone spine | Rib if quests fail |
|---|---|---|
| 1–6 | Starter (Northshire / Coldridge / …) | starter woods loop |
| 6–12 | Elwynn / Dun Morogh / Teldrassil / Azuremyst | east Elwynn grind |
| 12–18 | Westfall / Loch / Darkshore / Bloodmyst | WF shore / Loch |
| 18–24 | Redridge / Duskwood edge / Ashenvale start | RR orbs / Barrens if mid |
| 24–30 | Duskwood / Wetlands / Ashenvale | wet-avoid drown ribs |
| 30–40 | Arathi / Desolace / STV north / Thousand Needles | after mount |
| 40–50 | Tanaris / Feralas / Hinterlands | Tanaris pirate grind |
| 50–60 | Un’Goro / Felwood / WPL / Burning Steppes | Ungoro crystal grind |
| 60–62 | Hellfire | HP peninsula grind |
| 62–64 | Zangarmarsh | coilfang outskirts grind |
| 64–66 | Terokkar / Nagrand | bone wastes grind |
| 66–68 | Nagrand / Blade’s Edge | ogre loop |
| 68–70 | Shadowmoon / NS / Blade’s | SM grind to 70 |

Horde file mirrors: Durotar/Mulgore/Eversong → Barrens spine is actually *better* XP; keep Barrens ribs fat.

**Skip all dungeons** on the spine for v1. Instances are a different perception problem (no map xy on some clients, tight geometry).

---

## 8. Skills and System 1 (the body)

### 8.1 Skill = temporally extended program

Jev never emits raw keys in the happy path. It arms a skill.

Canonical catalog (implement in this order):

1. `IDLE`
2. `FOLLOW_PATH` (polyline + navmesh if you have one, else waypoint stick)
3. `TRAVEL_TO` (point on map, uses FOLLOW_PATH)
4. `COMBAT_PROFILE` (named JSON rotation)
5. `APPROACH_TARGET` / `FACE`
6. `LOOT` / `SKIN?`
7. `EAT_DRINK`
8. `VENDOR_REPAIR`
9. `TRAIN_CLASS`
10. `CORPSE_RUN` / `RELEASE_SPIRIT` / `REZ`
11. `HEARTH`
12. `FLIGHT_PATH`
13. `BOAT_OR_ZEP` (timer + zone-change predicate — fragile)
14. `ACCEPT_QUEST` / `TURNIN_QUEST` / `GOSSIP_PICK`
15. `GRIND_UNTIL` (wraps path + combat + loot + eat)
16. `STUCK_RECOVER`
17. `BAG_MAKE_SPACE` (destroy greys / vendor if near)
18. `BUY_AMMO_REAGENT_FOOD`
19. `MOUNT_UP` / `DISMOUNT`
20. `ABORT_WAIT`

Each skill:

```json
{
  "name": "VENDOR_REPAIR",
  "pre": ["ui.vendor or dist(vendor) < r or armed_with_travel"],
  "steps": ["TRAVEL_TO vendor", "INTERACT", "REPAIR", "SELL_GREY", "CLOSE"],
  "success": "bags.free >= 6 and durability_min > 0.7",
  "timeout_s": 180,
  "on_fail": "STUCK_RECOVER then retry once else escalate"
}
```

### 8.2 GOAP / priority (runtime, not LLM)

World bits → pick skill if Jev has not armed one, or **preempt** if safety trips.

Hard preempts (S1, no Jev):

- `hp < 0.20` and combat → eat unavailable; potion if profiled else run
- `dead` → CORPSE_RUN pipeline
- `stuck_s > 8` → STUCK_RECOVER
- `falling` / `swimming_drown` → emergency keys
- loot window open → LOOT (short)

Soft priority when idle:

`safety > corpse > vendor_if_critical > eat > loot > combat > armed_guide_skill > grind_default`

This is the WowClassicGrindBot idea: costed goals, ~24 flags. You do not need their C# repo, you need that control split.

### 8.3 Combat profiles (data, per class × bracket)

Example mage early:

```json
{
  "id": "mage_single_1_20",
  "pull": [{"key": "1", "name": "Frostbolt", "req": ["mana>0.35", "range_fb"]}],
  "combat": [
    {"key": "2", "name": "Fireball", "req": ["target_hp>0.25", "mana>0.30"]},
    {"key": "3", "name": "Fire Blast", "req": ["target_hp<0.20"]},
    {"key": "0", "name": "wand", "req": ["mana<0.25"]}
  ],
  "panic": [{"key": "frostnova", "req": ["hp<0.35", "melee_on_me"]}]
}
```

Hunter/warlock/pets = phase 2. Start **Mage or Rogue or Warrior**. One body plan.

### 8.4 Navigation

v1: recorded polylines per step + click-to-move or WASD toward next waypoint using facing + xy from addon.

v2: navmesh (Recast/DotRecast or server-exported). Worth it after 20, when indoor inns and Wetlands rivers appear.

Stuck detect:

- xy displacement < ε for 5 s while a move skill is armed
- heading oscillation
- same waypoint index for too long

Recover: jump, strafe random, back up, repath, hearth if 3 fails.

### 8.5 Virtual input

One module: `hid.py`

- key down/up, mouse move/click, wheel
- focus the correct HWND
- jitter: 30–90 ms, mouse Bezier, no 0-delay chords
- stagger client ticks by `client_id * 7 ms` so 10 bots are not a chorus

S1 talks only to `hid.py`.

---

## 9. Jev (System 2)

### 9.1 When Jev runs

Wake on:

- skill success / fail / timeout
- step ADVANCE / FAIL / OFF_ROUTE
- death, bag free < 2, durability < 0.2, money_gate
- zone change, load screen
- “no XP for 8 minutes”
- human hotkey
- heartbeat every 15–30 s if nothing else (cheap “still ok?”)

### 9.2 Output contract

```json
{
  "goal": "advance:ally_human_012_hogger",
  "intent": "advance | skip | rejoin | grind_rib | service | escalate | wait",
  "skill": "GRIND_UNTIL",
  "params": {"path_id": "elwynn_east_12", "until": "step_or_level_12"},
  "abort_if": ["dead", "stuck_s>8", "zone!=Elwynn", "hp<0.2"],
  "confidence": 0.78,
  "why": "objective not ticking; use east grind rib 2 min then recheck Hogger"
}
```

Invalid plans get rejected by a **verifier** (rules, not Claude):

- skill exists
- params.zone == state.zone or skill is TRAVEL/HEARTH/FLIGHT
- not VENDOR while dead
- not ACCEPT while combat unless abort_if includes combat

### 9.3 Prompt contents (small)

- identity + output schema
- current `state_v1` trimmed (no raw pixels)
- `ACTIVE_STEP` + next 2 nodes + on_fail edges
- top-5 retrieved skills (name, success_rate, one-line desc)
- last 3 events
- last death postmortem if any (10 lines)

No 40k-token Wowhead dump.

### 9.4 Tools

- `retrieve_skills(q)`
- `get_step(id)` / `get_rib(zone, level)`
- `list_nearby_service(kind)`
- `propose_skill_draft(...)` (does not arm)
- `escalate_claude(reason, clip_id)`
- `mark_step_skippable(id, why)` (writes graph annotation, versioned)

### 9.5 Models

- Jev: cheapest model that can emit valid JSON reliably (fast). Quality < latency.
- Claude: queued, 1 worker, max N inflight. Used for draft skills, graph patches, 3-minute video-caption postmortems.
- Optional captioner: every N seconds store 1-line scene caption for RAG (“dead at lion’s pride, spirit run”).

Budget example: 10 clients × Jev 2/min × tiny prompt ≈ fine. 10 clients × Claude 2/min ≈ you are funding Anthropic’s dinner.

---

## 10. RAG and memory

Four collections, separate on purpose.

| Collection | Contents | Query |
|---|---|---|
| `static` | class spells by level, vendor coords, zone graph, boat timers | zone+level+class |
| `guide` | all nodes embedded by title+kind+zone | current step |
| `skills` | verified programs + rates | intent + zone + kind |
| `episodes` | death/stuck/success clips summaries | last error + zone |

Write policy for `skills`:

- draft → `proposed`
- success on 2 clients × 3 runs → `stable`
- success_rate < 0.4 over 20 → `retired`

Never let Jev retrieve `retired` unless Claude is in the loop.

Episode row (parquet, 2 Hz + events):

`t, client_id, state_v1, keys, skill, armed_by, reward_bits, frame_ref`

Frames: store 1–2 fps JPEG or embeddings, not 20 fps raw, or you will fill a disk before lunch.

---

## 11. Multi-client farm

### 11.1 Roles (10 seats)

- 4 × same class, same spine, staggered steps (on-policy)
- 2 × same class, other starter (generalize tracker)
- 2 × “chaos”: worse combat profile / no addon (recovery data)
- 2 × second class **or** human-shadow (hotkey takeover logs)

### 11.2 Supervisor

- launch/bind windows, restart capture if black
- detect “still at login / stuck in queue”
- pause client if gold/XP rate is insane (possible server exploit — do not learn it)
- rotate characters at ding-20 to keep the factory in the band you are currently authoring

### 11.3 Character ops

- Pre-create accounts. Do not automate Blizzard-style CAPTCHA.
- Bind hearth early (guide node).
- Naming: `Jev01`… so logs are readable.
- If the server is PvP: spine stays near guards until 20; flag-on is a fail policy.

---

## 12. Learning and weaning

### 12.1 Principle

Do not RL raw pixels on a live server.

Order:

1. Scripted skills + guide tracker (you get 1–12).
2. Voyager loop: Jev/Claude write skills, library grows (no weight updates).
3. Behavior-clone System 1 from *high-XP, low-death* slices (walk/face/loot).
4. Distill Jev intents: `state → intent+skill` classifier for a finished bracket.
5. Contextual bandit on **which rib** given congestion/time (optional).

### 12.2 Labels that make 4 possible

Every tick must know `armed_by ∈ {s1_preempt, tracker, jev, claude, human}`.

Train the wean classifier only on `{tracker, jev, human}` where the next 60 s did not death-loop.

### 12.3 Rewards

Primary: guide step advances / hour.  
Secondary: XP/h.  
Penalty: death, stuck>15s, off_route>60s, Claude call.

### 12.4 What weans vs what never weans

Wean: combat cadence, path stick, loot clicks, “vendor now?”, “eat now?”, “this step is done.”

Never wean in v1: new zone graph, boats, missing-quest surgery, novel elite, server event, UI layout change.

### 12.5 NitroGen-like policy (optional later)

A small vision→action net is System 1 replacement for WASD + look, not for 1–70 planning. Fine-tune only if you have 30+ clean hours. It will not replace the GuideGraph.

---

## 13. Class and economy (the silent 1–70 killers)

Handle as guide nodes, not hope.

- **Money gates:** spells, mount 30 (~90–100g TBC classic-ish; **use this server’s vendor price**), riding 75/150, later flying.
- When `gold < gate`: force grind rib with best g/h you have measured, not the next quest.
- **Reagents:** mage food/water (conjure skill), warlock shards, hunter ammo, rogue poisons (delay poisons to 20+).
- **Durability 0:** weapon break → nearest vendor even if off-spine.
- **Bags:** 6-slot starter hell; buy bags is a step, not an afterthought.
- **Trainer:** every 2 levels early, every 4 later. Skip if dead broke (flag `untrained_spells`).
- **Talents:** static template per class, applied at ding by a skill if the UI is readable; else human once.
- **Mount skill:** travel skills check `level>=30 and riding_known`.

Pick **Mage** first: ranged, conjure food/water, blink as stuck tool, no pet.

---

## 14. Failure taxonomy (design for these)

| Failure | Owner | Policy |
|---|---|---|
| Dead | S1 | release if far, corpse run polyline, rez, eat, rejoin step |
| Stuck on geometry | S1 | jump/strafe/back, then hearth |
| Drowning Wetlands | Guide | avoid node; if swimming hp drop → surface skill |
| Fear into extra pack | S1 | stop pull; nova/run profile |
| Quest missing | Tracker | on_fail grind/skip |
| Elite camped | Jev | skip after 2 deaths |
| Vendor closed / wrong gossip | Skill | retry list; escalate |
| Boat missed | Skill | wait timer; do not WASD off the dock |
| Loading / taxi | S1 | freeze plans until zone stable 3 s |
| Addon strip gone | Fusion | vision mode, grind-only nodes |
| Anticheat kick | Supervisor | stop farm, do not auto-recreate 50 accounts |
| XP exploit found | Supervisor | pause, do not distill |

Every death writes a 10 s clip + state + step_id. That is the most valuable dataset you will own.

---

## 15. Repo, team, clocks

### 15.1 Repo

```
jev/
  clients/      hwnd, capture, hid
  perceive/     radio decode, vision, fuse
  world/        state_v1
  guide/        graph json, tracker, recorder
  skills/       catalog + combat profiles + paths
  jev/          prompt, tools, verifier
  claude/       queue, templates
  rag/          indexes
  orch/         supervisor
  learn/        parquet, bc, distill
  content/tbc/  ally_human.json, horde_orc.json, ribs/
  eval/         dashboard
```

One `state_v1`. One `hid`. One `GuideGraph` version per server build.

### 15.2 If 3 people

- A: capture + hid + S1 combat + stuck
- B: radio addon + fusion + tracker + graphs
- C: Jev/Claude/RAG + dashboard + supervisor

Do not all touch combat JSON.

### 15.3 48-hour clock

**H0–4** One window: capture, hid, walk in a circle, swing key 1.  
**H4–10** Radio + decode + fused hp/xy. Combat profile. Loot. Eat.  
**H10–16** Tracker + 15-node Northshire/Elwynn graph. Corpse run. Vendor.  
**H16–24** Jev event loop. 3 clients. Dashboard.  
**H24–36** 6–10 clients, second zone or offset, Claude queue, episode parquet.  
**H36–48** Vision failover demo, wean metric plot even if classifier is dummy, freeze features, rehearse judges.

After event: author 12–30, then 30–70 ribs. Distill Elwynn intent model. Only then say “70.”

---

## 16. Eval board (always on)

Per client, 15-min rolling:

- level, xp/h, gold/h
- deaths/h, time-to-rez
- stuck events/h, off_route_s
- steps/h
- tick share S1 / Jev / Claude / human
- skill success table
- addon_ok %

**Bracket freeze rule:** a band is “scriptable” when 2 hours, deaths/h < 2, stuck>15s = 0, Claude < 3/h, steps moving.

Demo narrative: *tokens falling, steps still advancing.* That is the project.

---

## 17. Judge demo script (8 minutes)

1. Six windows, three at different steps on the same graph.
2. Kill one process; supervisor brings capture back; character resumes CORPSE or FOLLOW.
3. Toggle radio off on one client; it degrades to grind rib, does not freeze forever.
4. Show a skill that was `proposed` last night now `stable` in RAG.
5. Graph: Claude calls/h ↓ , steps/h → .
6. Say the sentence: “Jev does not press keys. Jev services a leveling program. Ten bodies generate the program’s memory.”

Do not promise a live 1–70 during Q&A. Show the spine file with empty ribs marked `TODO` so they see the road.

---

## 18. Explicit non-goals (v1)

- Dungeons, raids, BGs, GDKP, AH flipping
- Multi-boxing one human legally vs 10 brains (you have 10 agents, not ISBoxer)
- Pet-class excellence
- Full quest-text understanding
- End-to-end RL
- Official Blizzard realms
- Copying paid guide IP

---

## 19. Risks that look like intelligence but are not

- Bigger model on the mouse
- More YOLO classes instead of a better graph
- Ten independent Claude sessions
- “We’ll record paths later”
- Training overnight on death loops
- Quest-first on a custom server
- Ignoring mount gold
- Identical 10-client timing

---

## 20. The whole thing in one paragraph

Lock the UI. Paint TBC state. Fuse with vision. Drive a GuideGraph you recorded on this server. System 1 executes a small skill list at 30 Hz. Jev only arms skills and moves the playhead. Claude only writes when the playhead jams. Ten clients share deaths, skills, and step labels. Distill the playhead and the hands. Fill ribs until 70. The LLM is a supervisor with a library. The product is a leveling program that gets cheaper to run.

---

## 21. Immediate next artifacts to generate

1. `state_v1.py` pydantic + JSON schema  
2. `guide/ally_human_1_12.json` empty template + recorder hotkeys  
3. `skills/catalog.md` with success predicates  
4. `JevRadio` TOC + pixel map  
5. `jev/output_schema.json` + verifier rules  
6. Dashboard counters listed in §16  

Build those six before another model is discussed.
