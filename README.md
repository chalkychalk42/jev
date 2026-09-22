# Jev

A guide-directed leveling agent for a private TBC 2.4.3 server, built so that **it gets
cheaper to run the longer it runs**.

```
System 1   33-50 ms   scripted     free       move, face, swing, loot, eat
Tracker    250 ms     predicates   free       step done? off route? timed out?
Coach      0.2-2 Hz   policy       free       arm a skill, move the playhead
Teacher    queued     Claude/GLM   expensive  resolve ambiguity, write skills
Learner    background training    free       grade, evaluate, canary, promote
```

The teacher never presses a key and is never on the hot path. It answers the small
fraction of moments no rule can settle, and everything it answers becomes training data
for the policy that replaces it. The demo sentence is *tokens falling, steps still
advancing*.

## Read these in order

| | |
|---|---|
| **[ARCHITECTURE.md](ARCHITECTURE.md)** | how it is built, and why it differs from the plan |
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
The shared runtime has now passed supervised Windows repair, water purchase, travel,
recording and stop checks. Wolf targeting still admits grass as a selection ring and
blocks sustained quest progress. See `STATUS.md` for measured outcomes and
[the roadmap](docs/ROADMAP.md) for the remaining build and acceptance sequence.

```bash
.venv/bin/python -m jev.run.cli --check  # graph/capability report; never attaches a client
.venv/bin/python -m jev.learn.grade runs/<run-id> --closed  # derive outcome grades
```

On Windows, `python tools/probe_slice.py` remains the live entry point and delegates to
`jev.run.cli`. It attaches the client and can send input. It uses the scripted floor by
default; optional background learning, shadow/adaptive policy and the bounded teacher
queue share the same runtime. Synthetic and replay examples are excluded from training
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
