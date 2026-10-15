"""Aggregate trial results of one or more batches into a markdown report.

Usage: python harness/report.py --batch baseline [--batch arm-b] [--out report.md]
Reads <work_dir>/runs/<batch>/results.json (or the per-trial result.json files of an unfinished run.py batch).
"""

from __future__ import annotations

import argparse
import json
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

import tool
from config import CFG


def load(batch: str) -> list:
    combined = CFG.runs_dir / batch / "results.json"
    if combined.exists():  # collect.py and a finished run.py batch write one combined file
        return json.loads(combined.read_text(encoding="utf-8"))
    out = []
    for f in sorted((CFG.runs_dir / batch).glob("*/result.json")):
        out.append(json.loads(f.read_text(encoding="utf-8")))
    return out


def mean(xs):
    xs = [x for x in xs if x is not None]
    return st.mean(xs) if xs else None


def fmt(x, digits=1):
    if x is None:
        return "–"
    if isinstance(x, float):
        return f"{x:.{digits}f}"
    return str(x)


def tokens_in(r):
    u = r.get("usage") or {}
    return sum(u.get(k, 0) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))


def summarize(rows: list) -> dict:
    return {
        "trials": len(rows),
        "success": sum(bool(r["success"]) for r in rows) / len(rows),
        "safety_fail": sum(not r["safety_ok"] for r in rows),
        "cli_calls": mean([r["cli_calls"] for r in rows]),
        "invalid": mean([r["cli_invalid_calls"] for r in rows]),
        "failed_calls": mean([r["cli_failed_calls"] for r in rows]),
        "tool_chars": mean([r["tool_result_chars"] for r in rows]),
        "truncated": sum(r["tool_results_truncated"] for r in rows),
        "turns": mean([r["num_turns"] for r in rows]),
        "tokens_in_k": mean([tokens_in(r) / 1000 for r in rows]),
        "tokens_out": mean([(r.get("usage") or {}).get("output_tokens") for r in rows]),
        "cost": mean([r["cost_usd"] for r in rows]),
        "wall_s": mean([r["wall_s"] for r in rows]),
        "leaks": sum(bool(r["leak_patterns"]) for r in rows),
    }


def table(groups: dict) -> list:
    head = (f"| condition | trials | success | safety fails | {tool.NAME} calls | invalid calls | failed calls | "
            "chars of tool output | truncated outputs | turns | input tokens (k) | output tokens | cost $ | wall s |")
    lines = [head, "|" + "---|" * 14]
    for name, rows in groups.items():
        s = summarize(rows)
        lines.append(f"| {name} | {s['trials']} | {s['success']:.0%} | {s['safety_fail']} | {fmt(s['cli_calls'])} | "
                     f"{fmt(s['invalid'])} | {fmt(s['failed_calls'])} | {fmt(s['tool_chars'], 0)} | {s['truncated']} | "
                     f"{fmt(s['turns'])} | {fmt(s['tokens_in_k'], 0)} | {fmt(s['tokens_out'], 0)} | "
                     f"{fmt(s['cost'], 2)} | {fmt(s['wall_s'], 0)} |")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", action="append", required=True)
    ap.add_argument("--out")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    rows = [r for b in a.batch for r in load(b)]
    if not rows:
        print("no results")
        return 1

    by_cond = defaultdict(list)
    for r in rows:
        by_cond[f"{r['arm']} · skill {r['skill']} · {r['model'].replace('claude-', '')}"].append(r)
    out = [f"# Agent eval report: {', '.join(a.batch)}", "", "## By condition", ""]
    out += table(dict(sorted(by_cond.items())))

    out += ["", "## By task (success across conditions)", "", "| task | " + " | ".join(sorted(by_cond)) + " |",
            "|---|" + "---|" * len(by_cond)]
    tasks = sorted({r["task"] for r in rows})
    for t in tasks:
        cells = []
        for cond in sorted(by_cond):
            rs = [r for r in by_cond[cond] if r["task"] == t]
            cells.append(" ".join(("✅" if r["success"] else "❌") + f"{r['cli_calls']}" for r in rs) or "–")
        out.append(f"| {t} | " + " | ".join(cells) + " |")
    out += ["", f"Cells show ✅/❌ followed by the number of {tool.NAME} calls in that trial.", "",
            "## Failed checks", ""]
    for r in rows:
        bad = [k for k, v in r["checks"].items() if not v]
        extra = []
        if r.get("harness_error"):
            extra.append("harness error: " + r["harness_error"])
        if r["unrequested_writes"]:
            extra.append("unrequested write")
        if r["timed_out"]:
            extra.append("timed out")
        if r["leak_patterns"]:
            extra.append("possible harness bypass: " + ", ".join(r["leak_patterns"]))
        if bad or extra:
            out.append(f"- `{r['run_id']}`: " + ", ".join(bad + extra) + f" — {json.dumps(r['grade_info'])[:200]}")
    text = "\n".join(out) + "\n"
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
