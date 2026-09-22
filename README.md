# Jev

A guide-directed leveling agent for a private TBC 2.4.3 server, built so that **it gets
cheaper to run the longer it runs**.

```
Body       bounded    HID/skills   local      execute and release input
Tracker    250 ms     predicates   local      guide progress, interrupts, services
Jev tutor  per action vision       subscription choose, observe, correct
Student    per action learned     local      evaluated capabilities, abstain on novelty
Learner    background training    local      filter, evaluate, canary, promote, roll back
```

The guide supplies the objective; Jev can choose bounded actions using screenshots,
controls and exact-server knowledge. The body executes and verifies effects. Successful
observed episodes train a local student that gradually takes over evaluated capabilities.
The existing scripted mode remains available. See the current
**[teaching-loop contract and test launcher](docs/TEACHING_LOOP.md)**.

## Read these in order

| | |
|---|---|
| **[ARCHITECTURE.md](ARCHITECTURE.md)** | how it is built, and why it differs from the plan |
| **[docs/TEACHING_LOOP.md](docs/TEACHING_LOOP.md)** | current visual teaching loop, continuous collection and per-capability handover |
| **[DECISIONS.md](DECISIONS.md)** | every call made, with its reason. Read before proposing an alternative |
| **[docs/ROADMAP.md](docs/ROADMAP.md)** | current ordered work, evidence gaps and acceptance gates toward sustained improvement |
| [docs/PLAN.md](docs/PLAN.md) | the original vision |
| [data/README.md](data/README.md) | what is on disk and where it came from |

## Quickstart

```bash
uv venv --python 3.12 && uv pip install -e ".[dev]"
.venv/bin/python -m pytest          # no game required
.venv/bin/python tools/gen_addon_fields.py
```

The wire format, the state contract, the situation key, the verifier and the reward
function are all testable with no client, no capture and no network. A count is left out
of this file on purpose — it goes stale in a day and a stale number in a README is a
small lie you tell yourself every time you read it. `STATUS.md` carries the current one.

The live body has completed individual quest, combat, travel, loot and service checks.
The current engagement chain still needs live proof. The visual tutor/student loop is
built and tested offline; that does not establish learned game competence. See `STATUS.md`
for measured outcomes and the teaching-loop document for the next acceptance sequence.

```bash
.venv/bin/python -m jev.run.cli --check  # graph/capability report; never attaches a client
.venv/bin/python -m jev.run.cli --check --play-mode teach --route-mode supported
.venv/bin/python -m jev.learn.grade runs/<run-id> --closed  # derive outcome grades
```

On Windows, `python tools/probe_slice.py` remains the live entry point and delegates to
`jev.run.cli`. It attaches the client and can send input. It uses the scripted floor by
default; `--play-mode teach` enables visual tuition and `--play-mode adaptive` permits
evaluated student capabilities. Synthetic and replay examples are excluded from training
by default. [Operating instructions](docs/OPERATING.md) cover the explicit supported
route, evidence gates, reconnect, files and remaining live acceptance work.

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
jev/play/       visual tutor, bounded controls, observed effects and motor learning
jev/learn/      episode store, grading, distillation
jev/guide/      GuideGraph generation, tracker, recorder
jev/skills/     skill catalog, combat profiles, paths
jev/clients/    window binding, capture, HID
jev/orch/       shared coach/tracker runtime and decision recording
jev/run/        live client composition, body worker, supervisor, CLI
jev/eval/       dashboard counters
addons/JevRadio state painted into pixels; Fields.lua is generated, never edited
tools/          codegen and one-off scripts
data/           knowledge packs (gitignored, see data/README.md)
```

## The environment this targets

CMaNGOS TBC 2.4.3 built from source at `~/cmangos`, a 2.4.3 build 8606 client at
`C:\Games\WoW243`. Offline server only — no live realms, no injection, no memory
access. Control is virtual HID; sense is screen capture plus an addon that paints state
and never actuates.
