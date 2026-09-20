"""The always-on eval board (PLAN §16), rendered as plain text.

Terminal output, not a web page. The board's job is to be readable on the machine that is
running the clients, over ssh, in a log file and in a screenshot pasted into a postmortem,
and to still be there when the display server, the browser or the chart library is not.
Sixty characters of fixed-width text satisfies all of that; a dashboard satisfies none of
it and needs maintaining on the day a run is going wrong.

`unresolved/h` is given the top block and a full line to itself because it is the number
the project is judged by (`ARCHITECTURE.md` §1). Everything else on this board can look
excellent while that number climbs, and a layout that lets it hide among twenty other
rates is a layout that will let it hide.

Unknown prints as a dash, never as zero. A rate that was not measured and a rate that was
measured at zero are opposite findings.
"""

from __future__ import annotations

import argparse
import pathlib
import time
from collections.abc import Sequence

from jev.eval.counters import (
    ARMED_BY_ORDER,
    FREEZE_SPAN_S,
    WINDOW_S,
    Agreement,
    Counters,
    FreezeVerdict,
    Streams,
    clients,
    compute,
    freeze_verdict,
    load_run,
)

WIDTH = 78
DASH = "-"
"""What an unmeasured number looks like. One character, so a column of them is obvious."""


def _n(v: float | None, fmt: str = "{:.1f}") -> str:
    return DASH if v is None else fmt.format(v)


def _pct(v: float | None) -> str:
    return DASH if v is None else f"{v:.1f}%"


def _dur(v: float | None) -> str:
    if v is None:
        return DASH
    if v < 90:
        return f"{v:.0f}s"
    return f"{v / 60:.1f}m"


def _rule(ch: str = "=") -> str:
    return ch * WIDTH


def _bar(frac: float, width: int = 18) -> str:
    """A share bar. Not a chart — a chart needs a library, a browser and a decision about
    colour, and this needs to survive being pasted into a text file."""
    filled = round(frac * width)
    return "#" * filled + "." * (width - filled)


def headline(c: Counters) -> list[str]:
    """The block that has to be impossible to skim past."""
    share = (c.unresolved / c.ticks * 100.0) if c.ticks else None
    lines = [
        _rule("="),
        f"  UNRESOLVED/h   {_n(c.unresolved_per_h):>10}     <- the number that must fall",
        _rule("="),
        f"    {c.unresolved} decisions no rule could settle"
        f"  ({_pct(share)} of {c.ticks} ticks)",
        f"    cost: teacher {_n(c.teacher_calls_per_h)}/h"
        f"   saved: {c.cache_hits} cached, {c.deduped} deduped",
    ]
    if share is not None and share >= 30.0:
        # ARCHITECTURE.md §1 names the number at which the diagnosis changes. Saying it
        # out loud on the board is cheaper than the week spent swapping models first.
        lines.append("    at this share the problem is PERCEPTION, not intelligence")
    return lines


def progress_block(c: Counters) -> list[str]:
    lvl = DASH if c.level is None else f"{c.level}"
    xp = DASH if c.xp_pct is None else f"{c.xp_pct * 100:.0f}%"
    return [
        f"  PROGRESS     level {lvl} ({xp})   bracket {c.bracket or DASH}"
        f"   levels/h {_n(c.levels_per_h, '{:.2f}')}",
        f"               steps/h {_n(c.steps_per_h)}"
        f"   gold {_n(c.gold, '{:.2f}')} ({_n(c.gold_per_h, '{:+.2f}')}/h)",
    ]


def safety_block(c: Counters) -> list[str]:
    return [
        f"  SAFETY       deaths/h {_n(c.deaths_per_h)}"
        f"   time-to-rez {_dur(c.time_to_rez_s)} (n={c.rez_samples})",
        f"               stuck>{15:g}s events {c.stuck_long_events}"
        f"   stuck/h {_n(c.stuck_events_per_h)}   off-route {_dur(c.off_route_s)}",
        f"  SENSE        addon_ok {_pct(c.addon_ok_pct)}",
    ]


def share_block(c: Counters) -> list[str]:
    lines = ["  TICK SHARE"]
    for name in ARMED_BY_ORDER:
        frac = c.tick_share.get(name, 0.0)
        lines.append(f"    {name:<12} {_bar(frac)} {frac * 100:5.1f}%")
    return lines


def agreement_block(c: Counters) -> list[str]:
    def one(label: str, a: Agreement) -> str:
        return (
            f"    {label:<12} {_pct(None if a.rate is None else a.rate * 100)}"
            f"   n={a.samples}   unmeasurable={a.unmeasurable}"
        )

    return [
        "  AGREEMENT    shadow vs teacher (Gate C wants >= 90% on a bracket)",
        one("intent", c.intent_agreement),
        one("skill", c.skill_agreement),
    ]


def skill_block(c: Counters) -> list[str]:
    if not c.skills:
        return ["  SKILLS       none armed in this window"]
    lines = ["  SKILLS       skill                  graded   ok    rate   ungraded"]
    for s in c.skills:
        rate = DASH if s.rate is None else f"{s.rate * 100:.0f}%"
        lines.append(
            f"    {s.skill:<24} {s.attempts:>6} {s.successes:>5} {rate:>7}"
            f" {s.ungraded_episodes:>10}"
        )
    return lines


def freeze_block(v: FreezeVerdict) -> list[str]:
    if v.scriptable:
        return [f"  FREEZE       bracket {v.bracket or DASH}: SCRIPTABLE"]
    lines = [f"  FREEZE       bracket {v.bracket or DASH}: not yet"]
    lines.extend(f"                 - {why}" for why in v.failures())
    return lines


def render(c: Counters, *, freeze_span_s: float = FREEZE_SPAN_S) -> str:
    """One client, one window, top to bottom."""
    observed = f"{c.span_s / 60:.1f}m observed"
    head = (
        f" client {c.client_id}   window {c.window_s / 60:.0f}m ({observed})"
        f"   {time.strftime('%H:%M:%S', time.localtime(c.last_t)) if c.last_t else DASH}"
    )
    parts = [
        head,
        *headline(c),
        "",
        *progress_block(c),
        *safety_block(c),
        "",
        *share_block(c),
        "",
        *agreement_block(c),
        "",
        *skill_block(c),
        "",
        *freeze_block(freeze_verdict(c, min_span_s=freeze_span_s)),
        _rule("-"),
    ]
    return "\n".join(parts)


def render_all(
    streams: Streams,
    *,
    now: float | None = None,
    window_s: float = WINDOW_S,
    freeze_span_s: float = FREEZE_SPAN_S,
) -> str:
    """Every client that produced a tick, one block each.

    No fleet-wide totals. Promotion is per bracket and health is per client
    (`ARCHITECTURE.md` §4, §9); a farm average hides the one client that has been dead in
    a ditch for ten minutes, which is the only client the board exists to find.
    """
    names = clients(streams)
    if not names:
        return "no ticks in the store yet"
    return "\n".join(
        render(compute(streams, client_id=n, now=now, window_s=window_s),
               freeze_span_s=freeze_span_s)
        for n in names
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jev.eval.board",
        description="Print the eval board for one or more run directories.",
    )
    parser.add_argument("runs", nargs="+", type=pathlib.Path)
    parser.add_argument("--window-min", type=float, default=WINDOW_S / 60)
    parser.add_argument(
        "--freeze-h",
        type=float,
        default=FREEZE_SPAN_S / 3600,
        help="span the bracket freeze rule requires, in hours",
    )
    args = parser.parse_args(argv)

    streams = Streams([], [], [])
    for d in args.runs:
        streams = streams + load_run(d)
    print(
        render_all(
            streams,
            window_s=args.window_min * 60.0,
            freeze_span_s=args.freeze_h * 3600.0,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
