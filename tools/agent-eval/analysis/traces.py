"""Print per-trial call traces for a batch: argv (shortened), exit code, error code/problem.

Usage: python analysis/traces.py <batch> [--task T09] [--model opus] [--failed-only]
Reads <work_dir>/runs/<batch>/plan.json and each trial's recorder log under <workspaces_dir>/<wid>/.
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "harness"))
from config import CFG  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")


def trials(batch):
    plan = json.loads((CFG.runs_dir / batch / "plan.json").read_text(encoding="utf-8"))
    for lane in plan["lanes"]:
        for t in lane["trials"]:
            yield lane["task"], t


def calls(wid):
    ws = CFG.workspaces_dir / wid
    p = ws / "calls.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        c = json.loads(line)
        body = ""
        for ext in (".out", ".err"):
            f = ws / "calls" / f"{c['id']}{ext}"
            if f.exists():
                body += f.read_text(encoding="utf-8", errors="replace")
        c["body"] = body
        out.append(c)
    return out


def err_info(body):
    try:
        j = json.loads(body.strip().splitlines()[0]) if body.strip().startswith("{") else None
    except Exception:
        j = None
    if not j:
        m = re.search(r'"code"\s*:\s*"(E_[A-Z0-9_]+)"', body)
        return (m.group(1) if m else "", "")
    e = j.get("error") or {}
    d = e.get("details") or {}
    return e.get("code", ""), d.get("problem", "") or ""


def short(argv):
    s = " ".join(argv)
    s = re.sub(r"--project[ =]acme/payments-service", "-P", s)  # the worked example's main project
    s = re.sub(r"ct2_[a-z2-7]{26}_[0-9a-f]{32}|ct_\d+_[0-9a-f]+", "<tok>", s)  # 2.0 and 1.x confirm tokens
    return s[:150]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("batch")
    ap.add_argument("--task")
    ap.add_argument("--model")
    ap.add_argument("--failed-only", action="store_true")
    a = ap.parse_args()
    for task, t in trials(a.batch):
        if a.task and not task.startswith(a.task):
            continue
        if a.model and t["model"] != a.model:
            continue
        cs = calls(t["wid"])
        print(f"== {t['wid']} {task} {t['model']} skill={t['skill']} calls={len(cs)}")
        for c in cs:
            if a.failed_only and c["exit"] == 0:
                continue
            code, prob = err_info(c["body"]) if c["exit"] != 0 else ("", "")
            print(f"   {c['exit']:>2} {code:<24} {prob:<22} {short(c['argv'])}")
