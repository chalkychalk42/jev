"""Food and drink as the runs record them (DECISIONS V550-V555): rests with food against without,
seconds a rest, restocks bought against given up, and potions drunk.

    python tools/provisions_report.py                                  # this checkout's runs
    python tools/provisions_report.py --runs /home/ash/JevHive/var/runs --since 20261009T0300
    python tools/provisions_report.py --runs ... --since ... --until ... --step 3

Each run's `executions.jsonl` and `skills.jsonl` (or their packed `.gz`) are read a line at a time
and only counters are kept: a rest is its `rest.summary` event (how each role was taken: from the
bar, the bags or neither, the body regenerating alone), a restock its BUY_AMMO_REAGENT_FOOD skill's
outcome, with the `provisions.*` and `bar.item` events beside them. Runs from before V550 carry no
summary; their rests are counted by operation alone.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _lines(path: Path):
    for candidate, opener in ((path, open), (path.with_name(path.name + ".gz"), gzip.open)):
        try:
            handle = opener(candidate, "rt", encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue
        with handle:
            try:
                for line in handle:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue
            except (EOFError, OSError):
                return
        return


def restock_kind(row: dict) -> str:
    """What became of a purchase: bought, or given up and why."""
    detail, outcome = row.get("detail") or "", row.get("outcome")
    if re.search(r"is a \d+-yard walk", detail):
        return "gave_up:too_far"
    if "supplier" in detail or "sells food" in detail:
        return "gave_up:no_supplier"
    if outcome == "succeeded":
        return "not_needed" if "hold food" in detail or "enough" in detail else "bought"
    if "poor" in detail or "purse" in detail:
        return "gave_up:too_poor"
    return f"failed:{outcome}"


def read_run(run: Path, counts: Counter) -> None:
    for row in _lines(run / "skills.jsonl"):
        if row.get("skill") == "BUY_AMMO_REAGENT_FOOD":
            rule = (row.get("rule") or "?").removeprefix("jev:")
            counts[("restock", rule, restock_kind(row))] += 1
    for row in _lines(run / "executions.jsonl"):
        name, phase = row.get("operation"), row.get("phase")
        data = row.get("data") or {}
        if name == "rest.summary":
            took = data.get("took") or {}
            fed = any(way in ("bar", "bags") for way in took.values())
            key = "+".join(f"{role}:{way}" for role, way in sorted(took.items()))
            seconds = data.get("seconds") or 0.0
            counts[("rest", "with food" if fed else "without", "n")] += 1
            counts[("rest", "with food" if fed else "without", "s")] += seconds
            counts[("rest_kind", key, "n")] += 1
            counts[("rest_kind", key, "s")] += seconds
            counts[("summaries",)] += 1
        elif name in ("rest", "rest.both") and phase == "end":
            counts[("rest_ops",)] += 1
            counts[("rest_ops_s",)] += row.get("duration_s") or 0.0
        elif name == "fight" and phase == "end" and row.get("code") == "killed":
            counts[("kills",)] += 1
        elif name in ("provisions.plan", "provisions.bought", "provisions.gave_up"):
            counts[(name, row.get("code") or "")] += 1
            if name == "provisions.bought":
                counts[("provisions.units",)] += data.get("units") or 0
        elif name == "bar.item":
            counts[("bar.item", row.get("code") or "")] += 1
        elif name == "fight.potion":
            counts[("fight.potion", data.get("kind") or "")] += 1
        elif name == "consume.request":
            counts[("press", data.get("role"))] += 1
    counts[("runs",)] += 1


def report(counts: Counter) -> str:
    kills = counts[("kills",)] or 1
    lines = [f"{counts[('runs',)]} runs, {counts[('kills',)]} kills, "
             f"{counts[('rest_ops',)]} rests ({counts[('rest_ops_s',)] / kills:.1f} s a kill), "
             f"{counts[('summaries',)]} with a summary (V550)", "",
             "| rests | n | s a rest | s a kill |", "|---|---:|---:|---:|"]
    for fed in ("with food", "without"):
        n, s = counts[("rest", fed, "n")], counts[("rest", fed, "s")]
        if n:
            lines.append(f"| {fed} | {n} | {s / n:.1f} | {s / kills:.2f} |")
    lines += ["", "| how each role was taken | n | s a rest |", "|---|---:|---:|"]
    kinds = sorted({k[1] for k in counts if k[0] == "rest_kind"},
                   key=lambda k: -counts[("rest_kind", k, "n")])
    for kind in kinds:
        n = counts[("rest_kind", kind, "n")]
        lines.append(f"| {kind} | {n} | {counts[('rest_kind', kind, 's')] / n:.1f} |")
    lines += ["", "| restocks: rule | outcome | n |", "|---|---|---:|"]
    for key in sorted(k for k in counts if k[0] == "restock"):
        lines.append(f"| {key[1]} | {key[2]} | {counts[key]} |")
    extra = sorted(k for k in counts if k[0] in ("provisions.plan", "provisions.bought",
                                                  "provisions.gave_up", "bar.item", "press",
                                                  "fight.potion"))
    if extra:
        lines += ["", "| event | code | n |", "|---|---|---:|"]
        lines += [f"| {k[0]} | {k[1]} | {counts[k]} |" for k in extra]
        lines.append(f"| provisions.units | | {counts[('provisions.units',)]} |")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=Path, default=ROOT / "runs")
    parser.add_argument("--since", default="")
    parser.add_argument("--until", default="~")
    parser.add_argument("--step", type=int, default=1, help="read one run in this many")
    args = parser.parse_args()
    names = sorted(p.name for p in args.runs.iterdir() if args.since <= p.name < args.until)
    counts: Counter = Counter()
    for name in names[::max(1, args.step)]:
        if (args.runs / name).is_dir():
            read_run(args.runs / name, counts)
    print(report(counts))


if __name__ == "__main__":
    main()
