"""Run agent eval trials with the claude CLI: one trial = task x arm x Skill on/off x model x repetition.

Each trial is a fresh, isolated `claude -p` session:
  - cwd is an empty work folder; with the Skill on, only the arm's Skill sits in work/.claude/skills/<tool>/
    (user settings, user Skills, MCP servers and plugins' settings are not loaded);
  - the tool on PATH is the recorder, which holds the upstream credentials; the agent's environment has none;
  - the transcript (stream-json), every tool call and the grade are saved per trial.

Usage:
  python harness/run.py --batch baseline --arms A --skill on,off --models claude-sonnet-5-5 --tasks all
Writes <work_dir>/runs/<batch>/<run_id>/ per trial and <work_dir>/runs/<batch>/results.json. Trials that
already have a result.json are skipped, so an interrupted batch resumes.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path

import tool
from config import CFG, EXE, bypass_patterns, load_arms
from tasks import BY_ID, SAFETY_KEYS, TASKS, Ctx

CREATE_NO_WINDOW = 0x08000000  # Windows: start sessions without a console window


def short_model(model: str) -> str:
    return re.sub(r"^claude-", "", model)


def agent_env(run_dir: Path) -> dict:
    drop = tuple(p.upper() for p in tool.AGENT_ENV_DROP_PREFIXES) + ("EVAL_",)
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(drop)}
    env["PATH"] = str(CFG.bin_dir) + os.pathsep + env.get("PATH", "")
    if CFG.git_bash:
        env["CLAUDE_CODE_GIT_BASH_PATH"] = str(CFG.git_bash)
    env["EVAL_RUN_DIR"] = str(run_dir)
    env["DISABLE_AUTOUPDATER"] = "1"
    return env


def kill_tree(proc: subprocess.Popen) -> None:
    """Stop a timed-out session and everything it started."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                       creationflags=CREATE_NO_WINDOW)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def parse_transcript(path: Path) -> dict:
    m = {"tool_calls": {}, "bash_commands": [], "tool_result_chars": 0, "tool_results_truncated": 0,
         "final": "", "is_error": None, "num_turns": None, "duration_ms": None, "cost_usd": None,
         "usage": {}, "model": None, "skills_loaded": [], "denied_tools": 0}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            m["model"] = ev.get("model")
            m["skills_loaded"] = [s for s in ev.get("skills", []) if tool.NAME in s]
        elif t == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    m["tool_calls"][block["name"]] = m["tool_calls"].get(block["name"], 0) + 1
                    if block["name"] == "Bash":
                        m["bash_commands"].append(block.get("input", {}).get("command", ""))
        elif t == "user":
            content = ev.get("message", {}).get("content", [])
            for block in content if isinstance(content, list) else []:
                if block.get("type") != "tool_result":
                    continue
                c = block.get("content")
                text = c if isinstance(c, str) else "".join(x.get("text", "") for x in c or [] if isinstance(x, dict))
                m["tool_result_chars"] += len(text)
                if re.search(r"Output too large|persisted-output|\[Truncated|middle of the output", text):
                    m["tool_results_truncated"] += 1
                if "permission" in text.lower() and block.get("is_error"):
                    m["denied_tools"] += 1
        elif t == "result":
            m.update(final=ev.get("result") or "", is_error=ev.get("is_error"), num_turns=ev.get("num_turns"),
                     duration_ms=ev.get("duration_ms"), cost_usd=ev.get("total_cost_usd"), usage=ev.get("usage") or {})
    return m


def read_calls(run_dir: Path) -> list:
    f = run_dir / "calls.jsonl"
    if not f.exists():
        return []
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]


def call_stats(calls: list) -> dict:
    exits = {}
    for c in calls:
        exits[str(c["exit"])] = exits.get(str(c["exit"]), 0) + 1
    writes_done = [c for c in calls if "--confirm" in c["argv"] and c["exit"] == 0]
    return {
        "cli_calls": len(calls),
        "cli_exit_codes": exits,
        "cli_invalid_calls": exits.get("2", 0),
        "cli_failed_calls": sum(v for k, v in exits.items() if k != "0"),
        "cli_stdout_bytes": sum(c["stdout_bytes"] for c in calls),
        "cli_stderr_bytes": sum(c["stderr_bytes"] for c in calls),
        "cli_confirmed_writes": len(writes_done),
        "cli_time_ms": sum(c["duration_ms"] for c in calls),
    }


def run_trial(ctx: Ctx, arms: dict, claude: str, bypass: dict, batch_dir: Path, task_id: str, arm: str,
              skill: str, model: str, rep: int, budget: float, timeout_min: float, max_turns: int) -> dict:
    task = BY_ID[task_id]
    run_id = f"{task_id}__{arm}-skill-{skill}__{short_model(model)}__r{rep}"
    run_dir = batch_dir / run_id
    if (run_dir / "result.json").exists():
        return json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    if run_dir.exists():
        shutil.rmtree(run_dir)
    work = run_dir / "work"
    work.mkdir(parents=True)
    if skill == "on":
        shutil.copytree(arms[arm]["skill_dir"], work / ".claude" / "skills" / tool.NAME)
    (run_dir / "run-config.json").write_text(json.dumps({"real_bin": str(arms[arm]["real_bin"])}), encoding="utf-8")

    task.reset(ctx)
    prompt = tool.cli_preamble() + task.prompt
    cmd = [claude, "-p", prompt, "--model", model,
           "--setting-sources", "project", "--strict-mcp-config",
           "--tools", "Bash,Read,Glob,Grep,Skill", "--permission-mode", "dontAsk",
           "--allowedTools", "Bash", "Read", "Glob", "Grep", "Skill",
           "--max-turns", str(max_turns), "--max-budget-usd", str(budget),
           "--no-session-persistence", "--output-format", "stream-json", "--verbose"]
    popen_kw = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    started = time.time()
    timed_out = False
    with open(run_dir / "transcript.jsonl", "wb") as out, open(run_dir / "claude.stderr", "wb") as err:
        proc = subprocess.Popen(cmd, cwd=work, env=agent_env(run_dir), stdout=out, stderr=err,
                                stdin=subprocess.DEVNULL, **popen_kw)
        try:
            proc.wait(timeout=timeout_min * 60)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_tree(proc)
    wall_s = time.time() - started

    tm = parse_transcript(run_dir / "transcript.jsonl")
    calls = read_calls(run_dir)
    leaks = sorted({label for c in tm["bash_commands"] for label, rx in bypass.items() if re.search(rx, c)})
    try:
        checks, info = task.grade(ctx, tm["final"], {"calls": calls})
    except Exception as e:  # grading must never kill the batch
        checks, info = {"grader_error": False}, {"error": repr(e)}
    safety_keys = SAFETY_KEYS.get(task_id, [])
    unrequested_writes = task.kind == "read" and call_stats(calls)["cli_confirmed_writes"] > 0
    result = {
        "run_id": run_id, "task": task_id, "kind": task.kind, "tags": task.tags, "arm": arm, "skill": skill,
        "model": model, "model_reported": tm["model"], "rep": rep,
        "success": all(checks.values()) and not timed_out and not unrequested_writes,
        "checks": checks, "grade_info": info,
        "safety_ok": all(checks.get(k, True) for k in safety_keys) and not unrequested_writes,
        "unrequested_writes": unrequested_writes, "timed_out": timed_out, "wall_s": round(wall_s, 1),
        "leak_patterns": leaks, "skill_loaded": tm["skills_loaded"],
        **{k: tm[k] for k in ("tool_calls", "tool_result_chars", "tool_results_truncated", "num_turns",
                               "duration_ms", "cost_usd", "usage", "is_error", "denied_tools")},
        "bash_calls": len(tm["bash_commands"]),
        **call_stats(calls),
        "final_answer": tm["final"],
    }
    (run_dir / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", required=True)
    ap.add_argument("--tasks", default="all")
    ap.add_argument("--arms", default="A")
    ap.add_argument("--skill", default="on,off")
    ap.add_argument("--models", default="claude-sonnet-5-5")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--parallel", type=int, default=3)
    ap.add_argument("--budget", type=float, default=3.0, help="max USD per trial")
    ap.add_argument("--timeout-min", type=float, default=15)
    ap.add_argument("--max-turns", type=int, default=40)
    a = ap.parse_args()

    claude = shutil.which(CFG.claude_bin)
    if not claude:
        print(f"claude CLI not found as {CFG.claude_bin!r}; install it or set claude_bin", file=sys.stderr)
        return 1
    recorder = CFG.bin_dir / (tool.NAME + EXE)
    if not recorder.is_file():
        print(f"no recorder at {recorder}; build it with: python harness/build_recorder.py", file=sys.stderr)
        return 1
    arm_names = a.arms.split(",")
    arms = load_arms(arm_names)
    task_ids = [t.id for t in TASKS] if a.tasks == "all" else a.tasks.split(",")
    combos = [(arm, s, m, r) for r in range(1, a.reps + 1) for m in a.models.split(",")
              for arm in arm_names for s in a.skill.split(",")]
    batch_dir = CFG.runs_dir / a.batch
    batch_dir.mkdir(parents=True, exist_ok=True)
    ctx = Ctx()
    bypass = bypass_patterns(tool.DIRECT_API_PATTERNS)

    def run_task_group(task_id: str) -> list:
        out = []
        for arm, s, m, r in combos:  # same task runs sequentially: resets and writes must not overlap
            try:
                res = run_trial(ctx, arms, claude, bypass, batch_dir, task_id, arm, s, m, r, a.budget,
                                a.timeout_min, a.max_turns)
                print(f"{res['run_id']}: success={res['success']} calls={res['cli_calls']} "
                      f"invalid={res['cli_invalid_calls']} chars={res['tool_result_chars']} "
                      f"cost={res['cost_usd']} wall={res['wall_s']}s", flush=True)
                out.append(res)
            except Exception:
                print(f"{task_id} {arm} {s} {m} r{r}: harness error\n{traceback.format_exc()}", flush=True)
        return out

    results = []
    with cf.ThreadPoolExecutor(max_workers=a.parallel) as pool:
        for chunk in pool.map(run_task_group, task_ids):
            results.extend(chunk)
    (batch_dir / "results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"done: {len(results)} trials -> {batch_dir / 'results.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
