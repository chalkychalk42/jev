#!/usr/bin/env python3
"""What sessions' fights cost, by the most attackers counted in them: the fights won, the
health and seconds each took, the fights lost, and how many of the roots pressed the client
answered.

    python tools/fight_costs.py 268 279        # sessions FIRST to LAST (captures/live-N.log)

Read-only: a session's log names its run, whose ticks and executions are read here. Nothing
here attaches to a client. It is how the root at every first contact was found to save single
fights 2% of health for 4 s more (V275), and one root in nine to go unanswered (V282).
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from session_report import RUNS, _jsonl, _run_of, session_log  # noqa: E402

# How long after a root's press its answer is looked for, until the next press: its slot
# cooling or its mana gone. Before V282 the first look came after the two-second step clear.
ANSWER_S = 4.0
# A fall in the mana share at least this large after a root's press is its cost.
ROOT_SPENT = 0.05
# A fight that ends with less than this share of mana left ran dry.
DRY_MANA = 0.1


def fights(run: Path) -> list[dict]:
    """Each fight won or lost in a run: the most attackers counted, the share of health lost,
    the seconds it took, whether a root was pressed in it, and the share of mana left at its
    end (`None` for a character without mana)."""
    vitals = [((t.get("state") or {}).get("t"), (t.get("state") or {}).get("vitals") or {})
              for t in _jsonl(run / "ticks.jsonl")]
    hp = [(t, v.get("hp")) for t, v in vitals
          if isinstance(t, (int, float)) and isinstance(v.get("hp"), (int, float))]
    power = [(t, v.get("power")) for t, v in vitals
             if isinstance(t, (int, float)) and isinstance(v.get("power"), (int, float))]
    rows = list(_jsonl(run / "executions.jsonl"))
    looks = [(r["t"], (r.get("data") or {}).get("combat.attackers") or 0)
             for r in rows if r.get("operation") == "combat.observed"]
    roots = [r["t"] for r in rows if r.get("operation") == "ability.request"
             and (r.get("data") or {}).get("role") == "root"]
    begun = {r.get("operation_id"): r["t"] for r in rows
             if r.get("operation") == "fight" and r.get("phase") == "begin"}
    out = []
    for r in rows:
        if r.get("operation") != "fight" or r.get("phase") != "end":
            continue
        # A death the supervisor saw first cancels the fight: that fight was lost too.
        died = r.get("code") == "died" or (r.get("code") == "exception"
                                           and "dead or ghost" in (r.get("detail") or ""))
        if r.get("code") != "killed" and not died:
            continue
        start, end = begun.get(r.get("operation_id")), r["t"]
        if start is None:
            continue
        health = [h for t, h in hp if start <= t <= end]
        if len(health) < 2:
            continue
        out.append({
            "attackers": max([a for t, a in looks if start <= t <= end] or [0]),
            "lost": health[0] - min(health),
            "seconds": end - start,
            "died": died,
            "rooted": any(start <= t <= end for t in roots),
            "mana": next((m for t, m in reversed(power) if t <= end + 1.0), None),
        })
    return out


def roots(run: Path) -> tuple[int, int]:
    """The roots pressed in a run, and how many the client answered within `ANSWER_S`."""
    rows = sorted(_jsonl(run / "executions.jsonl"), key=lambda r: r["t"])
    looks = [r for r in rows if r.get("operation") == "combat.observed"]
    presses = [r["t"] for r in rows if r.get("operation") == "ability.request"]
    answered = pressed = 0
    for r in rows:
        data = r.get("data") or {}
        if r.get("operation") != "ability.request" or data.get("role") != "root":
            continue
        pressed += 1
        bit = 1 << (data["slot"] - 1)
        before = [(look.get("data") or {}).get("vitals.power") for look in looks
                  if r["t"] - 1.0 <= look["t"] <= r["t"]]
        power = before[-1] if before else None
        until = min([t for t in presses if t > r["t"]] + [r["t"] + ANSWER_S])
        for look in looks:
            if not r["t"] < look["t"] <= until:
                continue
            seen = look.get("data") or {}
            ready, now = seen.get("bars.ready"), seen.get("vitals.power")
            if ((isinstance(ready, int) and not ready & bit)
                    or (isinstance(power, (int, float)) and isinstance(now, (int, float))
                        and power - now >= ROOT_SPENT)):
                answered += 1
                break
    return answered, pressed


def _row(label: str, won: list[dict]) -> str:
    if not won:
        return f"  {label:<10} {0:>5}"
    lost = statistics.median(f["lost"] for f in won)
    seconds = statistics.median(f["seconds"] for f in won)
    rooted = sum(f["rooted"] for f in won)
    mana = [f["mana"] for f in won if f.get("mana") is not None]
    left = f"{statistics.median(mana):>10.2f} {sum(m < DRY_MANA for m in mana):>4}" if mana else ""
    return f"  {label:<10} {len(won):>5} {lost:>12.2f} {seconds:>8.0f} {rooted:>6} {left}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("first", type=int)
    parser.add_argument("last", type=int)
    args = parser.parse_args(argv)
    all_fights, answered, pressed = [], 0, 0
    for number in range(args.first, args.last + 1):
        log = session_log(number)
        run = _run_of(log) if log.exists() else None
        if run is None:
            continue
        all_fights += fights(RUNS / run)
        a, p = roots(RUNS / run)
        answered, pressed = answered + a, pressed + p
    won = [f for f in all_fights if not f["died"]]
    died = [f for f in all_fights if f["died"]]
    print(f"sessions {args.first}-{args.last}: {len(won)} fights won, {len(died)} lost")
    print(f"  {'attackers':<10} {'won':>5} {'health lost':>12} {'seconds':>8} {'roots':>6} "
          f"{'mana left':>10} {'dry':>4}")
    print(_row("0-1", [f for f in won if f["attackers"] <= 1]))
    print(_row("2+", [f for f in won if f["attackers"] >= 2]))
    crowded = sum(f["attackers"] >= 2 for f in died)
    dry = sum(f.get("mana") is not None and f["mana"] < DRY_MANA for f in died)
    print(f"  lost: {len(died)} ({crowded} with two attackers or more, {dry} out of mana)")
    print(f"  roots answered: {answered} of {pressed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
