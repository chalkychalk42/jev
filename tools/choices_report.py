"""What the choice points have learned (`jev.learn.choices`, DECISIONS V158).

    python tools/choices_report.py              # every choice point, busiest first
    python tools/choices_report.py --top 20     # more objectives per point

Reads `var/choices.json` only; nothing here attaches to a client.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

from jev.learn.choices import PRIOR_VISITS, ChoiceMemory, pace, pooled

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--memory", type=Path, default=ROOT / "var" / "choices.json")
    parser.add_argument("--top", type=int, default=8)
    args = parser.parse_args()
    memory = ChoiceMemory(args.memory)
    for point in sorted(memory.points):
        arms = memory.arms(point)
        objectives = defaultdict(dict)
        for key, arm in arms.items():
            objective, _, option = key.partition("@")
            objectives[objective][option] = arm
        print(f"{point}: {len(objectives)} objectives, {len(arms)} options, "
              f"{sum(a.tries for a in arms.values())} tries")
        busiest = sorted(objectives.items(), key=lambda kv: -sum(a.tries for a in kv[1].values()))
        for objective, options in busiest[:args.top]:
            rate, pooled_s = pooled(options.values()), pace(options.values()) or 1.0
            tries = sum(a.tries for a in options.values())
            # Payoffs a second, each shrunk toward the pool as the draw is (V310).
            ranked = sorted(options.items(), key=lambda kv: -(
                (kv[1].wins + PRIOR_VISITS * rate) / (kv[1].seconds + PRIOR_VISITS * pooled_s)))
            best = ", ".join(f"{a.wins:g}/{a.tries:g} in {a.seconds / max(a.tries, 1):.0f} s"
                             for _, a in ranked[:3])
            barren = sum(1 for a in options.values() if a.tries >= 3 and a.wins == 0)
            print(f"  {objective:28s} {len(options):3d} options {tries:4g} tries "
                  f"pays off {rate:4.0%} in {pooled_s:.0f} s | best a second {best} | "
                  f"barren (3+ tries, none) {barren}")


if __name__ == "__main__":
    main()
