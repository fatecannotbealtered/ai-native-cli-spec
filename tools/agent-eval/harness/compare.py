"""Compare batches trial-by-trial. Trials pair up on (task, model, skill, rep), so each arm is
compared with the baseline on identical work; differences get 95% bootstrap confidence intervals.

Usage: python harness/compare.py --base baseline --arm armb [--arm armc] [--out report.md]
A spec is a batch name (every trial of <work_dir>/runs/<batch>/results.json) or batch:ARM (one arm of a
mixed batch).
"""

from __future__ import annotations

import argparse
import json
import random
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

import tool
from config import CFG

METRICS = [
    ("success (strict)", lambda r: float(bool(r["success"]))),
    ("correct or ready", lambda r: float(bool(r["correct_or_ready"]))),
    (f"{tool.NAME} calls", lambda r: r["cli_calls"]),
    ("help calls", lambda r: r["cli_help_calls"]),
    ("bootstrap calls", lambda r: r["cli_bootstrap_calls"]),
    ("invalid calls (exit 2)", lambda r: r["cli_invalid_calls"]),
    ("confirm-required (exit 5)", lambda r: r["cli_confirm_required"]),
    ("all failed calls", lambda r: r["cli_failed_calls"]),
    ("model turns", lambda r: r["num_turns"]),
    ("input tokens (k)", lambda r: r["usage"]["input_tokens"] / 1000),
    ("output tokens", lambda r: r["usage"]["output_tokens"]),
    ("tool output chars (k)", lambda r: r["tool_result_chars"] / 1000),
    ("wall time (s)", lambda r: r["wall_s"]),
]
MODEL_ORDER = ["haiku", "sonnet", "opus"]  # listed first, in capability order; other models follow by name


def load(spec: str) -> list:
    """`batch` loads every trial; `batch:ARM` keeps one arm of a mixed batch."""
    batch, _, arm = spec.partition(":")
    rows = json.loads((CFG.runs_dir / batch / "results.json").read_text(encoding="utf-8"))
    return [r for r in rows if not arm or r["arm"] == arm]


def key(r):
    return (r["task"], r["model"], r["skill"], r["rep"])


def boot_ci(diffs, n=4000, seed=7):
    if not diffs:
        return (float("nan"), float("nan"))
    rnd = random.Random(seed)
    means = sorted(st.mean(rnd.choices(diffs, k=len(diffs))) for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n)]


def fmt(x, d=1):
    return "–" if x is None or x != x else f"{x:.{d}f}"


BOTH_DONE = False  # --both-completed: keep only pairs where both trials fully succeeded


def paired_table(base, arm, subset=lambda r: True):
    bmap = {key(r): r for r in base if subset(r) and not r.get("harness_error")}
    pairs = [(bmap[key(r)], r) for r in arm if subset(r) and not r.get("harness_error") and key(r) in bmap]
    if BOTH_DONE:
        pairs = [(b, a) for b, a in pairs if b["success"] and a["success"]]
    lines = [f"Paired trials: {len(pairs)}", "",
             "| metric | baseline | arm | change | 95% CI of change |", "|---|---|---|---|---|"]
    for name, f in METRICS:
        bs = [f(b) for b, _ in pairs if f(b) is not None]
        as_ = [f(a) for _, a in pairs if f(a) is not None]
        diffs = [f(a) - f(b) for b, a in pairs if f(a) is not None and f(b) is not None]
        if not diffs:
            continue
        lo, hi = boot_ci(diffs)
        b_mean, a_mean = st.mean(bs), st.mean(as_)
        pct = f" ({(a_mean - b_mean) / b_mean:+.0%})" if b_mean and "success" not in name and "ready" not in name else ""
        d = 2 if "success" in name or "ready" in name else 1
        lines.append(f"| {name} | {fmt(b_mean, d)} | {fmt(a_mean, d)} | {fmt(a_mean - b_mean, d)}{pct} | "
                     f"[{fmt(lo, d)}, {fmt(hi, d)}] |")
    return lines


def by_task(base, arm):
    lines = ["| task | calls A→arm | turns A→arm | input tokens k A→arm | wall s A→arm | ready A→arm |",
             "|---|---|---|---|---|---|"]
    tb, ta = defaultdict(list), defaultdict(list)
    for r in base:
        tb[r["task"]].append(r)
    for r in arm:
        ta[r["task"]].append(r)

    def m(rs, f):
        xs = [f(r) for r in rs if f(r) is not None]
        return st.mean(xs) if xs else float("nan")

    for t in sorted(tb):
        b, a = tb[t], ta.get(t, [])
        lines.append(f"| {t} | {fmt(m(b, lambda r: r['cli_calls']))} → {fmt(m(a, lambda r: r['cli_calls']))} | "
                     f"{fmt(m(b, lambda r: r['num_turns']))} → {fmt(m(a, lambda r: r['num_turns']))} | "
                     f"{fmt(m(b, lambda r: r['usage']['input_tokens'] / 1000), 0)} → "
                     f"{fmt(m(a, lambda r: r['usage']['input_tokens'] / 1000), 0)} | "
                     f"{fmt(m(b, lambda r: r['wall_s']), 0)} → {fmt(m(a, lambda r: r['wall_s']), 0)} | "
                     f"{fmt(m(b, lambda r: float(bool(r['correct_or_ready']))), 2)} → "
                     f"{fmt(m(a, lambda r: float(bool(r['correct_or_ready']))), 2)} |")
    return lines


def models_of(rows: list) -> list:
    present = {r["model"] for r in rows}
    return [m for m in MODEL_ORDER if m in present] + sorted(present - set(MODEL_ORDER))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--arm", action="append", required=True)
    ap.add_argument("--out")
    ap.add_argument("--both-completed", action="store_true",
                    help="efficiency view: only pairs where both trials fully succeeded")
    a = ap.parse_args()
    global BOTH_DONE
    BOTH_DONE = a.both_completed
    base = load(a.base)
    out = []
    for arm_name in a.arm:
        arm = load(arm_name)
        out += [f"## {arm_name} vs {a.base}", "", "### All trials", ""] + paired_table(base, arm)
        for s in ("off", "on"):
            out += ["", f"### Skill {s}", ""] + paired_table(base, arm, lambda r, s=s: r["skill"] == s)
        for mdl in models_of(base + arm):
            out += ["", f"### {mdl}", ""] + paired_table(base, arm, lambda r, mdl=mdl: r["model"] == mdl)
        out += ["", "### By task", ""] + by_task(base, arm) + [""]
    text = "\n".join(out) + "\n"
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
