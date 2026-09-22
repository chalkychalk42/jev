# Continuous runs and learning

For the new visual tutor and motor learner, use
[TEACHING_LOOP.md](TEACHING_LOOP.md). `--play-mode teach` gives Jev bounded actions inside
the guide objective; `adaptive` permits evaluated motor handover. The instructions below
describe the earlier scripted/strategic-policy mode, which remains available.

Live acceptance started on 22 September 2026. Schema 7 deployment, reconnect, camera
reset, repair, water restocking, concurrent recording and the Windows learner have been exercised. See
`STATUS.md` for measured outcomes and remaining failures; wiring alone does not prove
unattended play or improvement.

## Offline

```bash
.venv/bin/python -m jev.run.cli --check --route-mode supported \
  --learn --policy-mode adaptive --teacher --reconnect
.venv/bin/python -m jev.learn.worker --once
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
.venv/bin/python -m jev.learn.worker
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

## Supervised live acceptance

The generated schema 7 addon is installed in the current Windows client. Follow
[VENDOR.md](VENDOR.md) for regeneration and files; `Supplies.lua` is required by the TOC.
Schema 6 captures still decode, with new inventory observations explicitly unknown.
Use Windows Python with the `eyes` and `learn` dependencies installed, the existing
path sidecar configured, and the intended character selected.

```text
python tools/probe_slice.py --route-mode supported --run-for 300 --retries 1 --screenshots --stop-file captures/STOP-test
```

This first run uses the scripted floor, records outcomes, and exercises the service and
session boundaries. Add `--reconnect` to use the existing measured login/character
screen sequence. Credentials come from `JEV_WOW_ACCOUNT` and `JEV_WOW_PASSWORD` in the
environment or the repo `.env`; environment values take precedence. They are never
printed by the run CLI. Unknown screens receive no guessed keypresses.

To collect shadow and teacher evidence during the supervised test:

```text
python tools/probe_slice.py --route-mode supported --run-for 300 --retries 1 --screenshots --stop-file captures/STOP-test --learn --policy-mode shadow --teacher --reconnect
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
existing ticks, decisions, skill results and derived grades. The learning store contains
immutable candidate artifacts, `registry.json`, worker state, `latest-cycle.json`,
teacher budget reservations and `live-<client-id>.json`. The live status is a snapshot;
its timestamp must be checked before treating it as current.

The live command prints its resolved learning store. Linux and native Windows checkouts
use `var/learning/`. Windows Python running from a UNC checkout uses
`%LOCALAPPDATA%\Jev\learning\<checkout-name>-<stable-digest>`; for this checkout it is
`C:\Users\NAS\AppData\Local\Jev\learning\foreverv2-3c309d13f4025150`.
Windows byte-range locks fail on the WSL UNC share with `EINVAL`, so the live worker,
registry and teacher budget stay together on a native drive. Locks are never bypassed.
The standalone learner uses the same checkout and platform defaults. `--learning-store`
overrides the live default; `--store` overrides the standalone worker's default.
The repo's earlier Linux store remains separate from the native Windows store. Use Windows
Python for both entrypoints when continuing the live corpus in the same native store.

During tests, `--screenshots` writes a lossless PNG every second under the run's
`screenshots/` directory, starting before initial focus and reconnect. `manifest.jsonl` records capture timestamps, missing frames,
write duration and any skipped slots. Review the sequence while the test runs. Encoding
and writes run off the input thread; storage failure stops the test. Create the exact
`--stop-file` path to request cooperative shutdown and input release, including during login. An existing stop
file prevents attachment; remove it intentionally before another test, or use a new
path. Ctrl-C uses the same cleanup path.

Playhead replacement is atomic. Completed quest IDs survive graph changes; absence
from the quest log alone never manufactures historical completion. Non-default client
IDs receive separate playhead files. A process-held input lock shared across checkouts
prevents a second supervisor for the same user from attaching while the first owns input.

The watchdog gives perception a 20-second grace period, bounds reconnect attempts,
and stops after 15 minutes without quest or experience progress. Movement and guide
retry cycles do not reset that deadline. Stops preserve the playhead, release inputs,
cancel teacher subprocesses and close recording. Tune `--blind-grace`, `--no-progress`
and `--reconnect-limit` explicitly if later measurements justify it.

The first serviced wolf run stopped after two approaches landed no hits. Its screenshots
showed the unit locator pairing a wolf nameplate with yellow grass. Colour and hollow
shape alone did not identify the ring, and simpler proximity/width gates still produced
false clicks on the recorded frames. This remains a blocker to unattended wolf progress;
repair and restocking success do not establish a successful hunt.

Named limitations remain: wandering NPC interaction, focus versus unstick measurement,
in-combat Holy Light timing and sustained Windows scheduling/stop acceptance. Vendor scope is exact starting
food/drink and confirmed junk; equipment, ammo/reagents and changing action bars are
not inferred. A depleted character too weak to reach a supplier can still stop with
`no_food`. A finished selected graph ends the run. This build does not claim infinite
content coverage or guaranteed continuous improvement.
