"""Smoke-test an arm's binary against the eval upstream, with read calls and dry runs only.

Usage: python analysis/smoke_arm.py <arm>
The binary gets the environment the recorder would give it (from <work_dir>/.secrets/recorder.json, written by
harness/seed.py) and its own home under <work_dir>/_smoke/<arm>/. Credentials are never printed. The command
list is gitlab-cli's (worked example); replace it when you port the harness. A 2.0 arm refuses even dry runs
without a scope file (SEC-010), so for one the smoke home gets SMOKE_GRANT.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "harness"))
import scopefile  # noqa: E402
import tool  # noqa: E402
from config import CFG, load_arms  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

P = "acme/payments-service"
SMOKE_GRANT = {"effects": ["change"], "dangerous": True, "targets": [{"key": P}]}  # covers the dry runs below
COMMANDS = [
    ["branch", "list", "--project", P, "--limit", "3"],
    ["member", "list", "--project", P],
    ["project", "get", "--project", P],
    ["user", "list", "--search", "carol"],
    ["pipeline", "list", "-p", P, "--limit", "2"],
    ["repo", "branch", "delete", "old-experiment", "--project", P, "--dangerous", "--dry-run"],
    ["discussion", "list", "3"],
    ["pipeline", "list", "--project", P, "--all"],
    ["mr", "list", "--project", P, "--pipeline-status", "failed"],
    ["issue", "comment", "create", "12", "--project", P, "--body", "smoke", "--dry-run"],
    ["mrs", "get", P + "!5"],
]
SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSWORD|KEY|AUTH", re.I)
HOME_VARS = {"HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
             "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"}


def binary_env(priv: dict, home: Path) -> dict:
    """The environment the recorder gives the real binary (childEnv in harness/recorder/main.go)."""
    set_env = priv.get("set_env") or {}
    drop = HOME_VARS | {k.upper() for k in priv.get("drop_env") or []} | {k.upper() for k in set_env}
    prefixes = tuple(p.upper() for p in priv.get("drop_env_prefixes") or [] if p)
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in drop and not (prefixes and k.upper().startswith(prefixes))}
    env.update({"HOME": str(home), "USERPROFILE": str(home),
                "APPDATA": str(home / "AppData" / "Roaming"), "LOCALAPPDATA": str(home / "AppData" / "Local")})
    env.update(set_env)
    return env


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    arm = sys.argv[1]
    arm_cfg = load_arms([arm])[arm]
    binp = arm_cfg["real_bin"]
    priv = json.loads((CFG.secrets_dir / "recorder.json").read_text(encoding="utf-8"))
    home = CFG.work_dir / "_smoke" / arm.lower()
    for d in (home / "AppData" / "Roaming", home / "AppData" / "Local"):
        d.mkdir(parents=True, exist_ok=True)
    if arm_cfg["spec"] == "2.0":
        scopefile.install(home, tool.STATE_DIR, scopefile.build(SMOKE_GRANT, tool_name=tool.NAME, workdir=home,
                                                                scope_id=scopefile.scope_id("smoke")))
    env = binary_env(priv, home)
    secrets = [v for k, v in (priv.get("set_env") or {}).items() if SECRET_NAME.search(k) and v]
    for c in COMMANDS:
        r = subprocess.run([str(binp)] + c, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env=env, timeout=60)
        out = (r.stdout or r.stderr).strip()
        for s in secrets:
            out = out.replace(s, "<redacted>")
        print(f"exit {r.returncode} | {' '.join(c)}\n   {out[:330]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
