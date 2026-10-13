"""Build fleet-audit.json from the gate-audit workflows' journals and print the VER-007 summary table.

Usage:
  python audit_table.py --journal <run dir>/journal.jsonl [--journal <run dir>/journal.jsonl ...] fleet-audit.json
      rebuild the results file from the journals (it is overwritten), then print the table
  python audit_table.py fleet-audit.json
      print the table of an existing results file

A journal is the journal.jsonl in a Workflow run's transcript directory. Each agent's label says where its
result goes: map:<tool> and verify:<tool> from fleet-confirm-binding-audit.js, trace:<tool> from
fleet-update-injection-check.js. Pass every journal of the release audit in one call.
"""
import argparse
import json
import sys
from pathlib import Path

KINDS = ("map", "verify", "trace")


def read_journals(paths: list) -> dict:
    """{tool: {map, verify, trace}} from the result events of the given journals (later runs win)."""
    res = {}
    for path in paths:
        events = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
        labels = {e.get("key"): e.get("label", "") for e in events if e.get("type") == "started"}
        for e in events:
            if e.get("type") != "result":
                continue
            kind, _, cli = (labels.get(e.get("key")) or e.get("label") or "").partition(":")
            if kind not in KINDS or not cli:
                continue
            r = e.get("result")
            if isinstance(r, str):
                try:
                    r = json.loads(r)
                except ValueError:
                    continue
            if isinstance(r, dict):
                res.setdefault(cli, {})[kind] = r
    return res


def table(res: dict) -> list:
    lines = ["| CLI | version | binding | write cmds | gaps confirmed (high/med/low) | refuted | uncertain | missed+ "
             "| other issues confirmed | update |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    tot = {"conf": 0, "ref": 0, "unc": 0, "miss": 0, "other": 0}
    for cli in sorted(res):
        m, v, t = res[cli].get("map"), res[cli].get("verify"), res[cli].get("trace")
        update = (t or {}).get("verdict", "–")
        if not m:
            if t:
                lines.append(f"| {cli} | | | | | | | | | {update} |")
            continue
        gaps = m.get("gaps", [])
        verdicts = (v or {}).get("gap_verdicts", [])
        same_len = v is not None and len(verdicts) == len(gaps)

        def sev_of(i, x):
            # skeptics keep the mapper's gap order (and sometimes reword the command); else match by name
            if same_len:
                return gaps[i].get("severity", "medium")
            return next((g.get("severity", "medium") for g in gaps if x["command"].startswith(g["command"])), "medium")

        conf = [(i, x) for i, x in enumerate(verdicts) if x.get("verdict") == "confirmed"]
        ref = [x for x in verdicts if x.get("verdict") == "refuted"]
        unc = [x for x in verdicts if x.get("verdict") == "uncertain"]
        miss = [x for x in (v or {}).get("missed", []) if x.get("verdict") == "confirmed"]
        other = [x for x in (v or {}).get("other_issue_verdicts", []) if x.get("verdict") == "confirmed"]
        hs = {"high": 0, "medium": 0, "low": 0}
        for i, x in conf:
            hs[sev_of(i, x)] += 1
        for x in miss:
            hs[x.get("severity", "medium")] += 1
        ver = str(m.get("version", "")).split(" ")[0]
        style = (m.get("mechanism") or {}).get("binding_style", "")
        lines.append(f"| {cli} | {ver} | {style} | {len(m.get('write_commands', []))} | {len(conf) + len(miss)} "
                     f"({hs['high']}/{hs['medium']}/{hs['low']}) | {len(ref)} | {len(unc)} | {len(miss)} | "
                     f"{len(other)} | {update} |" + ("" if v else " (verify missing)"))
        tot["conf"] += len(conf) + len(miss)
        tot["ref"] += len(ref)
        tot["unc"] += len(unc)
        tot["miss"] += len(miss)
        tot["other"] += len(other)
    lines.append("")
    lines.append(f"totals: {tot}")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results", help="fleet-audit.json (written when --journal is given, else read)")
    ap.add_argument("--journal", action="append", default=[], help="a workflow run's journal.jsonl (repeatable)")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    out = Path(a.results)
    if a.journal:
        res = read_journals(a.journal)
        out.write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"{out}: {len(res)} tools")
    elif out.exists():
        res = json.loads(out.read_text(encoding="utf-8"))
    else:
        print(f"{out} does not exist; build it with --journal", file=sys.stderr)
        return 2
    print("\n".join(table(res)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
