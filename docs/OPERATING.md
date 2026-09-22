# Continuous runs and learning

The client has not been opened for this build. These commands distinguish offline
verification from the later live acceptance run. The run loop, vendor transactions,
reconnect composition and model promotion still need representative live evidence.

## Offline

```bash
.venv/bin/python -m jev.run.cli --check --route-mode supported \
  --learn --policy-mode adaptive --teacher --reconnect
.venv/bin/python -m jev.learn.worker --runs runs --store var/learning --once
.venv/bin/pytest
```

`--check` reads the graph and saved completion facts, reports missing capabilities and
the explicit route exclusions, and exits before capture, input, teacher calls or training.
The supported Human route currently contains 31 quest chains. The source graph remains
available with `--route-mode full`; excluded quests are never marked completed.
Exploration, gameobjects and unsupported service branches are listed in the manifest.
The graph declares its navigation coordinate frame. Player and corpse observations
are converted through measured map bounds when the actual region changes, including
Elwynn/Stormwind transitions; raw map coordinates and the actual region are retained.
Unknown regions or a different continent supply no usable navigation position.
New observations declare State schema 2; original schema 1 corpus remains readable.
Legacy guides without a declared frame must be regenerated before live execution.

The worker can run continuously without a game client:

```bash
.venv/bin/python -m jev.learn.worker --runs runs --store var/learning
```

It grades mature observed windows, keeps unfinished tails pending, excludes synthetic
and replay examples by default, and splits training/evaluation by entire run. Only
new eligible evidence produces a versioned candidate. Defaults require 200 training
examples from three training runs, with additional runs held out. An OS lock prevents
two workers training into the same store. Interrupting the process preserves published
models and the last atomic registry; it does not promote an incomplete artifact.

The first pass on the existing 28 run directories completed without errors but yielded
zero eligible examples. Older unlinked or short records do not acquire invented outcome
credit. No model has been promoted from that corpus.

## Later live acceptance

Install the generated schema 7 addon when client testing is authorized. Follow
[VENDOR.md](VENDOR.md) for regeneration and files; `Supplies.lua` is required by the TOC.
Schema 6 captures still decode, with new inventory observations explicitly unknown.
Use Windows Python with the `eyes` and `learn` dependencies installed, the existing
path sidecar configured, and the intended character selected.

```text
python tools/probe_slice.py --route-mode supported --run-for 14400
```

This first run uses the scripted floor, records outcomes, and exercises the service and
session boundaries. Add `--reconnect` to use the existing measured login/character
screen sequence. Credentials come from `JEV_WOW_ACCOUNT` and `JEV_WOW_PASSWORD` in the
environment or the repo `.env`; environment values take precedence. They are never
printed by the run CLI. Unknown screens receive no guessed keypresses.

Once ready to collect shadow and teacher evidence, the complete composition is:

```text
python tools/probe_slice.py --route-mode supported --run-for 14400 --learn --policy-mode adaptive --teacher --reconnect
```

`--teacher-binary` selects the already authenticated Claude subscription executable;
`--teacher-model` defaults to `sonnet`. The existing subscription transport is used,
with no API key dependency. Teacher calls remain off the body thread, with a persistent
12-attempt/hour budget and at least 60 seconds between calls. Budget/store failure,
rate limiting, stale replies or rejected actions leave the scripted floor in charge.
Artifact proposals remain in decision records; receiving a proposal does not execute
code or install a graph patch.

`--policy-mode shadow` is the default and never lets learned predictions drive.
`adaptive` permits evidence-gated trials; it does not bypass evidence requirements.
A candidate first needs supported actions and observed outcomes on held-out runs,
then may receive a 10% canary for at most 24 hours. Promotion needs fresh outcomes
attributed to that model, adequate independent windows and runs, measured improvement,
and no regression in route-specific progress, XP, death or stall measures. Decisions
are scoped to level bracket and graph revision. Missing evidence blocks promotion.
Learned actions use current guide facts; emergency, combat, service and recovery rules
retain priority. An incompatible prediction abstains. Model faults and measured
regressions restore the prior verified model or the scripted floor.

## Files and stops

Each run records `route.json` with the selected graph digest and exclusions, plus the
existing ticks, decisions, skill results and derived grades. `var/learning/` contains
immutable candidate artifacts, `registry.json`, worker state, `latest-cycle.json`,
teacher budget reservations and `live-<client-id>.json`. The live status is a snapshot;
its timestamp must be checked before treating it as current.

Playhead replacement is atomic. Completed quest IDs survive graph changes; absence
from the quest log alone never manufactures historical completion. Non-default client
IDs receive separate playhead files. A process-held input lock shared across checkouts
prevents a second supervisor for the same user from attaching while the first owns input.

The watchdog gives perception a 20-second grace period, bounds reconnect attempts,
and stops after 15 minutes without quest or experience progress. Movement and guide
retry cycles do not reset that deadline. Stops preserve the playhead, release inputs,
cancel teacher subprocesses and close recording. Tune `--blind-grace`, `--no-progress`
and `--reconnect-limit` explicitly if later measurements justify it.

Named limitations remain: wandering NPC interaction, focus versus unstick measurement,
in-combat Holy Light timing, live Windows scheduling and stop acceptance, and the
pre-existing unverified camera calibration change. Vendor scope is exact starting
food/drink and confirmed junk; equipment, ammo/reagents and changing action bars are
not inferred. A depleted character too weak to reach a supplier can still stop with
`no_food`. A finished selected graph ends the run. This build does not claim infinite
content coverage or guaranteed continuous improvement.
