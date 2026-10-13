"""Create per-trial workspaces and the trial plan for a sub-agent batch.

Each trial gets an opaque workspace <workspaces_dir>/<wid>/ holding its own copy of the recorder (invoked by
full path, so parallel trials never share state) and, with the Skill on, a copy of the arm's Skill. Trials
are grouped into one lane per task; a lane runs sequentially so resets and writes on shared upstream objects
never overlap. Arms alternate inside each lane.

Usage:
  python harness/plan.py --batch pilot --prefix p --tasks T01-count-bugs,T05-comment \
      --arms A --skill on,off --models haiku,sonnet --reps 1
Writes <work_dir>/runs/<batch>/plan.json (every trial with its exact prompt) and
<work_dir>/runs/<batch>/workflow-args.json (the args of cli-eval-trials.workflow.js).
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import sys

import tool
from config import CFG, EXE, HARNESS_DIR, bash_path, load_arms
from tasks import BY_ID, TASKS

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")  # batch and prefix end up in shell commands and paths


def render_prompt(template: str, ws: str, request: str) -> str:
    """Fill a tool.prompt_template(); cli-eval-trials.workflow.js does exactly the same."""
    return template.replace("{ws}", ws).replace("{request}", request)


def step_command() -> str:
    """How helper agents start lane.py from Bash."""
    return f"{CFG.python} {shlex.quote((HARNESS_DIR / 'lane.py').as_posix())}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", required=True)
    ap.add_argument("--prefix", required=True, help="workspace id prefix, unique across batches, e.g. p for pilot")
    ap.add_argument("--tasks", default="all")
    ap.add_argument("--arms", default="A")
    ap.add_argument("--skill", default="on,off")
    ap.add_argument("--models", default="haiku,sonnet", help="model names the runner accepts")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--effort", default="medium", help="reasoning effort of the trial agents")
    a = ap.parse_args()

    for flag, value in (("--batch", a.batch), ("--prefix", a.prefix)):
        if not NAME_RE.fullmatch(value):
            print(f"{flag} may hold only letters, digits, '.', '_' and '-'", file=sys.stderr)
            return 2
    skills = a.skill.split(",")
    if not set(skills) <= {"on", "off"}:
        print("--skill takes on, off or on,off", file=sys.stderr)
        return 2
    task_ids = [t.id for t in TASKS] if a.tasks == "all" else a.tasks.split(",")
    unknown = [t for t in task_ids if t not in BY_ID]
    if unknown:
        print(f"unknown task(s): {', '.join(unknown)}", file=sys.stderr)
        return 2
    arm_names = a.arms.split(",")
    arms = load_arms(arm_names)
    recorder = CFG.bin_dir / (tool.NAME + EXE)
    if not recorder.is_file():
        print(f"no recorder at {recorder}; build it with: python harness/build_recorder.py", file=sys.stderr)
        return 1

    batch_dir = CFG.runs_dir / a.batch
    if (batch_dir / "plan.json").exists():
        print(f"{batch_dir / 'plan.json'} already exists; pick a new batch name", file=sys.stderr)
        return 1
    batch_dir.mkdir(parents=True, exist_ok=True)
    templates = {"on": tool.prompt_template(True), "off": tool.prompt_template(False)}

    n = 0
    lanes = []
    for tid in task_ids:
        task = BY_ID[tid]
        trials = []
        for rep in range(1, a.reps + 1):
            for model in a.models.split(","):
                for arm in arm_names:
                    for skill in skills:
                        n += 1
                        wid = f"{a.prefix}{n:03d}"
                        ws = CFG.workspaces_dir / wid
                        if ws.exists():
                            print(f"workspace {ws} already exists; use another prefix", file=sys.stderr)
                            return 1
                        ws.mkdir(parents=True)
                        shutil.copy2(recorder, ws / (tool.NAME + EXE))
                        (ws / "run-config.json").write_text(json.dumps({"real_bin": str(arms[arm]["real_bin"])}),
                                                            encoding="utf-8")
                        if skill == "on":
                            shutil.copytree(arms[arm]["skill_dir"], ws / "skill")
                        trials.append({"wid": wid, "task": tid, "arm": arm, "skill": skill, "model": model,
                                       "rep": rep, "prompt": render_prompt(templates[skill], bash_path(ws), task.prompt)})
        lanes.append({"task": tid, "kind": task.kind, "trials": trials})

    plan = {"batch": a.batch, "effort": a.effort, "lanes": lanes}
    (batch_dir / "plan.json").write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    wf_args = {"batch": a.batch, "effort": a.effort, "step": step_command(), "upstream_url": CFG.upstream_url,
               "ws_root": bash_path(CFG.workspaces_dir), "prompt_template": templates,
               "lanes": [{"task": l["task"], "kind": l["kind"], "task_prompt": BY_ID[l["task"]].prompt,
                          "trials": [{"wid": t["wid"], "model": t["model"], "skill": t["skill"]} for t in l["trials"]]}
                         for l in lanes]}
    (batch_dir / "workflow-args.json").write_text(json.dumps(wf_args, ensure_ascii=False), encoding="utf-8")
    print(f"{n} trials in {len(lanes)} lanes -> {batch_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
