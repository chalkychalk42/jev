#!/usr/bin/env python3
"""Measure what the client draws around a **hostile** unit.

    /mnt/c/forever-win/Scripts/python.exe tools/probe_reaction.py

`jev/perceive/units.py` has one measured rule — friendly — and two written by symmetry
that it refuses to use. That refusal is not caution for its own sake: switching the
hostile rule on produced a confident ring on a patch of Northshire terrain, because its
thresholds were a guess nobody had checked against a frame.

This checks them. The method is a difference, not a search, because searching for a colour
is the thing that needs the colour:

    capture with nothing targeted
    Tab                             the nearest attackable unit, a keybind, not chat
    the radio says what it is       `target.reaction`, so the label comes from the client
    capture again
    difference                      the selection ring is what appeared

The ring is then sampled from the pixels that changed, and the nameplate bar is measured
from the same frame by shape — a solid run far wider than it is tall — which needs no
colour either. Both come back as distributions, so the thresholds that go into `_RULES`
are read off a histogram rather than rounded off a guess.

A frame is saved either way, so the rule that comes out of this has a regression test.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
ROOT = pathlib.Path(__file__).resolve().parent.parent

from jev.clients import win32  # noqa: E402
from jev.guide.coords import bounds_by_radio_id  # noqa: E402
from jev.guide.graph import Graph  # noqa: E402
from jev.guide.path import MmapQuery  # noqa: E402
from jev.perceive.radio_frame import _blobs, _reaction  # noqa: E402
from jev.run.client import NotRunning, attach, with_travel  # noqa: E402

INTERFACE_Y = 120
INTERFACE_X = 560


def describe(name: str, pixels: np.ndarray) -> None:
    """A channel-wise summary, which is what a threshold is actually made of."""
    if pixels.size == 0:
        print(f"  {name}: nothing")
        return
    a = pixels.reshape(-1, 3).astype(int)
    print(f"  {name}: {len(a)} px")
    for i, ch in enumerate("RGB"):
        col = a[:, i]
        print(f"    {ch}: min {col.min():3}  p10 {np.percentile(col, 10):5.0f}  "
              f"median {np.median(col):5.0f}  p90 {np.percentile(col, 90):5.0f}  "
              f"max {col.max():3}")
    hi, lo = a.max(axis=1), a.min(axis=1)
    print(f"    spread (max-min per px): median {np.median(hi - lo):.0f}")


def bars(frame: np.ndarray) -> list:
    """Nameplate-shaped blobs, found by shape alone.

    Saturated in any hue, solid, and far wider than tall. Colour is deliberately not part
    of the test — it is the thing being measured.
    """
    a = frame.astype(np.int16)
    hi = a.max(axis=2)
    lo = a.min(axis=2)
    mask = (hi > 120) & (hi - lo > 60)
    mask[:INTERFACE_Y, :INTERFACE_X] = False
    out = []
    for b in _blobs(mask):
        if b.w < 40 or b.h > 12:
            continue
        if b.area / max(1.0, b.w * b.h) < 0.75:
            continue
        out.append(b)
    out.sort(key=lambda b: -b.w)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(ROOT / "tests" / "fixtures"))
    ap.add_argument("--tabs", type=int, default=1, help="how many Tab presses")
    ap.add_argument("--node", default=None,
                    help="graph node to walk to first, e.g. a quest_objective")
    ap.add_argument("--arrive", type=float, default=12.0,
                    help="arrival radius; a camp is a place, not a point")
    ap.add_argument("--mmaps", default="/home/ash/cmangos/run/bin/mmaps")
    ap.add_argument("--jevpath", default="/home/ash/ForeverV2/tools/jevpath/jevpath")
    args = ap.parse_args()

    try:
        client = attach("reaction")
    except NotRunning as e:
        print(e)
        return 2
    if not client.focused():
        print("could not bring the client to the foreground")
        return 1

    if args.node:
        v = client.read()
        if v is None:
            print("cannot read the strip")
            client.close()
            return 1
        bounds = bounds_by_radio_id(
            str(ROOT / "data" / "zones-tbc-243.json"))[v["pos.zone_id"]]
        graph = Graph.load(str(ROOT / "content" / "tbc" / "ally_human_1_12.json"))
        node = graph.get(args.node)
        if node is None or node.world is None:
            print(f"no placeable node {args.node!r}")
            client.close()
            return 1
        launcher = ("wsl.exe", "-d", "Ubuntu-24.04", "-e") if win32.IS_WINDOWS else ()
        with_travel(client, bounds,
                    MmapQuery(args.jevpath, args.mmaps, launcher=launcher),
                    arrival_yards=args.arrive, say=print)
        print(f"walking to {node.notes} for {node.objectives[0]}")
        if not client.approach(node.world):
            print("could not get there; measuring from here anyway")

    cap = client.cap
    hid = client.hid
    read = client.read

    before = cap.grab().rgb.copy()
    v = read()
    if v is None:
        print("cannot read the strip")
        return 1
    if v.get("target.has") is True:
        print("something is already targeted; the difference needs a clean start")

    for _ in range(max(1, args.tabs)):
        hid.tap("tab")
        time.sleep(0.5)
    after = cap.grab().rgb.copy()

    v = read()
    if v is None or v.get("target.has") is not True:
        print("Tab selected nothing; stand nearer something attackable")
        return 1
    reaction = v.get("target.reaction")
    name_id = v.get("target.name_id")
    # `UnitReaction` is 1-8, not a reaction name. The decoder already owns that mapping.
    decoded = _reaction(reaction)
    label = decoded.value if decoded is not None else "unknown"
    print(f"targeted: reaction={label} name_id={name_id} "
          f"level={v.get('target.level')} hp={v.get('target.hp')}")

    # What appeared. The selection ring is drawn only for the current target, so it is
    # exactly the thing the difference isolates.
    delta = np.abs(after.astype(int) - before.astype(int)).sum(axis=2)
    appeared = delta > 60
    appeared[:INTERFACE_Y, :INTERFACE_X] = False
    ys, xs = np.nonzero(appeared)
    if len(ys) == 0:
        print("nothing changed outside the interface; no ring to measure")
    else:
        print(f"\nchanged region: x {xs.min()}-{xs.max()}  y {ys.min()}-{ys.max()}")
        describe("ring (pixels that appeared)", after[ys, xs])

    print()
    for b in bars(after)[:4]:
        px = after[int(b.cy), max(0, int(b.cx) - b.w // 3):int(b.cx) + b.w // 3]
        describe(f"bar {b.w}x{b.h} at ({b.cx:.0f},{b.cy:.0f})", px)

    out = pathlib.Path(args.out) / f"live-{label}-targeted.npz"
    np.savez_compressed(out, before=before, after=after)
    print(f"\nsaved {out} ({out.stat().st_size // 1024} KB)")
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
