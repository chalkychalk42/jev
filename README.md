# Jev

A guide-directed leveling agent for a private TBC 2.4.3 server, built so that **it gets
cheaper to run the longer it runs**.

```
System 1   33-50 ms   scripted     free       move, face, swing, loot, eat
Tracker    250 ms     predicates   free       step done? off route? timed out?
Coach      0.2-2 Hz   policy       free       arm a skill, move the playhead
Teacher    queued     Claude/GLM   expensive  resolve ambiguity, write skills
Learner    nightly    training     free       grade, retrain, promote
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
| [docs/PLAN.md](docs/PLAN.md) | the original vision |
| [data/README.md](data/README.md) | what is on disk and where it came from |

## Quickstart

```bash
uv venv --python 3.12 && uv pip install -e ".[dev]"
.venv/bin/python -m pytest          # 108 tests, no game required
.venv/bin/python tools/gen_addon_fields.py
```

The wire format, the state contract, the situation key, the verifier and the reward
function are all testable with no client, no capture and no network.

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
jev/orch/       supervisor
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
