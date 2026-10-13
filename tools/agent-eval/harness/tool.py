"""The tool under test, as the generic harness sees it. Worked example: gitlab-cli on a disposable GitLab.

To port the harness to another tool, rewrite this file, gl.py (the upstream client used for seeding, resets
and grading), seed.py (the dataset) and tasks.py (tasks, resets, graders, ready checks, scope grants).
config.py, plan.py, lane.py, collect.py, compare.py, report.py, run.py, build_recorder.py, scopefile.py,
recorded.py, faultproxy.py, the recorder and the trial workflow are generic.
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

# The variable that carries the upstream address. A lost-reply trial points it at the fault proxy through its
# run-config.json, so recorder_config() lists it in trial_env.
UPSTREAM_URL_ENV = "GITLAB_CLI_HOST"

# ai-native-cli-spec 2.0 (arms with "spec": "2.0" in arms.json). STATE_DIR is the tool's state directory under
# its home (SEC-030: ~/.<tool>); the harness writes each trial's scope file there as scope.json (SEC-011). The
# two variables would move the scope file or the state directory, so the recorder must drop both from the
# agent's environment (here through drop_env_prefixes); plan.py and run.py refuse to start if it does not.
STATE_DIR = "." + NAME
SCOPE_FILE_ENV = "GITLAB_CLI_SCOPE_FILE"
STATE_DIR_ENV = "GITLAB_CLI_STATE_DIR"


def recorder_config(agent_token: str) -> dict:
    """The recorder's private config; seed.py writes it to <work_dir>/.secrets/recorder.json.

    The recorder removes drop_env, every variable starting with one of drop_env_prefixes and every variable it
    is about to set from the environment it gives the real binary, then sets set_env. It always gives the binary
    a per-trial HOME, USERPROFILE, APPDATA and LOCALAPPDATA and removes the XDG_* directories. A trial's
    run-config.json may override the variables listed in trial_env, and no others.
    """
    return {
        "tool": NAME,
        "set_env": {
            UPSTREAM_URL_ENV: CFG.upstream_url,
            "GITLAB_CLI_TOKEN": agent_token,
            "GITLAB_CLI_NO_UPDATE_CHECK": "1",
        },
        "drop_env": ["GITLAB_TOKEN", "GITLAB_HOST"],
        "drop_env_prefixes": ["GITLAB_CLI_"],
        "trial_env": [UPSTREAM_URL_ENV],
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
