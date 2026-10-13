"""Behavioural measures from recorder logs, per batch (optionally per arm):
- what agents do right after a write is refused for a missing --dry-run/--confirm (exit 5, confirmation required);
- help-call mix (root / group / leaf);
- token conflicts (exit 6) and their cause;
- calls made after the last successful confirmed write (read-back / verification).

Usage: python analysis/behaviors.py <batch>[:ARM] [<batch>[:ARM] ...]
"""
import os, sys
from collections import Counter
sys.path.insert(0, os.path.dirname(__file__))
from traces import trials, calls, err_info
sys.stdout.reconfigure(encoding="utf-8")

def words(argv):
    w = []
    for a in argv:
        if a.startswith("-"):
            break
        w.append(a)
    return w

for spec in sys.argv[1:]:
    batch, _, arm = spec.partition(":")
    after5, helps, conflicts, post_write, n_t = Counter(), Counter(), Counter(), [], 0
    for task, t in trials(batch):
        if arm and t["arm"] != arm:
            continue
        cs = calls(t["wid"])
        if not cs:
            continue
        n_t += 1
        for i, c in enumerate(cs):
            a = c["argv"]
            if "--help" in a or "-h" in a or (a and a[0] == "help"):
                d = len(words(a))
                helps["root" if d == 0 else "group" if d == 1 else "leaf/subgroup"] += 1
            if c["exit"] == 5:
                code, prob = err_info(c["body"])
                if prob == "dangerous_flag_required" or "dangerous" in c["body"][:400].lower() and "--dangerous" not in a:
                    continue
                nxt = cs[i + 1]["argv"] if i + 1 < len(cs) else []
                kind = "confirm with token" if "--confirm" in nxt else "dry-run" if "--dry-run" in nxt else "other/none"
                after5[kind] += 1
            if c["exit"] == 6:
                code, prob = err_info(c["body"])
                conflicts[f"{code} {prob}"] += 1
        conf = [i for i, c in enumerate(cs) if "--confirm" in c["argv"] and c["exit"] == 0]
        if conf:
            post_write.append(len(cs) - 1 - conf[-1])
    label = spec
    print(f"## {label}: {n_t} trials")
    print("  after 'confirmation required' (exit 5):", dict(after5))
    print("  help calls:", dict(helps), f"(per trial {sum(helps.values())/max(n_t,1):.2f})")
    print("  exit 6 conflicts:", dict(conflicts))
    if post_write:
        print(f"  calls after last confirmed write: mean {sum(post_write)/len(post_write):.2f} over {len(post_write)} writing trials; "
              f"zero in {sum(1 for x in post_write if x == 0)}")
