"""One table of per-arm means for the report.

Usage: python analysis/summary_table.py LABEL=BATCH[:ARM] [LABEL=BATCH[:ARM] ...]
  e.g. python analysis/summary_table.py "A=b0" "A′ (interleaved)=b1:A" "N (interleaved)=b1:N"
Each column holds every trial of <work_dir>/runs/BATCH/results.json, or of one ARM of a mixed batch.
"""
import json
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "harness"))
import tool  # noqa: E402
from config import CFG  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")


def load(spec):
    b, _, arm = spec.partition(":")
    rows = json.loads((CFG.runs_dir / b / "results.json").read_text(encoding="utf-8"))
    return [r for r in rows if not arm or r["arm"] == arm]


M = [("strict success", lambda r: float(bool(r["success"])), 2),
     ("correct or ready", lambda r: float(bool(r["correct_or_ready"])), 2),
     (f"{tool.NAME} calls", lambda r: r["cli_calls"], 1),
     ("invalid calls", lambda r: r["cli_invalid_calls"], 2),
     ("help calls", lambda r: r["cli_help_calls"], 1),
     ("model turns", lambda r: r["num_turns"], 1),
     ("input tokens (k)", lambda r: r["usage"]["input_tokens"] / 1000, 0),
     ("output tokens", lambda r: r["usage"]["output_tokens"], 0),
     ("tool output (k chars)", lambda r: r["tool_result_chars"] / 1000, 1),
     ("wall time (s)", lambda r: r["wall_s"], 0)]


def cell(rows, f, d):
    xs = [x for x in (f(r) for r in rows) if x is not None]
    return f"{st.mean(xs):.{d}f}" if xs else "–"


if __name__ == "__main__":
    specs = sys.argv[1:]
    if not specs or any("=" not in s for s in specs):
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    data = []
    for spec in specs:
        label, _, source = spec.partition("=")
        try:
            data.append((label, load(source)))
        except FileNotFoundError:
            data.append((label, []))
    print("| metric | " + " | ".join(n for n, _ in data) + " |")
    print("|---|" + "---|" * len(data))
    for name, f, d in M:
        print(f"| {name} | " + " | ".join(cell(rs, f, d) for _, rs in data) + " |")
    print("| trials | " + " | ".join(str(len(rs)) for _, rs in data) + " |")
