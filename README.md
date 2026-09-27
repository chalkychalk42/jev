# Jev

A guide-directed leveling agent for a private TBC 2.4.3 server. It plays unattended: a session
loop, generated quest guides, scripted skills that verify their own effects, and choices
learned from outcomes. It needs no model call to make progress.

```
Body       bounded     HID/skills   local         execute input and verify its effect
Tracker    250 ms      predicates   local         guide progress, interrupts, services
Policy     per tick    rules        local         the scripted floor: each objective's own routine
Choices    per visit   outcomes     local         Thompson-learned options, danger map, route memory
Tutor      on failure  vision       subscription  an objective whose routine failed, rarely
```

The guide supplies the objective; the body plays it with scripted routines and proves each
effect by what the addon paints. Where a routine has real alternatives (which hunt station,
when to heal), the choice is learned from how each option paid off. The tutor takes an
objective only after its routine has failed. See the current contract at the top of
**[ARCHITECTURE.md](ARCHITECTURE.md)** and **[docs/OPERATING.md](docs/OPERATING.md)** to run it.

## Read these in order

| | |
|---|---|
| **[ARCHITECTURE.md](ARCHITECTURE.md)** | how it is built: the current contract first, then the history |
| **[docs/OPERATING.md](docs/OPERATING.md)** | how it runs unattended, how to watch it, and how a change goes live |
| **[DECISIONS.md](DECISIONS.md)** | every call made, with its reason and evidence. Read before proposing an alternative |
| **[STATUS.md](STATUS.md)** | what was measured, when: the latest entry is the current state |
| [docs/ROADMAP.md](docs/ROADMAP.md) | ordered work and acceptance gates |
| [docs/PLAN.md](docs/PLAN.md) | the original vision |
| [docs/TEACHING_LOOP.md](docs/TEACHING_LOOP.md) | the visual teaching loop of 22 September (history: its students went with V225) |
| [data/README.md](data/README.md) | what is on disk and where it came from |

## Quickstart

```bash
uv venv --python 3.12 && uv pip install -e ".[dev]"
.venv/bin/python -m pytest          # no game required
.venv/bin/python tools/gen_addon_fields.py            # add --install to put it in the client
```

The wire format, the state contract, the situation key, the verifier and the reward
function are all testable with no client, no capture and no network. A count is left out
of this file on purpose — it goes stale in a day and a stale number in a README is a
small lie you tell yourself every time you read it. `STATUS.md` carries the current one.

Live, the bot has played for forty-eight hours unattended: a paladin from level 13 to 15.87,
then a mage the campaign tool made itself, from level 1 onwards (`STATUS.md`).

```bash
.venv/bin/python -m jev.run.cli --check  # graph/capability report; never attaches a client
```

The live entry point is the session loop (`tools/session_loop.sh`), which runs Windows Python
`tools/start_teaching.py --run --dispatch hybrid` for each session: it attaches the client and
sends input. [Operating instructions](docs/OPERATING.md) cover starting, pausing and stopping
it, the characters, what it learns, and how a change goes live.

### Platform split
The brain is pure Python and runs anywhere. Capture and input are Windows-only — they
need `user32`/`gdi32` and a real window — so a live client runs under Windows Python
against the repo, while development, tests and the simulator run anywhere.

## Layout

```
jev/world/      state_v1 — the contract everything crosses a process boundary as
jev/perceive/   JevRadio field table and codec, vision heads, fusion
jev/coach/      decision schema, verifier, situation_key
jev/teacher/    the queue, dedup and prompt templates
jev/play/       the tutor's bounded controls and observed effects (the corpus is recorded)
jev/learn/      outcome-learned choices, danger map, episode corpus
jev/guide/      GuideGraph generation, tracker, recorder
jev/skills/     skill catalog, combat profiles, paths
jev/clients/    window binding, capture, HID
jev/orch/       shared coach/tracker runtime and decision recording
jev/run/        live client composition, body worker, supervisor, CLI
addons/JevRadio state painted into pixels; Fields.lua is generated, never edited; the
                client gets one built file under a neutral name (build/addon/)
tools/          codegen and one-off scripts
data/           knowledge packs (gitignored, see data/README.md)
```

## The environment this targets

CMaNGOS TBC 2.4.3 built from source at `~/cmangos`, a 2.4.3 build 8606 client at
`C:\Games\WoW243`. Offline server only — no live realms, no injection, no memory
access. Control is virtual HID; sense is screen capture plus an addon that paints state
and never actuates.
