"""What the recorder kept for one trial, and the contract checks on it.

The recorder appends one JSON line per tool call to <trial>/calls.jsonl and keeps the raw stdout and stderr under
<trial>/calls/<id>.out and .err (harness/recorder/main.go); <trial> is the workspace of a sub-agent trial or the
run folder of a run.py trial. read_calls() returns the log with each call's stdout path added, so ready checks can
look at what the tool printed (preview_refusal) and check_envelopes() can validate it against the contract
(VER-002) with aicli's reference validator.
"""

from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

from config import CFG, ConfigError

PROBLEMS_KEPT = 5  # invalid calls described per trial in the results


def read_calls(trial_dir: Path) -> list:
    """calls.jsonl, each call with "stdout_file", the path of its raw stdout."""
    f = Path(trial_dir) / "calls.jsonl"
    if not f.exists():
        return []
    out = []
    for line in f.read_text(encoding="utf-8").splitlines():
        if line.strip():
            c = json.loads(line)
            c["stdout_file"] = str(Path(trial_dir) / "calls" / f"{c['id']}.out")
            out.append(c)
    return out


def stdout_of(call: dict) -> bytes | None:
    """The call's raw stdout, or None when the recorder kept none."""
    p = call.get("stdout_file")
    if not p:
        return None
    try:
        return Path(p).read_bytes()
    except OSError:
        return None


def envelope_of(call: dict) -> dict | None:
    """The call's stdout parsed as one JSON object, or None."""
    raw = stdout_of(call)
    if not raw:
        return None
    try:
        doc = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def preview_refusal(call: dict) -> bool:
    """A write the tool refused for want of a confirm token while returning the preview, exactly as a dry run
    does (CLI-052): exit 5, error.code E_CONFIRMATION_REQUIRED, details.problem confirmation_required and a
    details.preview object. dangerous_flag_required carries no preview and does not count."""
    if call.get("exit") != 5:
        return False
    env = envelope_of(call)
    if not env or env.get("ok") is not False:
        return False
    err = env.get("error")
    details = err.get("details") if isinstance(err, dict) else None
    return (isinstance(details, dict) and err.get("code") == "E_CONFIRMATION_REQUIRED"
            and details.get("problem") == "confirmation_required"
            and isinstance(details.get("preview"), dict) and bool(details["preview"]))


# ---------------------------------------------------------------- envelope validation (VER-002)

_PACKAGE = "_agent_eval_aicli"
_validator = None


def validator():
    """aicli's validate module from CFG.aicli_dir. It is imported as a private package without running the
    package's __init__.py, which imports click: the validators need only the standard library, and so does the
    harness. Raises ConfigError when the folder holds no validate.py."""
    global _validator
    if _validator is None:
        d = Path(CFG.aicli_dir)
        if not (d / "validate.py").is_file():
            raise ConfigError(f"aicli_dir {d} holds no validate.py; set aicli_dir to the aicli package "
                              "(template/python/aicli in the spec repository)")
        if _PACKAGE not in sys.modules:
            spec = importlib.machinery.ModuleSpec(_PACKAGE, None, is_package=True)
            spec.submodule_search_locations = [str(d)]
            sys.modules[_PACKAGE] = importlib.util.module_from_spec(spec)
        _validator = importlib.import_module(_PACKAGE + ".validate")
    return _validator


def _flag(argv: list, name: str):
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def skip_reason(call: dict, raw: bytes | None) -> str | None:
    """Why a call's stdout is not an envelope to validate, or None. Help is text (CLI-030); --format text and
    raw are for people and bytes (CLI-005), except an error the tool still emits as a JSON envelope."""
    argv = call.get("argv") or []
    if call.get("error"):
        return "the recorder could not start the tool"
    if raw is None:
        return "no stdout kept"
    if "--help" in argv or "-h" in argv or argv[:1] == ["help"]:
        return "help is text"
    fmt = _flag(argv, "--format")
    if fmt in ("text", "raw") and not (call.get("exit") != 0 and raw.lstrip().startswith(b"{")):
        return f"--format {fmt}"
    return None


def _is_stream(raw: bytes) -> bool:
    """NDJSON (CLI-006): the first line is a whole envelope with a type key."""
    first = raw.lstrip().split(b"\n", 1)[0]
    try:
        doc = json.loads(first.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    return isinstance(doc, dict) and "type" in doc


def _short(argv: list, limit: int = 80) -> str:
    s = " ".join(argv)
    return s if len(s) <= limit else s[:limit - 1] + "…"


def _exit_mismatch(v, doc_line: bytes, exit_code) -> list:
    """The exit code against the envelope (VER-002, CLI-010): 0 for ok, else the core code's exit in contract.json.
    Extension codes are not in the core table and are not checked."""
    doc = json.loads(doc_line.decode("utf-8"))
    if doc.get("ok") is True:
        want, what = 0, "a success envelope"
    else:
        code = (doc.get("error") or {}).get("code")
        row = v.CORE_CODES.get(code)
        if row is None:
            return []
        want, what = row.exit, code
    return [] if exit_code == want else [f"$: exit {exit_code}, but {what} means exit {want} (contract.json)"]


def _validate(v, raw: bytes, exit_code) -> list:
    """The contract problems of one recorded stdout ([] when it is valid)."""
    if not raw:
        return ["$: stdout is empty; CLI-001 wants exactly one envelope"]
    try:
        if _is_stream(raw):
            v.validate_stream(raw)
            return _exit_mismatch(v, raw.strip().split(b"\n")[-1], exit_code)  # the summary line sets the exit
        v.validate_envelope(raw)
        return _exit_mismatch(v, raw, exit_code)
    except v.ContractViolation as e:
        return list(e.problems)
    except Exception as e:  # noqa: BLE001 - a validator bug must not stop the collection
        return [f"validator error: {e!r}"]


def check_envelopes(calls: list, keep: int = PROBLEMS_KEPT) -> dict:
    """Validate every recorded stdout with aicli's validate_envelope (validate_stream for NDJSON), and its exit
    code against contract.json. Returns {"checked", "invalid", "skipped", "problems"}: problems describes the
    first `keep` invalid calls. A validator that cannot be loaded is reported, never raised."""
    out = {"checked": 0, "invalid": 0, "skipped": 0, "problems": []}
    try:
        v = validator()
    except Exception as e:  # noqa: BLE001 - a broken validator must not stop the collection
        return {**out, "error": f"validator unavailable: {e}"}
    for c in calls:
        raw = stdout_of(c)
        if skip_reason(c, raw):
            out["skipped"] += 1
            continue
        out["checked"] += 1
        problems = _validate(v, raw, c.get("exit"))
        if not problems:
            continue
        out["invalid"] += 1
        if len(out["problems"]) < keep:
            out["problems"].append(f"{c.get('id')} `{_short(c.get('argv') or [])}` (exit {c.get('exit')}): "
                                   + "; ".join(problems[:3]) + (" …" if len(problems) > 3 else ""))
    return out
