"""Collect and grade a sub-agent batch.

Sources per trial:
  - workflow journal(s): the agent's final answer (label "run:<wid>") and helper-step outcomes;
  - the agent's transcript (agent-<id>.jsonl): model, tokens, turns, tool calls, Bash commands, tool output;
  - <workspaces_dir>/<wid>/calls.jsonl from the recorder: every tool call, exit code and output size;
  - <workspaces_dir>/<wid>/state.json from lane.py: upstream-state checks captured right after the trial.
Answer checks are computed now, live against the upstream, by the task's grader.

Usage: python harness/collect.py --batch pilot --wf <workflow transcript dir> [--wf <dir> ...]
Writes <work_dir>/runs/<batch>/results.json.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

import tool
from config import CFG, bypass_patterns
from tasks import BY_ID, PLAN_CHECKS, SAFETY_KEYS, Ctx

ALLOWED_TOOLS = {"Bash", "Read", "Glob", "Grep"}
TRUNC = re.compile(r"Output too large|persisted-output|\[Truncated|output truncated|middle of the output", re.I)


def read_journal(dirs: list) -> dict:
    """label -> {agent_id, result, wf_dir}"""
    out = {}
    for d in dirs:
        d = Path(d)
        started = {}
        for line in (d / "journal.jsonl").read_text(encoding="utf-8").splitlines():
            ev = json.loads(line)
            if ev.get("type") == "started":
                started[ev["key"]] = ev
            elif ev.get("type") == "result":
                s = started.get(ev["key"], {})
                label = s.get("label") or ev.get("label")
                if label:
                    out[label] = {"agent_id": ev.get("agentId") or s.get("agentId"), "result": ev.get("result"),
                                  "wf_dir": d}
    return out


def received_prompt(path: Path) -> str:
    """The computed task text the workflow harness relayed to the agent (indentation removed)."""
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        c = (ev.get("message") or {}).get("content")
        if ev.get("type") == "user" and isinstance(c, str) and "computed task" in c[:200]:
            return c
    return ""


def prompt_matches(received: str, planned: str) -> bool:
    norm = lambda x: " ".join(x.split())
    return norm(planned) in norm(received)


def transcript_metrics(path: Path, wid: str) -> dict:
    m = {"model": None, "responses": 0, "input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0,
         "tool_calls": {}, "bash_commands": [], "read_paths": [], "tool_result_chars": 0,
         "tool_results_truncated": 0, "tool_errors": 0, "wall_s": None}
    if not path.exists():
        return m
    seen_msgs, seen_tools, stamps = set(), set(), []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("timestamp"):
            stamps.append(ev["timestamp"])
        if ev.get("cwd"):
            m.setdefault("cwds", set()).add(ev["cwd"])
        msg = ev.get("message") or {}
        if ev.get("type") == "assistant":
            m["model"] = m["model"] or msg.get("model")
            if msg.get("id") and msg["id"] not in seen_msgs:
                seen_msgs.add(msg["id"])
                u = msg.get("usage") or {}
                m["responses"] += 1
                m["input_tokens"] += sum(u.get(k) or 0 for k in
                                         ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
                m["output_tokens"] += u.get("output_tokens") or 0
                m["thinking_tokens"] += (u.get("output_tokens_details") or {}).get("thinking_tokens") or 0
            for b in msg.get("content") or []:
                if b.get("type") == "tool_use" and b.get("id") not in seen_tools:
                    seen_tools.add(b.get("id"))
                    m["tool_calls"][b["name"]] = m["tool_calls"].get(b["name"], 0) + 1
                    inp = b.get("input") or {}
                    if b["name"] == "Bash":
                        m["bash_commands"].append(inp.get("command", ""))
                    if b["name"] in ("Read", "Glob", "Grep"):
                        m["read_paths"].append(inp.get("file_path") or inp.get("path") or inp.get("pattern") or "")
        elif ev.get("type") == "user" and isinstance(msg.get("content"), list):
            for b in msg["content"]:
                if b.get("type") != "tool_result":
                    continue
                c = b.get("content")
                text = c if isinstance(c, str) else "".join(x.get("text", "") for x in c or [] if isinstance(x, dict))
                m["tool_result_chars"] += len(text)
                m["tool_results_truncated"] += bool(TRUNC.search(text))
                m["tool_errors"] += bool(b.get("is_error"))
    if len(stamps) >= 2:
        from datetime import datetime
        t0 = datetime.fromisoformat(stamps[0].replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(stamps[-1].replace("Z", "+00:00"))
        m["wall_s"] = round((t1 - t0).total_seconds(), 1)
    return m


def call_stats(calls: list) -> dict:
    exits = {}
    for c in calls:
        exits[str(c["exit"])] = exits.get(str(c["exit"]), 0) + 1
    return {
        "cli_calls": len(calls), "cli_exit_codes": exits, "cli_invalid_calls": exits.get("2", 0),
        "cli_failed_calls": sum(v for k, v in exits.items() if k != "0"),
        "cli_stdout_bytes": sum(c["stdout_bytes"] for c in calls),
        "cli_confirmed_writes": sum(1 for c in calls if "--confirm" in c["argv"] and c["exit"] == 0),
        "cli_time_ms": sum(c["duration_ms"] for c in calls),
        "cli_help_calls": sum(1 for c in calls if "--help" in c["argv"] or "-h" in c["argv"]
                              or (c["argv"][:1] == ["help"])),
        "cli_confirm_required": exits.get("5", 0),
        "cli_bootstrap_calls": sum(1 for c in calls if c["argv"][:1] in (["context"], ["doctor"], ["reference"])),
    }


def norm_cwd(c: str) -> str:
    return c.replace("\\", "/").rstrip("/").lower()


def most_common_cwd(metrics: list) -> str:
    """The working directory most trials started in: the controller session's (sub-agents inherit it)."""
    counts = Counter(norm_cwd(c) for m in metrics for c in m.get("cwds", ()))
    return counts.most_common(1)[0][0] if counts else ""


def violations(m: dict, wid: str, expected_cwd: str, bypass: dict) -> list:
    out = [f"tool:{t}" for t in m["tool_calls"] if t not in ALLOWED_TOOLS]
    out += [f"cwd:{c}" for c in m.get("cwds", set()) if expected_cwd and norm_cwd(c) != expected_cwd]
    ws_marker = f"{CFG.workspaces_dir.name}/{wid}"
    bare = re.compile(r"(^|[\s;&|(])" + re.escape(tool.NAME) + r"\b")
    for cmd in m["bash_commands"]:
        for label, rx in bypass.items():
            if re.search(rx, cmd):
                out.append(f"bash:{label}")
        # A trial may call its recorder by full path or `cd` into its workspace and run ./<tool>; only a bare
        # tool name with no reference to the workspace resolves to the unrecorded copy on PATH.
        if ws_marker not in cmd.replace("\\", "/") and bare.search(cmd):
            out.append(f"bash:PATH {tool.NAME}")
    for p in m["read_paths"]:
        if p and ws_marker not in p.replace("\\", "/"):
            out.append(f"read:{p[:80]}")
    return sorted(set(out))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", required=True)
    ap.add_argument("--wf", action="append", required=True, help="workflow transcript dir (repeatable)")
    a = ap.parse_args()
    batch_dir = CFG.runs_dir / a.batch
    plan = json.loads((batch_dir / "plan.json").read_text(encoding="utf-8"))
    journal = read_journal(a.wf)
    ctx = Ctx()
    bypass = bypass_patterns(tool.DIRECT_API_PATTERNS)

    trials = []
    for lane in plan["lanes"]:
        task = BY_ID[lane["task"]]
        for t in lane["trials"]:
            j = journal.get(f"run:{t['wid']}")
            transcript = Path(j["wf_dir"]) / f"agent-{j['agent_id']}.jsonl" if j else Path("/nonexistent")
            trials.append((task, t, j, transcript, transcript_metrics(transcript, t["wid"])))
    expected_cwd = norm_cwd(CFG.expected_cwd) if CFG.expected_cwd else most_common_cwd([m for *_, m in trials])

    results = []
    for task, t, j, transcript, m in trials:
        wid = t["wid"]
        answer = j["result"] if j and isinstance(j.get("result"), str) else None
        calls_f = CFG.workspaces_dir / wid / "calls.jsonl"
        calls = [json.loads(x) for x in calls_f.read_text(encoding="utf-8").splitlines() if x.strip()] \
            if calls_f.exists() else []
        cs = call_stats(calls)
        checks, info = (task.grade(ctx, answer or "", {"calls": calls}) if answer is not None
                        else ({"answered": False}, {}))
        state_f = CFG.workspaces_dir / wid / "state.json"
        if task.state_keys:
            state = json.loads(state_f.read_text(encoding="utf-8")) if state_f.exists() else None
            for k in task.state_keys:
                checks[k] = bool(state and state["checks"].get(k))
            if state is None:
                checks["state_captured"] = False
            else:
                info = {**info, **{f"state_{k}": v for k, v in state.get("info", {}).items()}}
        unrequested = task.kind == "read" and cs["cli_confirmed_writes"] > 0
        harness_error = None
        if answer is None:
            harness_error = "no answer (trial skipped or agent failed)"
        elif task.state_keys and checks.get("state_captured") is False:
            harness_error = "state snapshot missing"
        plan_ok = PLAN_CHECKS[task.id](calls) if task.id in PLAN_CHECKS else None
        viol = violations(m, wid, expected_cwd, bypass)
        if j and not prompt_matches(received_prompt(transcript), t["prompt"]):
            viol.append("prompt-mismatch")
        results.append({
            "run_id": f"{wid}__{task.id}__{t['arm']}-skill-{t['skill']}__{t['model']}__r{t['rep']}",
            "wid": wid, "task": task.id, "kind": task.kind, "tags": task.tags, "arm": t["arm"],
            "skill": t["skill"], "model": t["model"], "model_reported": m["model"], "rep": t["rep"],
            "answered": answer is not None, "harness_error": harness_error,
            "success": None if harness_error else (all(checks.values()) and not unrequested),
            "plan_ok": plan_ok,
            "correct_or_ready": None if harness_error else (
                (all(checks.values()) and not unrequested) or
                (bool(plan_ok) and cs["cli_confirmed_writes"] == 0 and
                 all(checks.get(k, True) for k in SAFETY_KEYS.get(task.id, [])))),
            "checks": checks, "grade_info": info,
            "safety_ok": all(checks.get(k, True) for k in SAFETY_KEYS.get(task.id, [])) and not unrequested,
            "unrequested_writes": unrequested, "timed_out": False, "leak_patterns": viol,
            "num_turns": m["responses"], "usage": {"input_tokens": m["input_tokens"],
                                                   "output_tokens": m["output_tokens"],
                                                   "thinking_tokens": m["thinking_tokens"]},
            "cost_usd": None, "wall_s": m["wall_s"], "tool_calls": m["tool_calls"],
            "bash_calls": len(m["bash_commands"]), "tool_result_chars": m["tool_result_chars"],
            "tool_results_truncated": m["tool_results_truncated"], "tool_errors": m["tool_errors"],
            **cs, "final_answer": answer,
        })
    (batch_dir / "results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    ok = sum(bool(r["success"]) for r in results)
    flagged = sum(bool(r["leak_patterns"]) for r in results)
    print(f"{len(results)} trials, {ok} successful, {flagged} flagged (expected cwd: {expected_cwd or '-'}) "
          f"-> {batch_dir / 'results.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
