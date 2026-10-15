"""Between-trial steps for write lanes, run by a helper agent inside the trial workflow.

  python lane.py step <batch> [--post WID] [--prep WID]
  python lane.py reset [--tasks T05-comment,T09-delete-branch]

step --post WID  stop WID's fault proxy, if it has one, and copy its log to <workspaces_dir>/WID/fault-proxy.json;
                 then snapshot the upstream-state checks of WID's task into <workspaces_dir>/WID/state.json
                 (before anything resets it)
step --prep WID  reset the task's upstream objects so WID starts from the seeded state; for a lost-reply trial
                 (plan.json "fault_proxy"), start the fault proxy and point WID's run-config.json at it
reset            run the reset of every write task (or of the listed tasks), e.g. after the last batch

Prints exactly one final line: "STEP OK ..." or "STEP FAILED: <reason>".
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
import traceback
from pathlib import Path

import faultproxy
import tool
from config import CFG
from tasks import BY_ID, TASKS, Ctx

PROXY_MAX_SECONDS = 2 * 3600  # a proxy whose --post never ran stops by itself after this long


def trial(batch: str, wid: str) -> dict:
    plan = json.loads((CFG.runs_dir / batch / "plan.json").read_text(encoding="utf-8"))
    for lane in plan["lanes"]:
        for t in lane["trials"]:
            if t["wid"] == wid:
                return t
    raise KeyError(f"{wid} not in plan {batch}")


def proxy_files(batch: str, wid: str) -> tuple[Path, Path]:
    """The live log and the stop file of WID's proxy, kept out of the agent's workspace during the trial."""
    d = CFG.runs_dir / batch / "fault-proxy"
    return d / f"{wid}.json", d / f"{wid}.stop"


def set_trial_env(wid: str, env: dict | None) -> None:
    """Add (or, with None, remove) the variables run-config.json overrides for the trial (recorder trial_env)."""
    path = CFG.workspaces_dir / wid / "run-config.json"
    cfg = json.loads(path.read_text(encoding="utf-8"))
    if env:
        cfg["set_env"] = env
    else:
        cfg.pop("set_env", None)
    path.write_text(json.dumps(cfg), encoding="utf-8")


def start_proxy(batch: str, wid: str, rule: dict) -> str:
    """Start WID's fault proxy (stopping one a retried step left behind) and point the trial at it."""
    log, stop = proxy_files(batch, wid)
    faultproxy.halt(log, stop)
    state = faultproxy.spawn(CFG.upstream_url, rule or {}, log, stop, max_seconds=PROXY_MAX_SECONDS)
    set_trial_env(wid, {tool.UPSTREAM_URL_ENV: state["tool_url"]})
    return state["tool_url"]


def stop_proxy(batch: str, wid: str) -> dict | None:
    """Stop WID's fault proxy, copy its final log into the workspace and point the trial back at the upstream."""
    log, stop = proxy_files(batch, wid)
    if not log.exists():
        return None
    state = faultproxy.halt(log, stop)
    shutil.copyfile(log, CFG.workspaces_dir / wid / "fault-proxy.json")
    set_trial_env(wid, None)
    return state


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
        proxy = "-"
        if a.post:
            stop_proxy(a.batch, a.post)
            task = BY_ID[trial(a.batch, a.post)["task"]]
            checks, info = task.grade(ctx, "", {"calls": [], "fault_proxy": None})
            state = {k: v for k, v in checks.items() if k in task.state_keys}
            (CFG.workspaces_dir / a.post / "state.json").write_text(
                json.dumps({"captured_at": now, "checks": state, "info": info}, indent=2), encoding="utf-8")
        if a.prep:
            t = trial(a.batch, a.prep)
            BY_ID[t["task"]].reset(ctx)
            if "fault_proxy" in t:  # after the reset, so the proxy sees only the trial's requests
                proxy = start_proxy(a.batch, a.prep, t["fault_proxy"])
            # prepped.json sits in the agent's workspace: nothing in it may hint at the scenario
            (CFG.workspaces_dir / a.prep / "prepped.json").write_text(json.dumps({"reset_at": now}), encoding="utf-8")
        print(f"STEP OK post={a.post or '-'} prep={a.prep or '-'} proxy={proxy}")
        return 0
    except Exception as e:
        traceback.print_exc()
        print(f"STEP FAILED: {e!r}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
