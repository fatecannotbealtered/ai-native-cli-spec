"""Between-trial steps for write lanes, run by a helper agent inside the trial workflow.

  python lane.py step <batch> [--post WID] [--prep WID]
  python lane.py reset [--tasks T05-comment,T09-delete-branch]

step --post WID  snapshot the upstream-state checks of WID's task into <workspaces_dir>/WID/state.json
                 (before anything resets it)
step --prep WID  reset the task's upstream objects so WID starts from the seeded state
reset            run the reset of every write task (or of the listed tasks), e.g. after the last batch

Prints exactly one final line: "STEP OK ..." or "STEP FAILED: <reason>".
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import traceback

from config import CFG
from tasks import BY_ID, TASKS, Ctx


def trial(batch: str, wid: str) -> dict:
    plan = json.loads((CFG.runs_dir / batch / "plan.json").read_text(encoding="utf-8"))
    for lane in plan["lanes"]:
        for t in lane["trials"]:
            if t["wid"] == wid:
                return t
    raise KeyError(f"{wid} not in plan {batch}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["step", "reset"])
    ap.add_argument("batch", nargs="?")
    ap.add_argument("--post")
    ap.add_argument("--prep")
    ap.add_argument("--tasks", default="all", help="reset: comma-separated task ids (default: every write task)")
    a = ap.parse_args()
    try:
        ctx = Ctx()
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        if a.cmd == "reset":
            ids = [t.id for t in TASKS if t.kind == "write"] if a.tasks == "all" else a.tasks.split(",")
            for tid in ids:
                BY_ID[tid].reset(ctx)
            print(f"STEP OK reset={','.join(ids)}")
            return 0
        if not a.batch:
            raise ValueError("step needs a batch")
        if a.post:
            task = BY_ID[trial(a.batch, a.post)["task"]]
            checks, info = task.grade(ctx, "", {"calls": []})
            state = {k: v for k, v in checks.items() if k in task.state_keys}
            (CFG.workspaces_dir / a.post / "state.json").write_text(
                json.dumps({"captured_at": now, "checks": state, "info": info}, indent=2), encoding="utf-8")
        if a.prep:
            BY_ID[trial(a.batch, a.prep)["task"]].reset(ctx)
            (CFG.workspaces_dir / a.prep / "prepped.json").write_text(json.dumps({"reset_at": now}), encoding="utf-8")
        print(f"STEP OK post={a.post or '-'} prep={a.prep or '-'}")
        return 0
    except Exception as e:
        traceback.print_exc()
        print(f"STEP FAILED: {e!r}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
