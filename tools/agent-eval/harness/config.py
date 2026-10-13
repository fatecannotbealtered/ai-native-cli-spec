"""Settings and local files of the agent-eval harness.

Settings come from a JSON file, and each one can be overridden by an environment variable:

    file: $EVAL_CONFIG if set, else eval.config.json in tools/agent-eval/ (the folder above this one)

    setting           variable                default
    work_dir          EVAL_WORK_DIR           required: eval data (.secrets/, bin/, runs/, ws/, arms.json, facts.json)
    upstream_url      EVAL_UPSTREAM_URL       required: base URL of the disposable upstream
    admin_token_file  EVAL_ADMIN_TOKEN_FILE   none: file holding the admin token
    admin_token_key   EVAL_ADMIN_TOKEN_KEY    none: with a KEY=VALUE file, the key that holds the token
    workspaces_dir    EVAL_WORKSPACES_DIR     <work_dir>/ws
    python            EVAL_PYTHON             python
    claude_bin        EVAL_CLAUDE_BIN         claude
    git_bash          EVAL_GIT_BASH           none
    expected_cwd      EVAL_EXPECTED_CWD       none: the working directory most trials of the batch started in

The admin token itself may also come from EVAL_ADMIN_TOKEN (environment only, never the file). Relative paths
resolve against the file's folder (in the file) or the current directory (in variables); empty values mean
"not set"; keys starting with "_" are comments; unknown keys are errors. README.md documents every setting.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

HARNESS_DIR = Path(__file__).resolve().parent
EVAL_DIR = HARNESS_DIR.parent
EXE = ".exe" if os.name == "nt" else ""

# setting -> (environment variable, holds a path)
SETTINGS = {
    "work_dir": ("EVAL_WORK_DIR", True),
    "upstream_url": ("EVAL_UPSTREAM_URL", False),
    "admin_token_file": ("EVAL_ADMIN_TOKEN_FILE", True),
    "admin_token_key": ("EVAL_ADMIN_TOKEN_KEY", False),
    "workspaces_dir": ("EVAL_WORKSPACES_DIR", True),
    "python": ("EVAL_PYTHON", False),
    "claude_bin": ("EVAL_CLAUDE_BIN", False),
    "git_bash": ("EVAL_GIT_BASH", True),
    "expected_cwd": ("EVAL_EXPECTED_CWD", False),
}


class ConfigError(Exception):
    """A setting or local file the harness needs is missing or invalid."""


def _report_config_error(kind, value, tb, _default=sys.excepthook):
    if issubclass(kind, ConfigError):
        print(f"agent-eval: {value}", file=sys.stderr)
    else:
        _default(kind, value, tb)


sys.excepthook = _report_config_error


def _resolve(value: str, base: Path) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (base / p).resolve()


def _unique_keys(pairs: list) -> dict:
    out = {}
    for key, value in pairs:
        if key in out:
            raise ConfigError(f"duplicate key {key!r}")
        out[key] = value
    return out


class Config:
    def __init__(self, values: dict, source: str):
        self._values = values
        self.source = source

    def _required(self, key: str):
        value = self._values.get(key)
        if not value:
            raise ConfigError(f"{key} is not set; set it in {self.source} or with {SETTINGS[key][0]} "
                              "(see tools/agent-eval/README.md)")
        return value

    @property
    def work_dir(self) -> Path:
        return self._required("work_dir")

    @property
    def upstream_url(self) -> str:
        return self._required("upstream_url").rstrip("/")

    @property
    def workspaces_dir(self) -> Path:
        return self._values.get("workspaces_dir") or self.work_dir / "ws"

    @property
    def secrets_dir(self) -> Path:
        return self.work_dir / ".secrets"

    @property
    def runs_dir(self) -> Path:
        return self.work_dir / "runs"

    @property
    def bin_dir(self) -> Path:
        return self.work_dir / "bin"

    @property
    def facts_file(self) -> Path:
        return self.work_dir / "facts.json"

    @property
    def arms_file(self) -> Path:
        return self.work_dir / "arms.json"

    @property
    def admin_token_file(self) -> Path | None:
        return self._values.get("admin_token_file")

    @property
    def admin_token_key(self) -> str:
        return self._values.get("admin_token_key") or ""

    @property
    def python(self) -> str:
        return self._values.get("python") or "python"

    @property
    def claude_bin(self) -> str:
        return self._values.get("claude_bin") or "claude"

    @property
    def git_bash(self) -> Path | None:
        return self._values.get("git_bash")

    @property
    def expected_cwd(self) -> str:
        return self._values.get("expected_cwd") or ""


def load() -> Config:
    named = os.environ.get("EVAL_CONFIG", "")
    path = _resolve(named, Path.cwd()) if named else EVAL_DIR / "eval.config.json"
    values: dict = {}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_keys)
        except (ValueError, ConfigError) as e:
            raise ConfigError(f"{path}: {e}") from None
        if not isinstance(raw, dict):
            raise ConfigError(f"{path}: expected a JSON object")
        for key, value in raw.items():
            if key.startswith("_"):
                continue
            if key not in SETTINGS:
                raise ConfigError(f"{path}: unknown setting {key!r}; known settings: {', '.join(SETTINGS)}")
            if not isinstance(value, str):
                raise ConfigError(f"{path}: {key} must be a string")
            if value:
                values[key] = _resolve(value, path.parent) if SETTINGS[key][1] else value
    elif named:
        raise ConfigError(f"EVAL_CONFIG names {path}, which is not a file")
    for key, (var, is_path) in SETTINGS.items():
        value = os.environ.get(var, "")
        if value:
            values[key] = _resolve(value, Path.cwd()) if is_path else value
    return Config(values, str(path))


CFG = load()


def admin_token() -> str:
    """The admin identity's token: EVAL_ADMIN_TOKEN, else admin_token_file (the whole file, or the value of the
    admin_token_key line of a KEY=VALUE file). Never printed."""
    token = os.environ.get("EVAL_ADMIN_TOKEN", "").strip()
    if token:
        return token
    path = CFG.admin_token_file
    if not path:
        raise ConfigError(f"no admin token: set admin_token_file (and admin_token_key) in {CFG.source}, "
                          "or EVAL_ADMIN_TOKEN")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as e:
        raise ConfigError(f"cannot read admin_token_file {path}: {e.strerror}") from None
    key = CFG.admin_token_key
    if not key:
        token = text.strip()
        if token and len(token.split()) == 1:
            return token
        raise ConfigError(f"admin_token_file {path} must hold only the token; for a KEY=VALUE file set "
                          "admin_token_key")
    for line in text.splitlines():
        name, sep, value = line.strip().removeprefix("export ").partition("=")
        if sep and name.strip() == key:
            value = value.strip().strip("'\"")
            if value:
                return value
    raise ConfigError(f"admin_token_file {path} has no {key}=... line")


def write_private(path: Path, text: str) -> None:
    """Write a file only its owner can read: mode 0600 on POSIX (on Windows the profile's ACL applies)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    if os.name != "nt":
        os.chmod(path, 0o600)


def load_arms(names: list) -> dict:
    """The named arms from <work_dir>/arms.json, with real_bin and skill_dir resolved against the file's folder.
    An arm's sha256, when recorded, must match its binary: arms are frozen (VER-006)."""
    path = CFG.arms_file
    try:
        arms = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError:
        raise ConfigError(f"no arms file at {path}; copy arms.example.json there and edit it") from None
    except ValueError as e:
        raise ConfigError(f"{path}: {e}") from None
    out = {}
    for name in names:
        arm = arms.get(name) if isinstance(arms, dict) else None
        if not isinstance(arm, dict) or not arm.get("real_bin") or not arm.get("skill_dir"):
            raise ConfigError(f"{path}: arm {name!r} needs real_bin and skill_dir")
        real_bin = _resolve(arm["real_bin"], path.parent)
        skill_dir = _resolve(arm["skill_dir"], path.parent)
        if not real_bin.is_file():
            raise ConfigError(f"arm {name}: real_bin {real_bin} is not a file")
        if not (skill_dir / "SKILL.md").is_file():
            raise ConfigError(f"arm {name}: skill_dir {skill_dir} has no SKILL.md")
        want = str(arm.get("sha256") or "").lower()
        if want:
            got = hashlib.sha256(real_bin.read_bytes()).hexdigest()
            if got != want:
                raise ConfigError(f"arm {name}: {real_bin} has sha256 {got}; arms.json records {want}")
        else:
            print(f"warning: arm {name} records no sha256 for its binary (VER-006 freezes it)", file=sys.stderr)
        out[name] = {**arm, "real_bin": real_bin, "skill_dir": skill_dir}
    return out


def bash_path(p: Path) -> str:
    """The path as a POSIX shell writes it: D:\\a\\b becomes /d/a/b (Git Bash); other paths keep their form."""
    s = str(p).replace("\\", "/")
    return "/" + s[0].lower() + s[2:] if len(s) > 1 and s[1] == ":" else s


NETWORK_PATTERNS = [r"\bcurl\b", r"\bwget\b", r"Invoke-WebRequest", r"Invoke-RestMethod",
                    r"urllib|requests\.(get|post)|fetch\("]
PRIVATE_NAMES = [".secrets", "tokens.json", "recorder.json", "facts.json", "arms.json"]


def bypass_patterns(direct_api: list) -> dict:
    """label -> regex for Bash commands that bypass the tool or look at harness internals: network clients,
    the tool's direct-API patterns, and the harness's private files, results and code."""
    pats = {p: p for p in NETWORK_PATTERNS + list(direct_api)}
    pats.update({name: re.escape(name) for name in PRIVATE_NAMES})
    pats["agent-eval code"] = r"agent-eval[\\/](harness|analysis)"
    private = [CFG.runs_dir, HARNESS_DIR, EVAL_DIR / "analysis"]
    if CFG.admin_token_file:
        private.append(CFG.admin_token_file)
        pats[CFG.admin_token_file.name] = re.escape(CFG.admin_token_file.name)
    for p in private:
        forms = {str(p), p.as_posix(), bash_path(p)}
        pats[f"path:{p.as_posix()}"] = "(?i)" + "|".join(re.escape(f) for f in sorted(forms))
    return pats
