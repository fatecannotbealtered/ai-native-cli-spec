"""Tally failed calls (exit != 0) by error code/problem and by normalized command pattern.

Usage: python analysis/failures.py <batch> [<batch> ...]
"""
import os, sys
from collections import Counter
sys.path.insert(0, os.path.dirname(__file__))
from traces import trials, calls, err_info
sys.stdout.reconfigure(encoding="utf-8")

def norm(argv):
    # keep command words up to the first flag; mark flags by name only
    words, flags = [], []
    for a in argv:
        if a.startswith("-"):
            flags.append(a.split("=")[0])
        elif not flags:
            words.append(a)
    return " ".join(words[:4]) + ("  " + " ".join(sorted(set(flags))) if flags else "")

for batch in sys.argv[1:]:
    by_code = Counter()
    pats = Counter()
    n_trials = n_calls = 0
    for task, t in trials(batch):
        cs = calls(t["wid"])
        if not cs:
            continue
        n_trials += 1
        n_calls += len(cs)
        for c in cs:
            if c["exit"] == 0:
                continue
            code, prob = err_info(c["body"])
            by_code[(c["exit"], code, prob)] += 1
            pats[(code, prob, norm(c["argv"]))] += 1
    print(f"#### {batch}: {n_trials} trials, {n_calls} calls")
    for (ex, code, prob), n in by_code.most_common():
        print(f"  {n:4}  exit {ex} {code} {prob}")
    print("  -- top patterns")
    for (code, prob, p), n in pats.most_common(30):
        print(f"  {n:4}  {code:<24} {prob:<24} {p[:110]}")
