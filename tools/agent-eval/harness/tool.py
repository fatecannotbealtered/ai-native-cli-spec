"""The tool under test, as the generic harness sees it. Worked example: gitlab-cli on a disposable GitLab.

To port the harness to another tool, rewrite this file, gl.py (the upstream client used for seeding, resets
and grading), seed.py (the dataset) and tasks.py (tasks, resets, graders, ready checks). config.py, plan.py,
lane.py, collect.py, compare.py, report.py, run.py, build_recorder.py, the recorder and the trial workflow
are generic.
"""

from __future__ import annotations

from config import CFG

NAME = "gitlab-cli"  # the command agents run; the recorder copy in each workspace and the Skill folder carry it
PRODUCT = "GitLab"  # how prompts name the upstream

# run.py removes the variables with these prefixes from the agent's own environment, so no upstream credential
# reaches the agent (EVAL_* is always removed as well).
AGENT_ENV_DROP_PREFIXES = ("GITLAB_",)

# A Bash command matching one of these reached the upstream without the tool (flagged by collect.py and run.py).
DIRECT_API_PATTERNS = [r"/api/v4"]


def recorder_config(agent_token: str) -> dict:
    """The recorder's private config; seed.py writes it to <work_dir>/.secrets/recorder.json.

    The recorder removes drop_env, every variable starting with one of drop_env_prefixes and every variable it
    is about to set from the environment it gives the real binary, then sets set_env. It always gives the binary
    a per-trial HOME, USERPROFILE, APPDATA and LOCALAPPDATA and removes the XDG_* directories.
    """
    return {
        "tool": NAME,
        "set_env": {
            "GITLAB_CLI_HOST": CFG.upstream_url,
            "GITLAB_CLI_TOKEN": agent_token,
            "GITLAB_CLI_NO_UPDATE_CHECK": "1",
        },
        "drop_env": ["GITLAB_TOKEN", "GITLAB_HOST"],
        "drop_env_prefixes": ["GITLAB_CLI_"],
    }


def cli_preamble() -> str:
    """Prompt prefix for run.py trials (claude -p), where the recorder is on PATH under NAME."""
    return (f"You're helping with our {PRODUCT} at {CFG.upstream_url}. The `{NAME}` command-line tool is "
            f"installed and already authenticated; use it for anything {PRODUCT}-related.\n\n")


def prompt_template(skill_on: bool) -> str:
    """Prompt for sub-agent trials, with {ws} (the trial workspace as a POSIX path) and {request} (the task
    prompt) left open. plan.py fills them for plan.json; cli-eval-trials.workflow.js fills them the same way
    from workflow-args.json; collect.py checks that every trial received the planned prompt."""
    lines = [
        f"You're helping with our {PRODUCT} at {CFG.upstream_url}. The {NAME} command-line tool is installed "
        "and authenticated for it. On this machine, run it by its full path:",
        "",
        f"    {{ws}}/{NAME}",
        "",
        f"(the {NAME} found on PATH is not set up for this {PRODUCT}). Use it for everything {PRODUCT}-related. "
        f"Don't call the {PRODUCT} API directly, and don't use the Skill tool, MCP or browser tools, or the Write "
        "and Edit tools; Bash, Read, Glob and Grep are all you need.",
    ]
    if skill_on:
        lines += ["", f"Before you start, read the {NAME} Skill at {{ws}}/skill/SKILL.md and follow it. "
                      "It may point you to other files in that folder."]
    lines += ["", "When you're done, reply with your final answer.", "", "Request: {request}"]
    return "\n".join(lines)
