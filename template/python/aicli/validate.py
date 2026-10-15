# Vendored from ai-native-cli-spec template/python/aicli.
# Do not edit in tool repos; run scripts/sync-spec.js.
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sean Guo

"""Validators for the contract's shapes (VER-002). They check the key sets, enums
and cross-field rules that contract.json and the JSON Schemas in contract/schemas
define, plus the semantic checks the schemas cannot express (receipt rules, batch
tallies, token binding, untrusted paths, size budgets), so validate_envelope
accepts every valid example of the contract corpus and rejects every invalid one."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable
from typing import Any

from ._util import parse_time
from .contract_gen import (
    BUILTIN_COMMANDS,
    CHANGE_VALUES,
    CHANGELOG_CATEGORIES,
    CHANGELOG_ENTRY_KEYS,
    CHANGELOG_OPTIONAL_KEYS,
    CHANGELOG_REQUIRED_KEYS,
    COMMAND_BUDGET_CHARS,
    CONFLICT_VALUES,
    CONTEXT_AUTHORIZATION_STATUS,
    CONTEXT_PROFILE_KEYS,
    CORE_CODES,
    DELIVERY_VALUES,
    DISPATCH_STATES,
    DOCTOR_REQUIRED_CHECKS,
    DOCTOR_STATUS_VALUES,
    EFFECT_VALUES,
    ERROR_BUDGET_CHARS,
    EXECUTION_VALUES,
    EXIT_CODE_MEANINGS,
    FAILED_STAGE_VALUES,
    GATE_CONFLICT_VALUES,
    GLOBAL_FLAGS,
    HUMAN_ACTION_VALUES,
    INDEX_BUDGET_CHARS,
    INDEX_COMMAND_KEYS,
    INDEX_GROUP_KEYS,
    MATURITY_VALUES,
    MINIMUM_RISK_TIER,
    OPERATION_DONE_STATUS_VALUES,
    OPERATION_OPTIONAL_KEYS,
    OPERATION_RECORD_KEYS,
    OPERATION_STATUS_VALUES,
    PARAM_OPTIONAL_KEYS,
    PARAM_REQUIRED_KEYS,
    PARAM_TYPES,
    PERSISTENCE_VALUES,
    PROBLEM_VALUES,
    PROFILE_SOURCE_VALUES,
    REFERENCE_COMMAND_KEYS,
    REFERENCE_REQUIRED_KEYS,
    RESOLUTION_OUTCOMES,
    SCHEMA_ENTRY_KEYS,
    SCHEMA_SHAPES,
    SCHEMA_VERSION,
    SCOPE_REFUSAL_STATUS,
    STATE_PROTECTION_VALUES,
    TOKEN_MAX_TTL_SECONDS,
    UPDATE_INSTALL_METHODS,
    UPDATE_OPTIONAL_KEYS,
    UPDATE_REQUIRED_KEYS,
    UPDATE_SIGNATURE_STATUS_VALUES,
    UPDATE_SKILL_SYNC_STATUS_VALUES,
    UPDATE_STAGES,
    UPDATE_STATUS_VALUES,
    VERIFICATION_VALUES,
    WRITE_EFFECTS,
)
from .scope import validate_scope_file

__all__ = [
    "ContractViolation",
    "validate_changelog",
    "validate_context",
    "validate_doctor",
    "validate_envelope",
    "validate_operation_view",
    "validate_reference_command",
    "validate_reference_full",
    "validate_reference_index",
    "validate_scope_file",
    "validate_stream",
]


class ContractViolation(ValueError):
    """A document that breaks the contract; problems lists every finding."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = list(problems)


SNAKE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
UTC_TIME = re.compile(
    r"[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]{1,9})?Z"
)
DATE = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])")
COMMAND_PATH = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*(?: [a-z][a-z0-9]*(?:-[a-z0-9]+)*)*")
TOOL_NAME = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
OP_ID = re.compile(r"op_[a-z2-7]{26}")
BATCH_ID = re.compile(r"opb_[a-z2-7]{26}")
ANY_OP_ID = re.compile(r"opb?_[a-z2-7]{26}")
TOKEN = re.compile(r"ct2_[a-z2-7]{26}_[0-9a-f]{32}")
ERROR_CODE = re.compile(r"E_[A-Z0-9][A-Z0-9_]*")
SHA256 = re.compile(r"[0-9a-f]{64}")
UNTRUSTED_PATH = re.compile(
    r"(?:data(?:\[\])*|error\.details)(?:\.[a-z][a-z0-9]*(?:_[a-z0-9]+)*(?:\[\])*)*"
)
SEMVER = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
MAP_KEYS = {"error_codes", "exit_codes"}
FLAG_NAME = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
FIELD_PATH = re.compile(
    r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*(?:\[\])?(?:\.[a-z][a-z0-9]*(?:_[a-z0-9]+)*(?:\[\])?)*"
)
EVIDENCE = re.compile(r"(?:simulated|live|eval):\S+")
ERROR_EXITS = tuple(code for code in EXIT_CODE_MEANINGS if code != 0)
RESERVED_RULE_CODES = {"E_OUTCOME_UNKNOWN", "E_PARTIAL", "E_VERIFY_FAILED", "E_VERIFY_INCOMPLETE"}
CORE_CONFLICTS = set(CONFLICT_VALUES)
# codes whose dispatch_state has its own rule in error-details.schema.json
OWN_DISPATCH_RULE = RESERVED_RULE_CODES | {"E_USAGE", "E_CONFIRMATION_REQUIRED", "E_NETWORK"}
# check-contract.py's tree rules (CLI-021, CLI-070)
RESERVED_ROOTS = {
    "reference",
    "context",
    "doctor",
    "changelog",
    "update",
    "operation",
    "help",
    "version",
}
SELF_UPDATE = "update"
LIBRARY_BUILTINS = [c for c in BUILTIN_COMMANDS if c != SELF_UPDATE]
TIERS = {"T0": 0, "T1": 1, "T2": 2}
SHADOWED_FLAGS = {f.lstrip("-") for f in GLOBAL_FLAGS} | {"help"}


class _V:
    def __init__(self) -> None:
        self.problems: list[str] = []

    def add(self, path: str, msg: str) -> None:
        self.problems.append(f"{path or '$'}: {msg}")

    def raise_if_any(self) -> None:
        if self.problems:
            raise ContractViolation(self.problems)


def _j(base: str, key: Any) -> str:
    if isinstance(key, int):
        return f"{base}[{key}]"
    return f"{base}.{key}" if base else str(key)


# ---------------------------------------------------------------- strict JSON


def _parse(raw: bytes, v: _V) -> Any:
    if raw.startswith(b"\xef\xbb\xbf"):
        v.add("$", "starts with a byte order mark")
        raw = raw[3:]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        v.add("$", "is not valid UTF-8")
        raise ContractViolation(v.problems) from None
    dups: list[str] = []

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        obj: dict[str, Any] = {}
        for k, val in items:
            if k in obj:
                dups.append(k)
            obj[k] = val
        return obj

    def constant(name: str) -> Any:
        raise ValueError(f"{name} is not JSON")

    try:
        dec = json.JSONDecoder(object_pairs_hook=pairs, parse_constant=constant)
        stripped = text.lstrip(" \t\r\n")
        doc, end = dec.raw_decode(stripped)
    except ValueError as exc:
        v.add("$", f"is not JSON: {exc}")
        raise ContractViolation(v.problems) from None
    if stripped[end:].strip(" \t\r\n"):
        v.add("$", "holds more than one JSON document")
    for k in dups:
        v.add("$", f"the key {json.dumps(k)} appears twice")
    return doc


# ---------------------------------------------------------------- primitives


def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _text(x: Any) -> bool:
    return isinstance(x, str) and len(x) > 0


def _str_list(x: Any) -> bool:
    return isinstance(x, list) and all(isinstance(e, str) for e in x)


def _time(x: Any) -> bool:
    return isinstance(x, str) and bool(UTC_TIME.fullmatch(x))


def _exact(v: _V, p: str, obj: Any, required: list[str] | tuple[str, ...], optional=()) -> bool:
    if not isinstance(obj, dict):
        v.add(p, "must be an object")
        return False
    for k in required:
        if k not in obj:
            v.add(p, f'missing key "{k}"')
    for k in obj:
        if k not in required and k not in optional:
            v.add(p, f'unexpected key "{k}"')
    return True


def _need(v: _V, p: str, obj: dict[str, Any], keys) -> None:
    for k in keys:
        if k not in obj:
            v.add(p, f'missing key "{k}"')


def _effects(v: _V, p: str, x: Any, write: bool) -> None:
    if not isinstance(x, list) or not x:
        v.add(p, "must be a non-empty array of effects")
        return
    if len(set(map(str, x))) != len(x):
        v.add(p, "must not repeat an effect")
    for e in x:
        if e not in EFFECT_VALUES:
            v.add(p, "holds an unknown effect")
    if write and not any(e in WRITE_EFFECTS for e in x):
        v.add(p, "must hold a write effect")


def _dangerous_rule(v: _V, p: str, obj: dict[str, Any]) -> None:
    eff = obj.get("effects")
    if (
        isinstance(eff, list)
        and any(e in ("deliver", "configure") for e in eff)
        and obj.get("dangerous") is not True
    ):
        v.add(p + ".dangerous", "must be true for deliver and configure (CLI-025)")


def _target(v: _V, p: str, x: Any) -> None:
    if not _exact(v, p, x, ["key"], ["kind", "label"]):
        return
    if not _text(x.get("key")):
        v.add(p + ".key", "must be a non-empty string")
    if "kind" in x and not (isinstance(x["kind"], str) and SNAKE.fullmatch(x["kind"])):
        v.add(p + ".kind", "must be a snake_case name")
    if "label" in x and not _text(x["label"]):
        v.add(p + ".label", "must be a non-empty string")


def _changes(v: _V, p: str, x: Any) -> None:
    if not isinstance(x, list):
        v.add(p, "must be an array")
        return
    for i, c in enumerate(x):
        cp = _j(p, i)
        if not _exact(v, cp, c, ["field"], ["before", "after", "add", "remove", "note"]):
            continue
        if not _text(c.get("field")):
            v.add(cp + ".field", "must be a non-empty string")
        if "note" in c and not _text(c["note"]):
            v.add(cp + ".note", "must be a non-empty string")


def _blocks(v: _V, p: str, x: Any, can_apply: Any) -> None:
    if not isinstance(x, list):
        v.add(p, "must be an array")
        return
    if can_apply is False and not x:
        v.add(p, "must say why when can_apply is false")
    if can_apply is True and x:
        v.add(p, "must be empty when can_apply is true")
    for i, b in enumerate(x):
        bp = _j(p, i)
        if not _exact(v, bp, b, ["kind", "message"]):
            continue
        if not (isinstance(b.get("kind"), str) and SNAKE.fullmatch(b["kind"])):
            v.add(bp + ".kind", "must be a snake_case name")
        if not _text(b.get("message")):
            v.add(bp + ".message", "must be a non-empty string")


# ---------------------------------------------------------------- receipts


class Codes:
    """The error codes a receipt rule allows: exact codes and, for "the cause code
    of the failed step", any code the other rules do not reserve."""

    def __init__(self, exact: tuple[str, ...] = (), cause: bool = False) -> None:
        self.exact = frozenset(exact)
        self.cause = cause

    def allows(self, code: str) -> bool:
        return code in self.exact or (self.cause and code not in RESERVED_RULE_CODES)

    def only(self, code: str) -> bool:
        return self.exact == {code} and not self.cause

    def describe(self) -> str:
        parts = sorted(self.exact)
        if self.cause:
            parts.append("the cause code of the failed step")
        return " or ".join(parts) or "ok"


def receipt_rule(receipt: dict[str, Any]) -> tuple[int, bool, Codes]:
    """contract.json receipt_rules (first match wins): the rule number, ok and
    the codes the rule allows."""
    o = receipt["outcome"]
    if (
        "unknown" in (o["execution"], o["change"], o["delivery"], o["persistence"])
        or o["execution"] == "running"
    ):
        return 1, False, Codes(("E_OUTCOME_UNKNOWN",))
    if "partial" in (o["change"], o["delivery"], o["persistence"]):
        return 2, False, Codes(("E_PARTIAL",))
    stopped = o["execution"] in ("failed", "interrupted", "not_started")
    if stopped and _lasting(o):
        return 3, False, Codes(("E_PARTIAL",))
    if stopped:
        return 4, False, Codes(cause=True)  # default E_SERVER
    if o["change"] == "rolled_back":
        # a failed or incomplete read-back (default E_VERIFY_FAILED) or a later step's code
        return 5, False, Codes(("E_VERIFY_FAILED", "E_VERIFY_INCOMPLETE"), cause=True)
    if o["delivery"] == "rejected":
        return 6, False, Codes(cause=True)  # default E_FORBIDDEN
    if o["verification"] == "failed":
        return 7, False, Codes(("E_VERIFY_FAILED",))
    if o["verification"] == "incomplete":
        return 8, False, Codes(("E_VERIFY_INCOMPLETE",))
    return 9, True, Codes()


def _lasting(o: dict[str, Any]) -> bool:
    return (
        o.get("change") == "applied"
        or o.get("delivery") == "delivered"
        or o.get("persistence") == "saved"
    )


def _receipt(v: _V, p: str, x: Any) -> bool:
    """A receipt (BLUEPRINT 4.4); True when its outcome can be ruled on."""
    if not _exact(
        v,
        p,
        x,
        ["operation_id", "command", "target", "effects", "outcome", "observed", "replayed"],
        ["mismatched", "failed_stage", "notes"],
    ):
        return False
    if not (isinstance(x.get("operation_id"), str) and OP_ID.fullmatch(x["operation_id"])):
        v.add(p + ".operation_id", "must be an operation id")
    if not (isinstance(x.get("command"), str) and COMMAND_PATH.fullmatch(x["command"])):
        v.add(p + ".command", "must be a command path")
    _target(v, p + ".target", x.get("target"))
    _effects(v, p + ".effects", x.get("effects"), True)
    observed = x.get("observed")
    if not isinstance(observed, dict):
        v.add(p + ".observed", "must be an object")
        observed = {}
    if not isinstance(x.get("replayed"), bool):
        v.add(p + ".replayed", "must be a boolean")
    out = x.get("outcome")
    good = True
    if not _exact(
        v, p + ".outcome", out, ["execution", "change", "delivery", "persistence", "verification"]
    ):
        return False
    for key, allowed in (
        ("execution", EXECUTION_VALUES),
        ("change", CHANGE_VALUES),
        ("delivery", DELIVERY_VALUES),
        ("persistence", PERSISTENCE_VALUES),
        ("verification", VERIFICATION_VALUES),
    ):
        if out.get(key) not in allowed:
            v.add(f"{p}.outcome.{key}", "must be one of " + ", ".join(allowed))
            good = False
    if not good:
        return False
    mismatched = x.get("mismatched", [])
    if "mismatched" in x:
        if not _str_list(mismatched) or len(set(mismatched)) != len(mismatched):
            v.add(p + ".mismatched", "must be an array of distinct strings")
            mismatched = []
    fs = x.get("failed_stage")
    if "failed_stage" in x and fs not in FAILED_STAGE_VALUES:
        v.add(p + ".failed_stage", "must be one of " + ", ".join(FAILED_STAGE_VALUES))
    effects = x.get("effects") if isinstance(x.get("effects"), list) else []
    if out["execution"] in ("failed", "interrupted") and "failed_stage" not in x:
        v.add(p, 'missing key "failed_stage"')
    if out["execution"] == "completed" and "failed_stage" in x and fs != "verify":
        v.add(p + ".failed_stage", "must be verify when execution completed")
    if fs == "verify" and out["verification"] not in ("failed", "incomplete"):
        v.add(
            p + ".outcome.verification", "must be failed or incomplete when failed_stage is verify"
        )
    if fs == "deliver" and "deliver" not in effects:
        v.add(p + ".failed_stage", "deliver needs the deliver effect")
    if fs == "persist" and not any(e in ("artifact", "persist") for e in effects):
        v.add(p + ".failed_stage", "persist needs the artifact or persist effect")
    if ("deliver" in effects) == (out["delivery"] == "not_applicable"):
        v.add(
            p + ".outcome.delivery", "is not_applicable exactly when the command does not deliver"
        )
    if any(e in ("artifact", "persist") for e in effects) == (
        out["persistence"] == "not_applicable"
    ):
        v.add(
            p + ".outcome.persistence",
            "is not_applicable exactly when the command writes no local file",
        )
    if any(e in ("change", "configure") for e in effects) == (out["change"] == "not_applicable"):
        v.add(
            p + ".outcome.change",
            "is not_applicable exactly when the command changes nothing upstream",
        )
    if out["verification"] == "failed" and not mismatched:
        v.add(p + ".mismatched", "must name the fields when verification failed")
    if out["verification"] == "passed" and mismatched:
        v.add(p + ".mismatched", "must be empty when verification passed")
    if out["verification"] in ("passed", "failed") and not observed:
        v.add(p + ".observed", "must hold the read-back when verification is passed or failed")
    for i, f in enumerate(mismatched or []):
        if isinstance(f, str) and f.split(".", 1)[0] not in observed:
            v.add(_j(p + ".mismatched", i), f"field {f!r} is not in observed (CLI-059)")
    return True


# ---------------------------------------------------------------- previews


def _preview(v: _V, p: str, x: Any) -> bool:
    """A preview (CLI-051, CLI-063). A batch preview cut to its envelope's size
    budget keeps its first targets and items and says in omitted how many
    targets it left out."""
    keys = [
        "operation_id",
        "command",
        "action",
        "target",
        "changes",
        "targets",
        "total",
        "items",
        "omitted",
        "effects",
        "dangerous",
        "inputs",
        "base",
        "destinations",
        "outputs",
        "can_apply",
        "blocked_by",
    ]
    if not _exact(
        v,
        p,
        x,
        [
            "operation_id",
            "command",
            "action",
            "effects",
            "dangerous",
            "inputs",
            "can_apply",
            "blocked_by",
        ],
        keys,
    ):
        return False
    batch = "items" in x
    oid = x.get("operation_id")
    if batch:
        _need(v, p, x, ["targets", "total", "items"])
        if "target" in x or "changes" in x:
            v.add(p, "a batch preview has targets and items, not target and changes")
        if not (isinstance(oid, str) and BATCH_ID.fullmatch(oid)):
            v.add(p + ".operation_id", "must be a batch id")
        if "omitted" in x and not (_is_int(x["omitted"]) and x["omitted"] >= 1):
            v.add(p + ".omitted", "must be a positive integer (absent when nothing was left out)")
    else:
        _need(v, p, x, ["target", "changes"])
        if "targets" in x or "total" in x or "omitted" in x:
            v.add(p, "a single preview has target and changes, not targets")
        if not (isinstance(oid, str) and OP_ID.fullmatch(oid)):
            v.add(p + ".operation_id", "must be an operation id")
    if not (isinstance(x.get("command"), str) and COMMAND_PATH.fullmatch(x["command"])):
        v.add(p + ".command", "must be a command path")
    if not _text(x.get("action")):
        v.add(p + ".action", "must be a non-empty string")
    _effects(v, p + ".effects", x.get("effects"), True)
    if not isinstance(x.get("dangerous"), bool):
        v.add(p + ".dangerous", "must be a boolean")
    _dangerous_rule(v, p, x)
    inputs = x.get("inputs")
    if not isinstance(inputs, dict):
        v.add(p + ".inputs", "must be an object")
    else:
        for k, val in inputs.items():
            if isinstance(val, dict) and "sha256" in val:
                ip = _j(p + ".inputs", k)
                _exact(v, ip, val, ["sha256", "bytes"])
                if not (isinstance(val.get("sha256"), str) and SHA256.fullmatch(val["sha256"])):
                    v.add(ip + ".sha256", "must be 64 lowercase hex characters")
                if not (_is_int(val.get("bytes")) and val["bytes"] >= 0):
                    v.add(ip + ".bytes", "must be a non-negative integer")
    can = x.get("can_apply")
    if not isinstance(can, bool):
        v.add(p + ".can_apply", "must be a boolean")
    _blocks(v, p + ".blocked_by", x.get("blocked_by"), can)
    if "target" in x:
        _target(v, p + ".target", x["target"])
    if "changes" in x:
        _changes(v, p + ".changes", x["changes"])
    if batch:
        targets = x.get("targets")
        if not isinstance(targets, list) or not targets:
            v.add(p + ".targets", "must be a non-empty array")
            targets = []
        for i, t in enumerate(targets):
            _target(v, _j(p + ".targets", i), t)
        if not (_is_int(x.get("total")) and x["total"] >= 1):
            v.add(p + ".total", "must be a positive integer")
        items = x.get("items")
        if not isinstance(items, list) or not items:
            v.add(p + ".items", "must be a non-empty array")
            items = []
        for i, it in enumerate(items):
            ip = _j(p + ".items", i)
            if not _exact(
                v, ip, it, ["target", "operation_id", "changes", "can_apply", "blocked_by"]
            ):
                continue
            _target(v, ip + ".target", it.get("target"))
            if not (
                isinstance(it.get("operation_id"), str) and OP_ID.fullmatch(it["operation_id"])
            ):
                v.add(ip + ".operation_id", "must be an operation id")
            _changes(v, ip + ".changes", it.get("changes"))
            if not isinstance(it.get("can_apply"), bool):
                v.add(ip + ".can_apply", "must be a boolean")
            _blocks(v, ip + ".blocked_by", it.get("blocked_by"), it.get("can_apply"))
    if "base" in x:
        b = x["base"]
        if _exact(v, p + ".base", b, ["version", "source"]):
            if not _text(b.get("version")):
                v.add(p + ".base.version", "must be a non-empty string")
            if not (isinstance(b.get("source"), str) and SNAKE.fullmatch(b["source"])):
                v.add(p + ".base.source", "must be a snake_case name")
    for key in ("destinations", "outputs"):
        if key in x and not (isinstance(x[key], list) and all(_text(d) for d in x[key])):
            v.add(f"{p}.{key}", "must be an array of non-empty strings")
    eff = x.get("effects") if isinstance(x.get("effects"), list) else []
    if "deliver" in eff and not (isinstance(x.get("destinations"), list) and x["destinations"]):
        v.add(p + ".destinations", "a deliver preview lists its destinations (decision 15)")
    if any(e in ("artifact", "persist") for e in eff) and not (
        isinstance(x.get("outputs"), list) and x["outputs"]
    ):
        v.add(p + ".outputs", "an artifact or persist preview lists its outputs (decision 15)")
    if batch and not v.problems:
        _check_preview_batch(v, p, x)
    return True


def _check_preview_batch(v: _V, p: str, x: dict[str, Any]) -> None:
    targets, items = x["targets"], x["items"]
    omitted = x.get("omitted", 0)
    if len(targets) != len(items):
        v.add(
            p + ".items",
            f"follow the {len(targets)} listed targets one to one (CLI-063); there are "
            f"{len(items)}",
        )
    elif [t["key"] for t in targets] != [it["target"]["key"] for it in items]:
        v.add(p + ".items", "follow the targets one to one, in input order (CLI-063)")
    if x["total"] != len(targets) + omitted:
        if omitted:
            v.add(
                p + ".omitted",
                f"is {omitted}, but total {x['total']} less the {len(targets)} listed targets is "
                f"{x['total'] - len(targets)} (CLI-063)",
            )
        else:
            v.add(
                p + ".total",
                f"counts the {len(targets)} targets one to one; a preview that leaves targets out "
                "says how many in omitted (CLI-063)",
            )
    keys = [t["key"] for t in targets]
    if len(set(keys)) != len(keys):
        v.add(p + ".targets", "are de-duplicated (CLI-063)")
    ops = [it["operation_id"] for it in items]
    if len(set(ops)) != len(ops) or x["operation_id"] in ops:
        v.add(
            p + ".items",
            "every item has its own operation id, distinct from the batch id (CLI-063)",
        )
    if x["can_apply"] and not all(it["can_apply"] for it in items):
        v.add(p + ".can_apply", "a batch that can be applied has no blocked item")


def _token_fields(v: _V, p: str, d: dict[str, Any]) -> None:
    preview = d.get("preview") if isinstance(d.get("preview"), dict) else {}
    can = preview.get("can_apply")
    tok = d.get("confirm_token")
    exp = d.get("expires_at")
    if can is False:
        if tok is not None:
            v.add(p + ".confirm_token", "must be null when the preview cannot apply")
        if exp is not None:
            v.add(p + ".expires_at", "must be null when the preview cannot apply")
    else:
        if not isinstance(tok, str):
            v.add(p + ".confirm_token", "must be a token when the preview can apply")
        if not isinstance(exp, str):
            v.add(p + ".expires_at", "must be a time when the preview can apply")
    if tok is not None and not (isinstance(tok, str) and TOKEN.fullmatch(tok)):
        v.add(p + ".confirm_token", "is not a canonical token")
    if exp is not None and not _time(exp):
        v.add(p + ".expires_at", "must be an ISO 8601 UTC time")
    oid = d.get("operation_id")
    if not (isinstance(oid, str) and ANY_OP_ID.fullmatch(oid)):
        v.add(p + ".operation_id", "must be an operation id")
        return
    if isinstance(preview.get("operation_id"), str) and preview["operation_id"] != oid:
        v.add(p + ".operation_id", "is the preview's operation id (CLI-051)")
    if isinstance(tok, str) and TOKEN.fullmatch(tok) and tok[4:30] != oid.split("_", 1)[1]:
        v.add(p + ".confirm_token", "carries the id part of its operation id (CLI-055)")


# ---------------------------------------------------------------- batches


def _classify_item(item: dict[str, Any]) -> str:
    if item.get("ok"):
        return "succeeded"
    error, receipt = item.get("error"), item.get("receipt")
    if error and error.get("code") == "E_OUTCOME_UNKNOWN":
        return "unknown"
    if receipt and receipt_rule(receipt)[0] == 1:
        return "unknown"
    if not error and (
        not receipt
        or (receipt["outcome"]["execution"] == "not_started" and not _lasting(receipt["outcome"]))
    ):
        return "skipped"
    return "failed"


def _item_applied(item: dict[str, Any]) -> bool:
    return bool(item.get("ok")) or bool(
        item.get("receipt") and _lasting(item["receipt"]["outcome"])
    )


def batch_expected_code(batch: dict[str, Any]) -> str | None:
    """contract.json batch.ok_rule: None when every item succeeded, else the code
    the batch must carry ("" when the listed items do not say). A shortened list
    (omitted) is judged by its summary, which counts every item."""
    items, summary = batch["items"], batch["summary"]
    kinds = [_classify_item(it) for it in items]
    unknown, applied = "unknown" in kinds, any(_item_applied(it) for it in items)
    if "omitted" in batch:
        if summary["succeeded"] == summary["total"] and all(k == "succeeded" for k in kinds):
            return None
        unknown, applied = unknown or summary["unknown"] > 0, applied or summary["succeeded"] > 0
    elif all(k == "succeeded" for k in kinds):
        return None
    if unknown:
        return "E_OUTCOME_UNKNOWN"
    if applied:
        return "E_PARTIAL"
    for it in items:
        if it.get("error"):
            return it["error"]["code"]
    return ""


BATCH_COUNTS = ("succeeded", "failed", "unknown", "skipped")


def _batch(v: _V, p: str, batch: dict[str, Any]) -> bool:
    """A batch result (CLI-063): items in input order, each under its own op_ id,
    and a summary that counts every target. A list cut to the size budget says
    in omitted how many items it left out and keeps the items with ok false
    first, all of them before any item that succeeded."""
    items, summary = batch.get("items"), batch.get("summary")
    good = True
    if not isinstance(items, list) or not items:
        v.add(p + ".items", "must be a non-empty array")
        return False
    if "omitted" in batch and not (_is_int(batch["omitted"]) and batch["omitted"] >= 1):
        v.add(p + ".omitted", "must be a positive integer (absent when nothing was left out)")
        good = False
    for i, it in enumerate(items):
        ip = _j(p + ".items", i)
        if not _exact(v, ip, it, ["target", "operation_id", "ok"], ["receipt", "error", "result"]):
            good = False
            continue
        if not _text(it.get("target")):
            v.add(ip + ".target", "must be the target key")
        if not (isinstance(it.get("operation_id"), str) and OP_ID.fullmatch(it["operation_id"])):
            v.add(ip + ".operation_id", "must be an operation id")
        if not isinstance(it.get("ok"), bool):
            v.add(ip + ".ok", "must be a boolean")
            good = False
        if it.get("ok") is True and "error" in it:
            v.add(ip + ".error", "a successful item has no error")
        if "receipt" in it and not _receipt(v, ip + ".receipt", it["receipt"]):
            good = False
        if "error" in it:
            e = it["error"]
            if _exact(v, ip + ".error", e, ["code", "retryable", "message"]):
                if not (isinstance(e.get("code"), str) and ERROR_CODE.fullmatch(e["code"])):
                    v.add(ip + ".error.code", "must match ^E_[A-Z0-9][A-Z0-9_]*$")
                    good = False
                if not isinstance(e.get("retryable"), bool):
                    v.add(ip + ".error.retryable", "must be a boolean")
                elif (
                    e.get("code") in CORE_CODES
                    and e["retryable"] != CORE_CODES[e["code"]].retryable
                ):
                    v.add(
                        ip + ".error.retryable",
                        f"must be {str(CORE_CODES[e['code']].retryable).lower()} for {e['code']}",
                    )
                if not _text(e.get("message")):
                    v.add(ip + ".error.message", "must be a non-empty string")
            else:
                good = False
    if _exact(v, p + ".summary", summary, ["total", "succeeded", "failed", "unknown", "skipped"]):
        if not (_is_int(summary.get("total")) and summary["total"] >= 1):
            v.add(p + ".summary.total", "must be a positive integer")
        for k in BATCH_COUNTS:
            if not (_is_int(summary.get(k)) and summary[k] >= 0):
                v.add(f"{p}.summary.{k}", "must be a non-negative integer")
    else:
        good = False
    if not good or v.problems:
        return False
    keys = [it["target"] for it in items]
    if len(set(keys)) != len(keys):
        v.add(p + ".items", "targets are de-duplicated before execution (CLI-063)")
    ops = [it["operation_id"] for it in items]
    if len(set(ops)) != len(ops) or batch.get("operation_id") in ops:
        v.add(
            p + ".items",
            "every item runs under its own operation id, distinct from the batch id (CLI-063)",
        )
    for i, it in enumerate(items):
        ip = _j(p + ".items", i)
        r = it.get("receipt")
        if r:
            if r["operation_id"] != it["operation_id"]:
                v.add(ip + ".receipt", "the item and its receipt name the same operation")
            number, ok, codes = receipt_rule(r)
            if ok != it["ok"]:
                v.add(
                    ip + ".ok",
                    f"follows its receipt (receipt rule {number} gives ok {str(ok).lower()})",
                )
            elif not ok and "error" in it and not codes.allows(it["error"]["code"]):
                v.add(ip + ".error.code", f"receipt rule {number} gives {codes.describe()}")
        if not it["ok"] and "error" not in it and _classify_item(it) != "skipped":
            v.add(ip, "an attempted item that did not succeed carries its error code")
    tally = Counter(_classify_item(it) for it in items)
    omitted = batch.get("omitted")
    if omitted is None:
        expected = {"total": len(items), **{k: tally[k] for k in BATCH_COUNTS}}
        for k, val in expected.items():
            if summary.get(k) != val:
                v.add(
                    f"{p}.summary.{k}",
                    f"is {summary.get(k)} but the items give {val} (CLI-063: counts equal the item "
                    "tally)",
                )
        return True
    if summary["total"] != len(items) + omitted:
        v.add(
            p + ".omitted",
            f"is {omitted}, but summary.total {summary['total']} less the {len(items)} listed "
            "items is "
            f"{summary['total'] - len(items)} (CLI-063)",
        )
    if sum(summary[k] for k in BATCH_COUNTS) != summary["total"]:
        v.add(
            p + ".summary",
            "counts every item exactly once: succeeded + failed + unknown + skipped = total "
            "(CLI-063)",
        )
    for k in BATCH_COUNTS:
        if summary[k] < tally[k]:
            v.add(
                f"{p}.summary.{k}",
                f"is {summary[k]}, but {tally[k]} of the listed items are {k} (CLI-063)",
            )
    not_ok = sum(summary[k] for k in BATCH_COUNTS[1:])
    listed_not_ok = sum(tally[k] for k in BATCH_COUNTS[1:])
    succeeded = [_classify_item(it) == "succeeded" for it in items]
    if succeeded != sorted(succeeded):
        v.add(
            p + ".items",
            "a shortened list puts the items with ok false first, before any that succeeded "
            "(CLI-063)",
        )
    elif tally["succeeded"] and listed_not_ok < not_ok:
        v.add(
            p + ".items",
            "a shortened list keeps every item with ok false before any that succeeded; it leaves "
            "out "
            f"{not_ok - listed_not_ok} of the {not_ok} items with ok false but lists "
            f"{tally['succeeded']} that succeeded (CLI-063)",
        )
    return True


# ---------------------------------------------------------------- pages


def _page(v: _V, p: str, x: dict[str, Any]) -> None:
    keys = [
        "items",
        "count",
        "has_more",
        "next_cursor",
        "next_offset",
        "continuation_reason",
        "offset",
        "truncated",
        "truncated_reason",
        "scope",
        "sort",
        "total",
    ]
    if not _exact(v, p, x, ["items", "count", "has_more"], keys):
        return
    if not isinstance(x.get("items"), list):
        v.add(p + ".items", "must be an array")
    if not (_is_int(x.get("count")) and x["count"] >= 0):
        v.add(p + ".count", "must be a non-negative integer")
    more = x.get("has_more")
    if not isinstance(more, bool):
        v.add(p + ".has_more", "must be a boolean")
    conts = [k for k in ("next_cursor", "next_offset", "continuation_reason") if k in x]
    if more is True and len(conts) != 1:
        v.add(p, "has_more needs exactly one of next_cursor, next_offset or continuation_reason")
    if more is False and conts:
        v.add(p, "a complete list has no continuation")
    if x.get("truncated") is True:
        if not _text(x.get("truncated_reason")):
            v.add(p + ".truncated_reason", "is required when truncated is true")
        if more is not True:
            v.add(p + ".has_more", "is true when the list was truncated")
    elif "truncated_reason" in x:
        v.add(p + ".truncated_reason", "is only given when truncated is true")
    for k in ("next_offset", "offset", "total"):
        if k in x and not (_is_int(x[k]) and x[k] >= 0):
            v.add(f"{p}.{k}", "must be a non-negative integer")
    for k in ("next_cursor", "continuation_reason", "sort"):
        if k in x and not _text(x[k]):
            v.add(f"{p}.{k}", "must be a non-empty string")
    if "scope" in x and not isinstance(x["scope"], dict):
        v.add(p + ".scope", "must be an object")
    if "truncated" in x and not isinstance(x["truncated"], bool):
        v.add(p + ".truncated", "must be a boolean")
    if (
        isinstance(x.get("items"), list)
        and _is_int(x.get("count"))
        and x["count"] != len(x["items"])
    ):
        v.add(p + ".count", f"is {x['count']} but there are {len(x['items'])} items (CLI-041)")
    if _is_int(x.get("next_offset")) and _is_int(x.get("offset")) and _is_int(x.get("count")):
        if x["next_offset"] < x["offset"] + max(x["count"], 1):
            v.add(p + ".next_offset", "advances past offset + count (CLI-041)")
    if _is_int(x.get("total")) and _is_int(x.get("count")) and _is_int(x.get("offset", 0)):
        end = x.get("offset", 0) + x["count"]
        if x["total"] < end:
            v.add(
                p + ".total",
                f"is {x['total']}, less than the {end} items read up to the end of this page "
                "(CLI-041)",
            )
        elif x.get("has_more") is False and "offset" in x and x["total"] != end:
            v.add(
                p + ".total",
                f"has_more is false, so this page ends the list at offset + count = {end}; total "
                f"says {x['total']} (CLI-041)",
            )


# ---------------------------------------------------------------- scope refusals


def _scope_object(v: _V, p: str, x: Any) -> None:
    """A scope file object (grant_example)."""
    req = ["scope_id", "tool", "granted_by", "granted_at", "expires_at", "effects", "dangerous"]
    opt = ["targets", "destinations", "outputs", "roots", "limits", "revoked", "notes"]
    if not _exact(v, p, x, req, opt):
        return
    for k in ("scope_id", "granted_by"):
        if k in x and not _text(x[k]):
            v.add(f"{p}.{k}", "must be a non-empty string")
    if "tool" in x and not (isinstance(x["tool"], str) and TOOL_NAME.fullmatch(x["tool"])):
        v.add(p + ".tool", "must be a kebab-case tool name")
    if "granted_at" in x and not _time(x["granted_at"]):
        v.add(p + ".granted_at", "must be an ISO 8601 UTC time")
    if "expires_at" in x and x["expires_at"] is not None and not _time(x["expires_at"]):
        v.add(p + ".expires_at", "must be an ISO 8601 UTC time or null")
    if "effects" in x:
        _effects(v, p + ".effects", x["effects"], False)
    if "dangerous" in x and not isinstance(x["dangerous"], bool):
        v.add(p + ".dangerous", "must be a boolean")
    if "targets" in x and not isinstance(x["targets"], list):
        v.add(p + ".targets", "must be an array")
    for i, t in enumerate(x.get("targets", []) if isinstance(x.get("targets"), list) else []):
        tp = _j(p + ".targets", i)
        if _exact(v, tp, t, ["key"], ["match"]):
            if not _text(t.get("key")):
                v.add(tp + ".key", "must be a non-empty string")
            if "match" in t and t["match"] not in ("subtree", "exact"):
                v.add(tp + ".match", "must be subtree or exact")
    for k in ("destinations", "outputs", "roots"):
        if k in x and not (isinstance(x[k], list) and all(_text(d) for d in x[k])):
            v.add(f"{p}.{k}", "must be an array of non-empty strings")
    if "limits" in x and _exact(v, p + ".limits", x["limits"], [], ["max_operations"]):
        mo = x["limits"].get("max_operations")
        if "max_operations" in x["limits"] and not (_is_int(mo) and mo >= 1):
            v.add(p + ".limits.max_operations", "must be a positive integer")
    if "revoked" in x and not isinstance(x["revoked"], bool):
        v.add(p + ".revoked", "must be a boolean")
    if "notes" in x and not isinstance(x["notes"], str):
        v.add(p + ".notes", "must be a string")
    if isinstance(x.get("expires_at"), str) and isinstance(x.get("granted_at"), str):
        a, b = parse_time(x["granted_at"]), parse_time(x["expires_at"])
        if a is not None and b is not None and b <= a:
            v.add(p + ".expires_at", "a scope expires after it was granted (SEC-011)")
    for k in ("outputs", "roots"):
        for i, path in enumerate(x[k] if isinstance(x.get(k), list) else []):
            if isinstance(path, str) and not _is_absolute(path):
                v.add(
                    _j(f"{p}.{k}", i),
                    "outputs and roots are absolute directories, checked after symlink "
                    "resolution (SEC-012)",
                )


def _scope_refusal(v: _V, p: str, x: Any) -> None:
    if not _exact(v, p, x, ["status", "path"], ["scope_id", "missing", "grant_example"]):
        return
    status = x.get("status")
    if status not in SCOPE_REFUSAL_STATUS:
        v.add(p + ".status", "must be one of " + ", ".join(SCOPE_REFUSAL_STATUS))
    if not _text(x.get("path")):
        v.add(p + ".path", "must be a non-empty string")
    sid = x.get("scope_id")
    if "scope_id" in x and sid is not None and not _text(sid):
        v.add(p + ".scope_id", "must be a non-empty string or null")
    if status in ("missing", "invalid") and sid is not None:
        v.add(p + ".scope_id", "is null when the scope is missing or invalid")
    if status in ("expired", "revoked", "insufficient", "budget_spent") and not isinstance(
        sid, str
    ):
        v.add(p + ".scope_id", "names the scope that refused")
    if status == "insufficient" and "missing" not in x:
        v.add(p, 'missing key "missing"')
    if "missing" in x:
        mi = x["missing"]
        if not isinstance(mi, dict) or not mi:
            v.add(p + ".missing", "must be a non-empty object")
        else:
            if "effects" in mi:
                e = mi["effects"]
                if not (
                    isinstance(e, list)
                    and all(x in EFFECT_VALUES for x in e)
                    and len(set(e)) == len(e)
                ):
                    v.add(p + ".missing.effects", "must be distinct effects")
            if "dangerous" in mi and not isinstance(mi["dangerous"], bool):
                v.add(p + ".missing.dangerous", "must be a boolean")
            if "target" in mi and not _text(mi["target"]):
                v.add(p + ".missing.target", "must be a non-empty string")
            for k in ("destinations", "outputs", "roots"):
                if k in mi and not (isinstance(mi[k], list) and all(_text(d) for d in mi[k])):
                    v.add(f"{p}.missing.{k}", "must be an array of non-empty strings")
    if "grant_example" in x:
        _scope_object(v, p + ".grant_example", x["grant_example"])


# ---------------------------------------------------------------- errors


def _skill_sync_command(v: _V, p: str, d: dict[str, Any], partial: bool) -> None:
    """F10 (P16): skill_sync_command comes only when the Skill sync could not finish
    (skill_sync_status failed, or the E_PARTIAL at skill_sync, partial), and then
    always; never with skipped, not_needed or synced."""
    status = d.get("skill_sync_status")
    if status in ("skipped", "not_needed", "synced") and "skill_sync_command" in d:
        v.add(
            p + ".skill_sync_command",
            "comes only when the Skill sync could not finish, not with skill_sync_status "
            f"{status} (F10)",
        )
    if (status == "failed" or partial) and "skill_sync_command" not in d:
        v.add(p, 'missing key "skill_sync_command": the Skill sync could not finish (F10)')


def _update_failure(v: _V, p: str, d: dict[str, Any], code: str = "") -> None:
    _need(v, p, d, ["stage", "current_version", "binary_replaced", "skill_sync_status"])
    _skill_sync_command(v, p, d, code == "E_PARTIAL" and d.get("stage") == "skill_sync")
    stages = (
        "discover",
        "download",
        "verify_signature",
        "verify_checksum",
        "replace",
        "skill_sync",
    )
    if "stage" in d and d["stage"] not in stages:
        v.add(p + ".stage", "must be an update stage")
    for k in ("current_version", "target_version", "previous_version"):
        if k in d and not (isinstance(d[k], str) and SEMVER.fullmatch(d[k])):
            v.add(f"{p}.{k}", "must be a semantic version")
    if "skill_sync_status" in d and d["skill_sync_status"] not in (
        "synced",
        "not_needed",
        "failed",
        "skipped",
    ):
        v.add(p + ".skill_sync_status", "must be a Skill sync status")
    if d.get("binary_replaced") is True:
        _need(v, p, d, ["target_version"])
        if d.get("stage") not in ("replace", "skill_sync"):
            v.add(p + ".stage", "is replace or skill_sync once the binary is replaced")
        if d.get("skill_sync_status") not in ("failed", "skipped"):
            v.add(p + ".skill_sync_status", "is failed or skipped for a partial update")
        if d.get("update_available") not in (None, False):
            v.add(p + ".update_available", "is false once the binary is replaced")
        if "target_version" in d and d.get("current_version") != d["target_version"]:
            v.add(
                p + ".current_version",
                "once the binary is replaced the running version is the target (CLI-070)",
            )
    else:
        if d.get("stage") == "skill_sync":
            v.add(p + ".stage", "skill_sync comes after the binary was replaced")
        if "status" in d:
            v.add(p + ".status", "only a partial update has a status")


def _error_object(v: _V, p: str, x: Any) -> None:
    if not _exact(v, p, x, ["code", "message", "retryable", "details"]):
        return
    code = x.get("code")
    if not (isinstance(code, str) and ERROR_CODE.fullmatch(code)):
        v.add(p + ".code", "must match ^E_[A-Z0-9][A-Z0-9_]*$")
        code = ""
    if not _text(x.get("message")):
        v.add(p + ".message", "must be a non-empty string")
    retry = x.get("retryable")
    if not isinstance(retry, bool):
        v.add(p + ".retryable", "must be a boolean")
    elif code in CORE_CODES and retry != CORE_CODES[code].retryable:
        v.add(p + ".retryable", f"must be {str(CORE_CODES[code].retryable).lower()} for {code}")
    d = x.get("details")
    if not isinstance(d, dict):
        v.add(p + ".details", "must be an object")
        return
    _details(v, p + ".details", code, d)


def _details(v: _V, p: str, code: str, d: dict[str, Any]) -> None:
    _need(v, p, d, ["dispatch_state", "next_action"])
    ds = d.get("dispatch_state")
    if "dispatch_state" in d and ds not in DISPATCH_STATES:
        v.add(p + ".dispatch_state", "must be one of " + ", ".join(DISPATCH_STATES))
    if "next_action" in d and not _text(d["next_action"]):
        v.add(p + ".next_action", "must be a non-empty string")
    problem = d.get("problem")
    if "problem" in d and problem not in PROBLEM_VALUES:
        v.add(p + ".problem", "must be one of the contract's problem values")
    for k in ("parameter", "usage", "lookup", "resume"):
        if k in d and not _text(d[k]):
            v.add(f"{p}.{k}", "must be a non-empty string")
    if "did_you_mean" in d and not (
        isinstance(d["did_you_mean"], list)
        and d["did_you_mean"]
        and all(_text(s) for s in d["did_you_mean"])
    ):
        v.add(p + ".did_you_mean", "must be a non-empty array of strings")
    if "available" in d and not (
        isinstance(d["available"], list) and all(_text(s) for s in d["available"])
    ):
        v.add(p + ".available", "must be an array of strings")
    if "upstream_message" in d and not isinstance(d["upstream_message"], str):
        v.add(p + ".upstream_message", "must be a string")
    if "http_status" in d and not (_is_int(d["http_status"]) and 100 <= d["http_status"] <= 599):
        v.add(p + ".http_status", "must be an HTTP status")
    conflict = d.get("conflict")
    if "conflict" in d and conflict not in CORE_CONFLICTS:
        v.add(p + ".conflict", "must be one of the contract's conflict values")
    for k in ("active_operation", "operation_id"):
        if k in d and not (isinstance(d[k], str) and ANY_OP_ID.fullmatch(d[k])):
            v.add(f"{p}.{k}", "must be an operation id")
    if "candidates" in d:
        cands = d["candidates"]
        if not isinstance(cands, list):
            v.add(p + ".candidates", "must be an array")
            cands = []
        for i, c in enumerate(cands):
            cp = _j(p + ".candidates", i)
            if _exact(v, cp, c, ["ref", "label"]):
                for k in ("ref", "label"):
                    if not _text(c.get(k)):
                        v.add(f"{cp}.{k}", "must be a non-empty string")
        if code == "E_AMBIGUOUS" and len(cands) < 2:
            v.add(p + ".candidates", "an ambiguous name has at least two candidates")
    if "scope" in d:
        _scope_refusal(v, p + ".scope", d["scope"])
    if "required" in d and not isinstance(d["required"], (str, dict)):
        v.add(p + ".required", "must be a string or an object")
    if "preview" in d:
        _preview(v, p + ".preview", d["preview"])
    if (
        "confirm_token" in d
        and d["confirm_token"] is not None
        and not (isinstance(d["confirm_token"], str) and TOKEN.fullmatch(d["confirm_token"]))
    ):
        v.add(p + ".confirm_token", "is not a canonical token")
    if "expires_at" in d and d["expires_at"] is not None and not _time(d["expires_at"]):
        v.add(p + ".expires_at", "must be an ISO 8601 UTC time")
    receipt_ok = "receipt" in d and _receipt(v, p + ".receipt", d["receipt"])
    if "items" in d and "summary" not in d:
        v.add(p, 'missing key "summary" (items come with their summary)')
    if "omitted" in d and "items" not in d:
        v.add(p, 'missing key "items" (omitted counts the items a list left out)')
    if "action" in d and code == "E_HUMAN_REQUIRED" and d["action"] not in HUMAN_ACTION_VALUES:
        v.add(p + ".action", "must be one of " + ", ".join(HUMAN_ACTION_VALUES))
    if "retry_after_ms" in d and not (_is_int(d["retry_after_ms"]) and d["retry_after_ms"] >= 0):
        v.add(p + ".retry_after_ms", "must be a non-negative integer")
    if "stage" in d and "binary_replaced" in d:
        _update_failure(v, p, d, code)
    # dispatched means a write's request reached the upstream, so the error says
    # what happened to it (error-details schema); the listed codes have their own rule
    if (
        ds == "dispatched"
        and code not in OWN_DISPATCH_RULE
        and not ("receipt" in d or "items" in d or ("stage" in d and "binary_replaced" in d))
    ):
        v.add(
            p + ".dispatch_state",
            "is dispatched only for a write, whose error carries its receipt or items (CLI-011)",
        )
    # per code
    if code == "E_USAGE":
        _need(v, p, d, ["problem"])
        if ds != "not_started":
            v.add(p + ".dispatch_state", "is not_started for a usage error")
        if problem in ("confirmation_required", "dangerous_flag_required"):
            v.add(p + ".problem", "belongs to E_CONFIRMATION_REQUIRED")
        if problem in ("unknown_flag", "missing_parameter") and "parameter" not in d:
            v.add(p, 'missing key "parameter"')
    elif code == "E_VALIDATION":
        _need(v, p, d, ["problem"])
        if problem in ("confirmation_required", "dangerous_flag_required"):
            v.add(p + ".problem", "belongs to E_CONFIRMATION_REQUIRED")
    elif code == "E_CONFIRMATION_REQUIRED":
        _need(v, p, d, ["problem"])
        if ds != "not_started":
            v.add(p + ".dispatch_state", "is not_started when confirmation is required")
        if problem not in ("confirmation_required", "dangerous_flag_required"):
            v.add(p + ".problem", "is confirmation_required or dangerous_flag_required")
        if problem == "confirmation_required":
            _need(v, p, d, ["preview", "confirm_token", "expires_at", "operation_id"])
            _token_fields(v, p, d)
    elif code == "E_CONFLICT":
        _need(v, p, d, ["conflict"])
        if conflict in GATE_CONFLICT_VALUES and ds != "not_started":
            v.add(
                p + ".dispatch_state",
                f"is not_started: the gate finds {conflict} before anything is sent (CLI-013)",
            )
        if conflict == "token_mismatch" and "parameter" not in d:
            v.add(p, 'missing key "parameter"')
        # lock_held names the running operation only when the lock's owner is known (P15).
        if conflict == "unresolved_operation" and "active_operation" not in d:
            v.add(p, 'missing key "active_operation"')
    elif code == "E_FORBIDDEN":
        if (
            ds == "not_started"
            and "scope" not in d
            and "http_status" not in d
            and "required" not in d
        ):
            v.add(p, 'missing key "scope" (a local refusal comes from the scope)')
    elif code == "E_AMBIGUOUS":
        _need(v, p, d, ["candidates"])
    elif code == "E_OUTCOME_UNKNOWN":
        _need(v, p, d, ["operation_id", "lookup"])
        if ds not in ("dispatched", "unknown"):
            v.add(p + ".dispatch_state", "is dispatched or unknown for an unknown outcome")
        if isinstance(d.get("lookup"), str) and isinstance(d.get("operation_id"), str):
            if d["operation_id"] not in d["lookup"]:
                v.add(p + ".lookup", "must be the command that looks up details.operation_id")
    elif code == "E_PARTIAL":
        if ds != "dispatched":
            v.add(p + ".dispatch_state", "is dispatched when part of the change took effect")
        if not ("receipt" in d or "items" in d or ("stage" in d and "binary_replaced" in d)):
            v.add(p, "needs receipt, items, or the update failure details")
        if "stage" in d and "binary_replaced" in d and d["binary_replaced"] is not True:
            v.add(p + ".binary_replaced", "is true for a partial update")
    elif code in ("E_VERIFY_FAILED", "E_VERIFY_INCOMPLETE"):
        _need(v, p, d, ["receipt"])
        if ds != "dispatched":
            v.add(p + ".dispatch_state", "is dispatched once the change was applied")
    elif code == "E_HUMAN_REQUIRED":
        _need(v, p, d, ["action", "resume"])
    elif code == "E_NETWORK":
        if ds != "not_started":
            v.add(
                p + ".dispatch_state", "is not_started: E_NETWORK means nothing was sent (CLI-015)"
            )
    elif code == "E_INTEGRITY":
        _need(v, p, d, ["stage", "current_version", "binary_replaced", "skill_sync_status"])
        # replace: an unsafe archive entry, or a new binary reporting another version
        if d.get("stage") not in ("verify_signature", "verify_checksum", "replace"):
            v.add(p + ".stage", "is verify_signature, verify_checksum or replace")
        if d.get("binary_replaced") is not False:
            v.add(p + ".binary_replaced", "is false: the install was refused")
    if v.problems:
        return
    # semantics
    if receipt_ok:
        r = d["receipt"]
        number, ok, codes = receipt_rule(r)
        if ok:
            v.add(
                p + ".receipt",
                "receipt_rules make this receipt ok; a successful write is not an error (CLI-059)",
            )
        elif not codes.allows(code):
            v.add(
                p.rsplit(".", 1)[0] + ".code",
                f"receipt rule {number} gives {codes.describe()}, not {code} (CLI-059)",
            )
        if "operation_id" in d and d["operation_id"] != r["operation_id"]:
            v.add(p + ".operation_id", "names the receipt's operation")
    if "items" in d and "summary" in d and _batch(v, p, d) and not v.problems:
        expected = batch_expected_code(d)
        if expected is None:
            v.add(p + ".items", "every item succeeded, so the batch is ok true (CLI-063)")
        elif expected and code != expected:
            v.add(
                p.rsplit(".", 1)[0] + ".code",
                f"batch.ok_rule gives {expected} for these items, not {code} (CLI-063)",
            )


# ---------------------------------------------------------------- operations


def _operation(v: _V, p: str, x: Any) -> None:
    if not _exact(v, p, x, list(OPERATION_RECORD_KEYS), list(OPERATION_OPTIONAL_KEYS)):
        return
    oid = x.get("operation_id")
    if not (isinstance(oid, str) and ANY_OP_ID.fullmatch(oid)):
        v.add(p + ".operation_id", "must be an operation id")
        return
    if not (isinstance(x.get("command"), str) and COMMAND_PATH.fullmatch(x["command"])):
        v.add(p + ".command", "must be a command path")
    status = x.get("status")
    if status not in OPERATION_STATUS_VALUES:
        v.add(p + ".status", "must be one of " + ", ".join(OPERATION_STATUS_VALUES))
    if x.get("target") is not None:
        _target(v, p + ".target", x["target"])
    _effects(v, p + ".effects", x.get("effects"), True)
    sid = x.get("scope_id")
    if sid is not None and not _text(sid):
        v.add(p + ".scope_id", "must be a non-empty string or null")
    if not _time(x.get("created_at")):
        v.add(p + ".created_at", "must be an ISO 8601 UTC time")
    for k in ("expires_at", "dispatched_at", "finished_at"):
        if x.get(k) is not None and not _time(x[k]):
            v.add(f"{p}.{k}", "must be an ISO 8601 UTC time or null")
    if x.get("preview") is not None:
        _preview(v, p + ".preview", x["preview"])
    if x.get("receipt") is not None:
        _receipt(v, p + ".receipt", x["receipt"])
    res = x.get("resolution")
    if res is not None and _exact(
        v, p + ".resolution", res, ["outcome", "evidence", "resolved_at"]
    ):
        if res.get("outcome") not in RESOLUTION_OUTCOMES:
            v.add(p + ".resolution.outcome", "must be applied, not_applied or partial")
        if not _text(res.get("evidence")):
            v.add(p + ".resolution.evidence", "must be a non-empty string")
        if not _time(res.get("resolved_at")):
            v.add(p + ".resolution.resolved_at", "must be an ISO 8601 UTC time")
    batch = oid.startswith("opb_")
    if x.get("batch_id") is not None and not (
        isinstance(x["batch_id"], str) and BATCH_ID.fullmatch(x["batch_id"])
    ):
        v.add(p + ".batch_id", "must be a batch id")
    items = x.get("items")
    if batch:
        if "items" in x and items is None:
            # Only a batch record that could not be read has null items (CLI-058): its
            # outcome is unknown, or resolved once operation resolve recorded it.
            if status not in ("unknown", "resolved"):
                v.add(
                    p + ".status",
                    "is unknown or resolved when the batch record could not be read (items is "
                    "null)",
                )
        elif not isinstance(items, list) or not items:
            v.add(p + ".items", "a batch lists its items")
        for k in ("target", "receipt", "batch_id"):
            if x.get(k) is not None:
                v.add(f"{p}.{k}", "is null for a batch")
    elif items is not None:
        v.add(p + ".items", "is null for a single operation")
    if "items_omitted" in x:
        if not batch:
            v.add(
                p + ".items_omitted",
                "is absent for a single operation; it counts the items a shortened batch leaves "
                "out",
            )
        elif not (_is_int(x["items_omitted"]) and x["items_omitted"] >= 1):
            v.add(p + ".items_omitted", "must be an integer of at least 1")
    if isinstance(items, list):
        for i, it in enumerate(items):
            ip = _j(p + ".items", i)
            if _exact(v, ip, it, ["operation_id", "target", "status"], ["receipt"]):
                if not (
                    isinstance(it.get("operation_id"), str) and OP_ID.fullmatch(it["operation_id"])
                ):
                    v.add(ip + ".operation_id", "must be an operation id")
                if it.get("target") is not None:
                    _target(v, ip + ".target", it["target"])
                if it.get("status") not in OPERATION_STATUS_VALUES:
                    v.add(ip + ".status", "must be an operation status")
                elif "target" in it and it["target"] is None and it["status"] != "unknown":
                    # A null target means the item's own record cannot be read.
                    v.add(
                        ip + ".status",
                        "is unknown when the item record cannot be read (target is null)",
                    )
    if status in ("planned", "expired"):
        for k in ("dispatched_at", "finished_at", "receipt"):
            if x.get(k) is not None:
                v.add(f"{p}.{k}", f"is null while the operation is {status}")
    if status == "dispatching":
        if not isinstance(x.get("dispatched_at"), str):
            v.add(p + ".dispatched_at", "is set once dispatched")
        for k in ("finished_at", "receipt"):
            if x.get(k) is not None:
                v.add(f"{p}.{k}", "is null while dispatching")
    if status in (
        "unknown",
        "applied",
        "not_applied",
        "partial",
        "failed",
        "resolved",
    ) and not isinstance(x.get("dispatched_at"), str):
        v.add(p + ".dispatched_at", f"is set for status {status}")
    if status in ("applied", "not_applied", "partial", "failed") and not isinstance(
        x.get("finished_at"), str
    ):
        v.add(p + ".finished_at", f"is set for status {status}")
    if status in ("applied", "partial") and not batch and not isinstance(x.get("receipt"), dict):
        v.add(p + ".receipt", f"is set for status {status}")
    if status == "resolved" and not isinstance(res, dict):
        v.add(p + ".resolution", "is set when resolved")
    if status != "resolved" and res is not None:
        v.add(p + ".resolution", "is null unless resolved")
    if v.problems:
        return
    _operation_semantics(v, p, x)


def _operation_batch_status(statuses: list[str]) -> str | None:
    if any(s in ("unknown", "dispatching") for s in statuses):
        return "unknown"
    if all(s == "applied" for s in statuses):
        return "applied"
    if any(s in ("applied", "partial") for s in statuses):
        return "partial"
    if all(s == "not_applied" for s in statuses):
        return "not_applied"
    if all(s in ("not_applied", "failed") for s in statuses):
        return "failed"
    return None


def _operation_semantics(v: _V, p: str, x: dict[str, Any]) -> None:
    """check-contract.py's operation checks: ids, status against the receipt and
    the items, time order and the token lifetime."""
    oid, status = x["operation_id"], x["status"]
    preview, receipt, target = x["preview"], x["receipt"], x["target"]
    if preview:
        if preview["operation_id"] != oid:
            v.add(p + ".preview.operation_id", "the recorded preview belongs to this operation")
        if target and "target" in preview and preview["target"]["key"] != target["key"]:
            v.add(p + ".preview.target", "the record and its preview name the same target")
    if receipt:
        if receipt["operation_id"] != oid:
            v.add(p + ".receipt.operation_id", "the receipt belongs to this operation")
        if target and receipt["target"]["key"] != target["key"]:
            v.add(p + ".receipt.target", "the record and its receipt name the same target")
        number, _, codes = receipt_rule(receipt)
        lasting = _lasting(receipt["outcome"])
        known_failure = number not in (1, 2) and not lasting
        if status == "applied" and (number in (1, 2) or not lasting):
            v.add(
                p + ".status",
                "applied needs a receipt with a lasting effect and no unknown or partial dimension",
            )
        elif status == "partial" and not codes.only("E_PARTIAL"):
            v.add(p + ".status", "partial needs a receipt that receipt_rules make E_PARTIAL")
        elif status == "unknown" and number != 1:
            v.add(p + ".status", "unknown cannot hold a receipt with a known outcome (CLI-060)")
        elif status in ("not_applied", "failed") and not known_failure:
            v.add(
                p + ".status",
                f"{status} holds a receipt with no lasting effect and no unknown dimension",
            )
    items = x["items"]
    if items is not None:
        # A batch too large for the budget lists only some items, those that need
        # attention first, and items_omitted counts the rest (operation.shortening).
        # An item whose own record cannot be read (target null, status unknown) says
        # nothing about the batch: the status rules and the preview comparison use the
        # readable items.
        omitted = x.get("items_omitted", 0)
        statuses = [it["status"] for it in items if it["target"] is not None]
        expected = _operation_batch_status(statuses) if statuses else None
        if status == "planned" and any(s != "planned" for s in statuses):
            v.add(p + ".status", "a planned batch has only planned items")
        elif (
            omitted
            and status != "unknown"
            and any(s in ("unknown", "dispatching") for s in statuses)
        ):
            # The listed items of a shortened batch can still prove it unknown.
            v.add(p + ".status", "a batch with an unknown or dispatching item is unknown")
        elif (
            not omitted
            and status in ("applied", "partial", "unknown", "failed", "not_applied")
            and expected
            and status != expected
        ):
            v.add(p + ".status", f"a batch whose items are {', '.join(statuses)} is {expected}")
        ops = [it["operation_id"] for it in items]
        if len(set(ops)) != len(ops):
            v.add(p + ".items", "every batch item has its own operation id")
        if omitted:
            seen_done = False
            for s in (it["status"] for it in items):
                if s in OPERATION_DONE_STATUS_VALUES:
                    seen_done = True
                elif seen_done:
                    v.add(
                        p + ".items",
                        "a shortened batch lists the items that need attention before the done "
                        "ones (operation.shortening)",
                    )
                    break
        if preview and "items" in preview:
            # the record's preview may be shortened (omitted); the record lists every item,
            # or items_omitted counts those it leaves out
            planned = [(it["target"]["key"], it["operation_id"]) for it in preview["items"]]
            recorded = [((it["target"] or {}).get("key"), it["operation_id"]) for it in items]
            if preview["total"] != len(items) + omitted:
                listed = f"{len(items)} + {omitted} omitted" if omitted else str(len(items))
                v.add(
                    p + ".items",
                    f"a batch record lists every item: its preview counts {preview['total']}, the "
                    f"record has {listed}",
                )
            elif omitted:
                # A shortened list is not in input order: each listed item has the
                # target of the preview item with its operation id.
                keys = {op: key for key, op in planned}
                if any(op in keys and key is not None and keys[op] != key for key, op in recorded):
                    v.add(p + ".items", "a batch record lists the items of its preview")
            elif any(
                p_op != r_op or (r_key is not None and p_key != r_key)
                for (p_key, p_op), (r_key, r_op) in zip(planned, recorded, strict=False)
            ):
                # Position by position: the same operation ids, and the same targets
                # where the item's record could be read.
                v.add(p + ".items", "a batch record lists the items of its preview, in order")
        for i, it in enumerate(items):
            if (
                "receipt" in it
                and isinstance(it["receipt"], dict)
                and it["receipt"].get("operation_id") != it["operation_id"]
            ):
                v.add(
                    _j(p + ".items", i) + ".receipt",
                    "the item and its receipt name the same operation",
                )
    created = parse_time(x["created_at"])
    previous_key, previous = "created_at", created
    for key in ("dispatched_at", "finished_at"):
        if x[key]:
            value = parse_time(x[key])
            if value is not None and previous is not None and value < previous:
                v.add(f"{p}.{key}", f"is not before {previous_key}")
            previous_key, previous = key, value
    if x["expires_at"]:
        b = parse_time(x["expires_at"])
        if (
            created is not None
            and b is not None
            and not 0 < (b - created).total_seconds() <= TOKEN_MAX_TTL_SECONDS
        ):
            v.add(
                p + ".expires_at",
                f"the token expires within {TOKEN_MAX_TTL_SECONDS} s of the dry-run (CLI-055)",
            )
    res = x["resolution"]
    if res:
        r = parse_time(res["resolved_at"])
        if r is not None and created is not None and r < created:
            v.add(p + ".resolution.resolved_at", "an operation is resolved after it was created")


# ---------------------------------------------------------------- notices and paths


def _notice(v: _V, p: str, x: Any) -> None:
    if not isinstance(x, dict):
        v.add(p, "must be an object")
        return
    _need(v, p, x, ["type", "severity", "message"])
    if not (isinstance(x.get("type"), str) and SNAKE.fullmatch(x["type"])):
        v.add(p + ".type", "must be a snake_case name")
    if x.get("severity") not in ("info", "warning"):
        v.add(p + ".severity", "must be one of info, warning")
    if not _text(x.get("message")):
        v.add(p + ".message", "must be a non-empty string")
    if x.get("type") == "update_available":
        _exact(
            v,
            p,
            x,
            [
                "type",
                "severity",
                "message",
                "current_version",
                "latest_version",
                "command",
                "checked_at",
            ],
        )
        for k in ("current_version", "latest_version"):
            if k in x and not (isinstance(x[k], str) and SEMVER.fullmatch(x[k])):
                v.add(f"{p}.{k}", "must be a semantic version")
        if "command" in x and not _text(x["command"]):
            v.add(p + ".command", "must be a non-empty string")
        if "checked_at" in x and not _time(x["checked_at"]):
            v.add(p + ".checked_at", "must be an ISO 8601 UTC time")
        if not v.problems:
            from .selfupdate import compare_versions, parse_version

            cur, latest = parse_version(x["current_version"]), parse_version(x["latest_version"])
            if compare_versions(latest, cur) <= 0:
                v.add(p + ".latest_version", "names a version newer than the current one (CLI-071)")
            elif latest.major > cur.major and x["severity"] != "warning":
                v.add(
                    p + ".severity", "an update that crosses a major version is a warning (CLI-071)"
                )


def _meta(v: _V, meta: Any, ok: bool) -> None:
    if not _exact(v, "meta", meta, ["duration_ms"], ["notices", "untrusted"]):
        return
    if not (_is_int(meta.get("duration_ms")) and meta["duration_ms"] >= 0):
        v.add("meta.duration_ms", "must be a non-negative integer")
    if "notices" in meta:
        n = meta["notices"]
        if not isinstance(n, list) or not n:
            v.add("meta.notices", "must be a non-empty array")
        else:
            for i, item in enumerate(n):
                _notice(v, _j("meta.notices", i), item)
    if "untrusted" in meta:
        u = meta["untrusted"]
        if not isinstance(u, list) or not u:
            v.add("meta.untrusted", "must be a non-empty array")
            return
        if len(set(map(str, u))) != len(u):
            v.add("meta.untrusted", "must not repeat a path")
        for i, s in enumerate(u):
            up = _j("meta.untrusted", i)
            if not (isinstance(s, str) and UNTRUSTED_PATH.fullmatch(s)):
                v.add(up, "is not a path like data.items[].title or error.details.upstream_message")
            elif ok and not re.match(r"data(?:\[|\.|$)", s):
                v.add(up, "a success response marks paths in data")
            elif not ok and not re.match(r"error\.details(?:\.|$)", s):
                v.add(up, "a failure response marks paths in error.details")


def _check_untrusted(v: _V, env: dict[str, Any]) -> None:
    for i, path in enumerate(env.get("meta", {}).get("untrusted", []) or []):
        if not isinstance(path, str):
            continue
        nodes: list[Any] = [env]
        segments = path.split(".")
        for depth, segment in enumerate(segments):
            name = segment.rstrip("[]")
            arrays = (len(segment) - len(name)) // 2
            following = depth + 1 < len(segments)
            nxt: list[Any] = []
            for node in nodes:
                if not isinstance(node, dict) or name not in node:
                    continue
                values = [node[name]]
                for _ in range(arrays):
                    expanded: list[Any] = []
                    for value in values:
                        if isinstance(value, list):
                            expanded.extend(value)
                        else:
                            v.add(
                                _j("meta.untrusted", i),
                                f"{path}: {name} is not an array, so [] does not apply",
                            )
                    values = expanded
                for value in values:
                    if following and isinstance(value, list):
                        v.add(
                            _j("meta.untrusted", i),
                            f"{path}: {name} is an array; step into it with [] (CLI-046)",
                        )
                    else:
                        nxt.append(value)
            nodes = nxt


def _generic(v: _V, value: Any, path: str = "", map_object: bool = False) -> None:
    """CLI-007 for every key: snake_case names, ids as strings, UTC times (keys of
    code-keyed maps are exempt from snake_case)."""
    if isinstance(value, dict):
        for k in value:
            item = value[k]
            at = _j(path, k)
            if not map_object:
                if not SNAKE.fullmatch(k):
                    v.add(at, "key is not lowercase snake_case (CLI-007)")
                if (
                    (k in ("id", "iid") or k.endswith("_id"))
                    and item is not None
                    and not isinstance(item, str)
                ):
                    v.add(at, "ids are strings (CLI-007)")
                if (k == "ids" or k.endswith("_ids")) and not _str_list(item):
                    v.add(at, "ids are strings (CLI-007)")
                if k.endswith("_at") and item is not None and not _time(item):
                    v.add(at, "times are ISO 8601 UTC with Z (CLI-007)")
            _generic(v, item, at, k in MAP_KEYS and isinstance(item, dict) and not map_object)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _generic(v, item, _j(path, i))


def _data_kind(data: Any) -> str:
    if not isinstance(data, dict):
        return "other"
    if all(k in data for k in ("operation_id", "status", "created_at")):
        return "operation"
    if "confirm_token" in data or "preview" in data:
        return "dry_run"
    if "receipt" in data:
        return "write"
    if "items" in data and "summary" in data:
        return "batch"
    if "items" in data and any(
        k in data
        for k in (
            "count",
            "has_more",
            "next_cursor",
            "next_offset",
            "continuation_reason",
            "truncated",
        )
    ):
        return "page"
    return "other"


def _success_data(v: _V, data: Any) -> None:
    kind = _data_kind(data)
    if kind == "operation":
        _operation(v, "data", data)
    elif kind == "dry_run":
        if _exact(v, "data", data, ["preview", "confirm_token", "expires_at", "operation_id"]):
            _preview(v, "data.preview", data.get("preview"))
            _token_fields(v, "data", data)
    elif kind == "write":
        if _exact(v, "data", data, ["operation_id", "receipt"], ["result"]):
            if not (
                isinstance(data.get("operation_id"), str) and OP_ID.fullmatch(data["operation_id"])
            ):
                v.add("data.operation_id", "must be an operation id")
            if _receipt(v, "data.receipt", data["receipt"]) and not v.problems:
                number, ok, _ = receipt_rule(data["receipt"])
                if not ok:
                    v.add(
                        "data.receipt", f"receipt rule {number} makes this write ok false (CLI-059)"
                    )
                if data["operation_id"] != data["receipt"]["operation_id"]:
                    v.add("data.operation_id", "names the receipt's operation")
    elif kind == "batch":
        if _exact(v, "data", data, ["items", "summary"], ["operation_id", "omitted"]):
            if "operation_id" in data and not (
                isinstance(data["operation_id"], str) and BATCH_ID.fullmatch(data["operation_id"])
            ):
                v.add("data.operation_id", "must be a batch id")
            if _batch(v, "data", data) and not v.problems:
                expected = batch_expected_code(data)
                if expected is not None:
                    v.add(
                        "data",
                        "a batch with a failed, unknown or skipped item is ok false with "
                        f"{expected or 'its code'} (CLI-063)",
                    )
    elif kind == "page":
        _page(v, "data", data)


def compact_size(doc: Any) -> int:
    """Characters of a document's compact JSON (CLI-042 measures budgets so)."""
    return len(json.dumps(doc, ensure_ascii=False, separators=(",", ":")))


def is_full_reference(data: Any) -> bool:
    """Whether success data is the full reference (CLI-033), which the success
    budget exempts (contract.json size_budgets.exempt): it has every key the full
    reference requires."""
    return isinstance(data, dict) and all(k in data for k in REFERENCE_REQUIRED_KEYS)


def _reference_kind(data: Any) -> str:
    """Which reference output data is, by its exact key set: reference (full),
    index, command or ""."""
    if not isinstance(data, dict):
        return ""
    keys = set(data)
    if keys == set(REFERENCE_REQUIRED_KEYS):
        return "reference"
    if keys in ({"tool", "version", "commands"}, {"tool", "version", "groups"}):
        return "index"
    if keys == {"tool", "version", "command", "schemas", "error_codes"}:
        return "command"
    return ""


def _envelope(v: _V, env: Any, stream: bool) -> None:
    if not isinstance(env, dict):
        v.add("$", "is not a JSON object")
        return
    _generic(v, {k: val for k, val in env.items() if k != "type" or not stream})
    ok = env.get("ok")
    if not isinstance(ok, bool):
        v.add("ok", "must be a boolean")
        return
    want = (
        ["ok", "schema_version", "data", "meta"]
        if ok
        else ["ok", "schema_version", "error", "meta"]
    )
    _exact(v, "$", env, want, ["type"] if stream else [])
    if stream and env.get("type") not in ("item", "summary"):
        v.add("type", "must be item or summary")
    if env.get("schema_version") != SCHEMA_VERSION:
        v.add("schema_version", f'must be "{SCHEMA_VERSION}"')
    _meta(v, env.get("meta"), ok)
    size = compact_size(env)
    if ok:
        if "data" in env:
            _success_data(v, env["data"])
        kind = _reference_kind(env.get("data"))
        # The 8,000-character success budget holds for default invocations only
        # (CLI-042), which the envelope cannot tell: conformance R04 checks it on its
        # own (Q24).
        if stream:
            pass  # NDJSON stream lines are not budgeted
        elif kind == "index" and size > INDEX_BUDGET_CHARS:
            v.add(
                "$",
                f"the reference --index response is {size} characters; above {INDEX_BUDGET_CHARS} "
                "it returns groups (CLI-031)",
            )
        elif kind == "command" and size > COMMAND_BUDGET_CHARS:
            v.add(
                "$",
                f"the reference --command response is {size} characters; the budget is "
                f"{COMMAND_BUDGET_CHARS} (CLI-032)",
            )
    else:
        _error_object(v, "error", env.get("error"))
        if not stream and size > ERROR_BUDGET_CHARS:
            v.add(
                "$",
                f"error envelope is {size} characters (compact), over the {ERROR_BUDGET_CHARS} "
                "budget",
            )
    if not v.problems:
        _check_untrusted(v, env)
        if not v.problems:
            for i, n in enumerate(env.get("meta", {}).get("notices", []) or []):
                _notice(v, _j("meta.notices", i), n)


def validate_envelope(raw: bytes | str) -> None:
    """Check one stdout document in JSON mode against the contract: a single UTF-8
    JSON object with a trailing newline and no BOM; the exact envelope keys;
    schema_version; meta; the error object, its details per code and the error
    size budget; and, for success data, the dry-run, receipt, batch, page and
    operation shapes with their semantic rules. The 8,000-character success budget
    applies to default invocations only (CLI-042), so conformance R04 checks it
    there, not this function (Q24). Raises ContractViolation listing every
    problem."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    v = _V()
    if not raw.endswith(b"\n") or raw.endswith(b"\r\n"):
        v.add("$", "does not end with a single \\n")
    doc = _parse(raw, v)
    _envelope(v, doc, stream=False)
    v.raise_if_any()


def validate_stream(raw: bytes | str) -> None:
    """Check an NDJSON stream (CLI-006): every line a full envelope with type item
    or summary, and the last line the summary. Raises ContractViolation."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    v = _V()
    if not raw.endswith(b"\n"):
        v.add("$", "does not end with \\n")
    lines = [line for line in raw.split(b"\n") if line.strip()]
    if not lines:
        v.add("$", "holds no lines")
    for i, line in enumerate(lines):
        lv = _V()
        try:
            doc = _parse(line, lv)
            _envelope(lv, doc, stream=True)
        except ContractViolation:
            pass
        for prob in lv.problems:
            v.add(f"line {i + 1}", prob)
        if lv.problems:
            continue
        last = i == len(lines) - 1
        if last and doc.get("type") != "summary":
            v.add(f"line {i + 1}", "the last line has type summary")
        if not last and doc.get("type") != "item":
            v.add(f"line {i + 1}", "only the last line has type summary")
    v.raise_if_any()


# ---------------------------------------------------------------- built-in data shapes


def _run(check: Callable[[_V, Any], None], data: Any) -> None:
    v = _V()
    check(v, data)
    _generic(v, data, "data")
    v.raise_if_any()


def _compact_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _semver_key(s: str) -> Any:
    from .selfupdate import parse_version

    ver = parse_version(s)
    pre = (
        [(0, int(x), "") if x.isdigit() else (1, 0, x) for x in ver.pre.split(".")]
        if ver.pre
        else None
    )
    # a release sorts after its pre-releases
    return (ver.major, ver.minor, ver.patch, 1 if pre is None else 0, pre or [])


def _tree(v: _V, p: str, paths: list[str], aliases: dict[str, list[str]] | None) -> None:
    """The command tree (CLI-021): paths are unique, a path is a command or a group
    but not both, and no alias collides with a path or another alias. aliases maps
    a command path to its aliases."""
    repeated = sorted({x for x in paths if paths.count(x) > 1})
    if repeated:
        v.add(p, "command paths are unique (CLI-021); repeated: " + ", ".join(repeated))
    for path in sorted(set(paths)):
        if any(other.startswith(path + " ") for other in paths):
            v.add(
                p, f"'{path}' is both a command and a group; a path is one or the other (CLI-021)"
            )
    # Aliases are per parent (P1): `label create` and `milestone create` may both have
    # the alias add. An alias collides only with the name of a sibling (a command or a
    # group under the same parent) or another sibling's alias, and a root alias also
    # with a built-in name.
    children: dict[str, set[str]] = {}
    for path in paths:
        words = path.split()
        for i in range(len(words)):
            children.setdefault(" ".join(words[:i]), set()).add(words[i])
    taken: set[str] = set()
    for path in sorted(aliases or {}):
        words = path.split()
        if not words:
            continue
        parent = " ".join(words[:-1])
        for alias in (aliases or {})[path]:
            qualified = (parent + " " + alias).strip()
            if (
                alias in children.get(parent, set())
                or qualified in taken
                or (parent == "" and alias in RESERVED_ROOTS)
            ):
                v.add(
                    p,
                    f"alias '{qualified}' collides with a command path or another alias (CLI-021)",
                )
            taken.add(qualified)


def _value_matches(type_: str, value: Any) -> bool:
    if type_ in ("string", "path"):
        return isinstance(value, str)
    if type_ == "integer":
        return _is_int(value)
    if type_ == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_ == "boolean":
        return isinstance(value, bool)
    return True


def _param(v: _V, p: str, x: Any, global_flag: bool = False) -> None:
    """One parameter entry (reference.schema.json $defs/param_fields)."""
    if not _exact(v, p, x, list(PARAM_REQUIRED_KEYS), list(PARAM_OPTIONAL_KEYS)):
        return
    name = x.get("name")
    if not (isinstance(name, str) and FLAG_NAME.fullmatch(name)):
        v.add(p + ".name", "must be a kebab-case flag name")
    elif not global_flag and name in SHADOWED_FLAGS:
        v.add(p + ".name", f"--{name} shadows a global flag (CLI-021)")
    type_ = x.get("type")
    if type_ not in PARAM_TYPES:
        v.add(p + ".type", "must be one of " + ", ".join(PARAM_TYPES))
    kind = x.get("kind")
    if kind not in ("flag", "positional") or (global_flag and kind != "flag"):
        v.add(p + ".kind", "must be flag" + ("" if global_flag else " or positional"))
    if not isinstance(x.get("required"), bool):
        v.add(p + ".required", "must be a boolean")
    for k in ("multiple", "secret", "file_input"):
        if k in x and not isinstance(x[k], bool):
            v.add(f"{p}.{k}", "must be a boolean")
    for k in ("unit", "description"):
        if k in x and not _text(x[k]):
            v.add(f"{p}.{k}", "must be a non-empty string")
    if "enum" in x:
        e = x["enum"]
        if (
            not isinstance(e, list)
            or not e
            or len({json.dumps(i, sort_keys=True) for i in e}) != len(e)
        ):
            v.add(p + ".enum", "must be a non-empty array of distinct values")
    if x.get("required") is True and "default" in x:
        v.add(p + ".default", "is absent on a required parameter (CLI-024)")
    if x.get("secret") is True and "default" in x:
        v.add(p + ".default", "is absent on a secret: a secret is never echoed (CLI-024, SEC-021)")
    if type_ == "enum" and "enum" not in x:
        v.add(p, 'missing key "enum" (type enum lists its values)')
    if x.get("file_input") is True and type_ != "path":
        v.add(p + ".type", "is path for a file input (CLI-048)")
    if v.problems:
        return
    if type_ != "enum" and not all(_value_matches(type_, i) for i in x.get("enum", [])):
        v.add(p + ".enum", f"--{name}: enum values match type {type_} (CLI-024)")
    if "default" in x:
        d = x["default"]
        values = d if x.get("multiple") and isinstance(d, list) else [d]
        good = (
            all(i in x["enum"] for i in values)
            if type_ == "enum"
            else all(_value_matches(type_, i) for i in values)
        )
        if not good:
            v.add(
                p + ".default",
                f"--{name}: the default matches the parameter's type and values (CLI-024)",
            )


def _schema_entry(v: _V, p: str, x: Any) -> None:
    if not _exact(v, p, x, list(SCHEMA_ENTRY_KEYS)):
        return
    if x.get("shape") not in SCHEMA_SHAPES:
        v.add(p + ".shape", "must be one of " + ", ".join(SCHEMA_SHAPES))
    for k in ("fields", "default_fields"):
        val = x.get(k)
        if not (
            isinstance(val, list) and all(isinstance(f, str) and SNAKE.fullmatch(f) for f in val)
        ) or len(set(val)) != len(val):
            v.add(f"{p}.{k}", "must be distinct snake_case field names")
            return
    uf = x.get("untrusted_fields")
    if not (
        isinstance(uf, list) and all(isinstance(f, str) and FIELD_PATH.fullmatch(f) for f in uf)
    ) or len(set(uf)) != len(uf):
        v.add(p + ".untrusted_fields", "must be distinct field paths")
        return
    fields = set(x["fields"])
    extra = [f for f in x["default_fields"] if f not in fields]
    if extra:
        v.add(
            p + ".default_fields",
            "default fields are fields of the schema; unknown: " + ", ".join(extra),
        )
    for i, path in enumerate(uf):
        if path.split(".", 1)[0].rstrip("[]") not in fields:
            v.add(
                _j(p + ".untrusted_fields", i),
                f"untrusted field {path!r} is not a field of the schema (CLI-046)",
            )


def _error_table(v: _V, p: str, table: Any, require_core: bool) -> bool:
    if not isinstance(table, dict) or not table:
        v.add(p, "must be a non-empty object of error codes")
        return False
    good = True
    for code, entry in table.items():
        ep = _j(p, code)
        if not ERROR_CODE.fullmatch(code):
            v.add(ep, "is not an error code")
        if not _exact(v, ep, entry, ["exit", "retryable"], ["meaning"]):
            good = False
            continue
        # an integer: true == 1 and 1.0 == 1 would pass the membership test
        if not _is_int(entry.get("exit")) or entry["exit"] not in ERROR_EXITS:
            v.add(ep + ".exit", "is not a documented failure exit code")
            good = False
        if not isinstance(entry.get("retryable"), bool):
            v.add(ep + ".retryable", "must be a boolean")
            good = False
        if "meaning" in entry and not _text(entry["meaning"]):
            v.add(ep + ".meaning", "must be a non-empty string")
    if not good:
        return False
    for code, row in CORE_CODES.items():
        if code not in table:
            if require_core:
                v.add(p, f"the error table lists every core code; missing {code} (CLI-010)")
            continue
        got = table[code]
        if (got["exit"], got["retryable"]) != (row.exit, row.retryable):
            v.add(
                _j(p, code),
                f"{code} keeps exit {row.exit} and retryable {str(row.retryable).lower()} "
                "(CLI-010)",
            )
    for code, got in table.items():
        if code in CORE_CODES:
            continue
        if got["exit"] == 9 and not code.endswith("_REQUIRED"):
            v.add(_j(p, code), f"{code}: exit 9 is reserved for human-action codes (CLI-018)")
    return True


def _command_entry(v: _V, p: str, x: Any, tool: Any, schemas: Any, table: Any) -> None:
    """One command (reference.schema.json $defs/command) and check-contract.py's
    links and examples. schemas and table are None when the caller checks the
    links itself."""
    if not _exact(v, p, x, list(REFERENCE_COMMAND_KEYS)):
        return
    path = x.get("path")
    if not (isinstance(path, str) and COMMAND_PATH.fullmatch(path)):
        v.add(p + ".path", "must be a command path")
        return
    aliases = x.get("aliases")
    if not (
        isinstance(aliases, list)
        and all(isinstance(a, str) and COMMAND_PATH.fullmatch(a) for a in aliases)
    ) or len(set(aliases)) != len(aliases):
        v.add(p + ".aliases", "must be distinct command paths")
        aliases = []
    if not _text(x.get("summary")):
        v.add(p + ".summary", "must be a non-empty string")
    type_ = x.get("type")
    if type_ not in ("read", "write"):
        v.add(p + ".type", "must be read or write")
    _effects(v, p + ".effects", x.get("effects"), type_ == "write")
    if type_ == "read" and any(e != "observe" for e in x.get("effects") or []):
        v.add(p + ".effects", "a read only observes (CLI-025)")
    if not isinstance(x.get("dangerous"), bool):
        v.add(p + ".dangerous", "must be a boolean")
    _dangerous_rule(v, p, x)
    if x.get("maturity") not in MATURITY_VALUES:
        v.add(p + ".maturity", "must be one of " + ", ".join(MATURITY_VALUES))
    ev = x.get("evidence")
    if not (isinstance(ev, list) and all(isinstance(e, str) and EVIDENCE.fullmatch(e) for e in ev)):
        v.add(p + ".evidence", "must be evidence references <kind>:<path> (VER-005)")
        ev = []
    if x.get("maturity") in ("verified", "stable") and not ev:
        v.add(
            p + ".evidence", "verified and stable commands cite their evidence (CLI-027, VER-005)"
        )
    params = x.get("params")
    if not isinstance(params, list):
        v.add(p + ".params", "must be an array")
        params = []
    for i, prm in enumerate(params):
        _param(v, _j(p + ".params", i), prm)
    if sum(1 for prm in params if isinstance(prm, dict) and prm.get("kind") == "positional") > 1:
        v.add(p + ".params", "a command binds at most one positional argument (CLI-023)")
    if not (isinstance(x.get("output"), str) and SNAKE.fullmatch(x["output"])):
        v.add(p + ".output", "must be a snake_case schema name")
    codes = x.get("error_codes")
    if not (
        isinstance(codes, list)
        and codes
        and all(isinstance(c, str) and ERROR_CODE.fullmatch(c) for c in codes)
    ) or len(set(codes)) != len(codes):
        v.add(p + ".error_codes", "must be a non-empty array of distinct error codes")
        codes = []
    examples = x.get("examples")
    if not (isinstance(examples, list) and examples and all(_text(e) for e in examples)):
        v.add(p + ".examples", "must be a non-empty array of strings")
        examples = []
    if v.problems:
        return
    if schemas is not None and x["output"] not in schemas:
        v.add(p + ".output", f"{path}: output names an entry of schemas (CLI-020)")
    missing = [c for c in codes if table is not None and c not in table]
    if missing:
        v.add(
            p + ".error_codes",
            f"{path}: error codes are declared in error_codes: {', '.join(missing)} (CLI-026)",
        )
    names = [prm["name"] for prm in params]
    if len(set(names)) != len(names):
        v.add(p + ".params", f"{path}: parameter names are unique")
    if isinstance(tool, str):
        # An alias replaces the last word of the path (aliases are per parent, P1).
        parent = path[: path.rfind(" ") + 1]
        prefixes = [f"{tool} {q}" for q in [path, *(parent + al for al in aliases)]]
        for i, e in enumerate(examples):
            if not any(e == q or e.startswith(q + " ") for q in prefixes):
                v.add(
                    _j(p + ".examples", i),
                    f"{path}: an example is a runnable call that starts with '{tool} {path}'",
                )
    if path == SELF_UPDATE:
        if any("--confirm" in e for e in examples):
            v.add(
                p + ".examples",
                "update is outside the confirm gate and never takes --confirm (CLI-070)",
            )
    elif type_ == "write":
        if not any("--dry-run" in e for e in examples) or not any(
            "--confirm" in e for e in examples
        ):
            v.add(
                p + ".examples",
                f"{path}: a write command's examples show the --dry-run and --confirm pair "
                "(CLI-030)",
            )
    if x["dangerous"] and not all("--dangerous" in e for e in examples):
        v.add(
            p + ".examples", f"{path}: a dangerous command's examples carry --dangerous (CLI-056)"
        )


def _reference(v: _V, p: str, d: Any) -> None:
    if not _exact(v, p, d, list(REFERENCE_REQUIRED_KEYS)):
        return
    if not (isinstance(d.get("tool"), str) and TOOL_NAME.fullmatch(d["tool"])):
        v.add(p + ".tool", "must be a kebab-case tool name")
    if not (isinstance(d.get("version"), str) and SEMVER.fullmatch(d["version"])):
        v.add(p + ".version", "must be a semantic version")
    if not (
        isinstance(d.get("spec_version"), str)
        and SEMVER.fullmatch(d["spec_version"])
        and d["spec_version"].startswith("2.")
    ):
        v.add(p + ".spec_version", "must be a 2.x.y semantic version")
    if d.get("schema_version") != SCHEMA_VERSION:
        v.add(p + ".schema_version", f'must be "{SCHEMA_VERSION}"')
    if d.get("risk_tier") not in TIERS:
        v.add(p + ".risk_tier", "must be T0, T1 or T2")
    flags = d.get("global_flags")
    if not isinstance(flags, list) or not flags:
        v.add(p + ".global_flags", "must be a non-empty array")
        flags = []
    for i, f in enumerate(flags):
        _param(v, _j(p + ".global_flags", i), f, global_flag=True)
    schemas = d.get("schemas")
    if not isinstance(schemas, dict) or not schemas:
        v.add(p + ".schemas", "must be a non-empty object")
        schemas = {}
    for name, s in schemas.items():
        if not SNAKE.fullmatch(name):
            v.add(_j(p + ".schemas", name), "is not a snake_case schema name")
        _schema_entry(v, _j(p + ".schemas", name), s)
    table = d.get("error_codes")
    table_ok = _error_table(v, p + ".error_codes", table, True)
    exits = d.get("exit_codes")
    if (
        not isinstance(exits, dict)
        or not exits
        or not all(re.fullmatch(r"0|[1-9][0-9]{0,2}", k) and _text(val) for k, val in exits.items())
    ):
        v.add(p + ".exit_codes", "must map exit codes to their meaning")
        exits = None
    commands = d.get("commands")
    if not isinstance(commands, list) or not commands:
        v.add(p + ".commands", "must list the commands")
        return
    for i, c in enumerate(commands):
        _command_entry(
            v, _j(p + ".commands", i), c, d.get("tool"), schemas, table if table_ok else None
        )
    if v.problems:
        return
    by_path = {c["path"]: c for c in commands}
    aliases: dict[str, list[str]] = {}
    for c in commands:
        aliases.setdefault(c["path"], []).extend(c["aliases"])
    _tree(v, p + ".commands", [c["path"] for c in commands], aliases)
    for i, c in enumerate(commands):
        root = c["path"].split(" ", 1)[0]
        if root in RESERVED_ROOTS and c["path"] not in [*LIBRARY_BUILTINS, SELF_UPDATE]:
            v.add(_j(p + ".commands", i), f"'{root}' is a reserved built-in name (CLI-021)")
    for name in LIBRARY_BUILTINS:
        c = by_path.get(name)
        if c is None:
            v.add(
                p + ".commands",
                f"the built-in '{name}' is part of the command tree (CLI-021, CLI-031, CLI-061)",
            )
        elif c["type"] != "read" or c["effects"] != ["observe"] or c["dangerous"]:
            v.add(p + ".commands", f"the built-in '{name}' is a read that only observes")
    if exits is not None and table_ok:
        used = {"0"} | {str(entry["exit"]) for entry in table.values()}
        missing = sorted(used - set(exits), key=int)
        if missing:
            v.add(
                p + ".exit_codes",
                "exit_codes explains every exit the error table uses; missing "
                + ", ".join(missing),
            )
    need = max(
        (
            TIERS[MINIMUM_RISK_TIER[e]]
            for c in commands
            if c["path"] != SELF_UPDATE
            for e in c["effects"]
        ),
        default=0,
    )
    if d["risk_tier"] in TIERS and need > TIERS[d["risk_tier"]]:
        v.add(
            p + ".risk_tier",
            f"the commands' effects need at least T{need}; the tool declares {d['risk_tier']} "
            "(SEC-001)",
        )
    names = [f.get("name") for f in flags if isinstance(f, dict)]
    missing_flags = [f for f in GLOBAL_FLAGS if f not in names]
    if missing_flags or len(set(names)) != len(names):
        v.add(
            p + ".global_flags",
            "lists each control flag once; missing " + (", ".join(missing_flags) or "none"),
        )


def validate_reference_full(data: Any) -> None:
    """The full reference (CLI-033): its schema and check-contract.py's checks of
    the tree, built-ins, error table, risk tier, links and examples."""
    _run(lambda v, d: _reference(v, "data", d), data)


def _reference_index(v: _V, p: str, d: Any) -> None:
    if not isinstance(d, dict):
        v.add(p, "must be an object")
        return
    if "groups" in d:
        _exact(v, p, d, ["tool", "version", "groups"])
    else:
        _exact(v, p, d, ["tool", "version", "commands"])
    if not (isinstance(d.get("tool"), str) and TOOL_NAME.fullmatch(d["tool"])):
        v.add(p + ".tool", "must be a kebab-case tool name")
    if not (isinstance(d.get("version"), str) and SEMVER.fullmatch(d["version"])):
        v.add(p + ".version", "must be a semantic version")
    if "groups" in d:
        groups = d["groups"]
        if not isinstance(groups, list) or not groups:
            v.add(p + ".groups", "must be a non-empty array")
            return
        for i, g in enumerate(groups):
            gp = _j(p + ".groups", i)
            if _exact(v, gp, g, list(INDEX_GROUP_KEYS)):
                if not (isinstance(g.get("path"), str) and COMMAND_PATH.fullmatch(g["path"])):
                    v.add(gp + ".path", "must be a command path")
                if not _text(g.get("summary")):
                    v.add(gp + ".summary", "must be a non-empty string")
                if not (_is_int(g.get("command_count")) and g["command_count"] >= 1):
                    v.add(gp + ".command_count", "must be a positive integer")
        if not v.problems:
            paths = [g["path"] for g in groups]
            if len(set(paths)) != len(paths):
                v.add(p + ".groups", "group paths are unique (CLI-031)")
    else:
        commands = d.get("commands")
        if not isinstance(commands, list) or not commands:
            v.add(p + ".commands", "must be a non-empty array")
            return
        for i, c in enumerate(commands):
            cp = _j(p + ".commands", i)
            if not _exact(v, cp, c, list(INDEX_COMMAND_KEYS)):
                continue
            if not (isinstance(c.get("path"), str) and COMMAND_PATH.fullmatch(c["path"])):
                v.add(cp + ".path", "must be a command path")
            if c.get("type") not in ("read", "write"):
                v.add(cp + ".type", "must be read or write")
            _effects(v, cp + ".effects", c.get("effects"), c.get("type") == "write")
            if c.get("type") == "read" and any(e != "observe" for e in c.get("effects") or []):
                v.add(cp + ".effects", "a read only observes (CLI-025)")
            if not isinstance(c.get("dangerous"), bool):
                v.add(cp + ".dangerous", "must be a boolean")
            _dangerous_rule(v, cp, c)
            if c.get("maturity") not in MATURITY_VALUES:
                v.add(cp + ".maturity", "must be one of " + ", ".join(MATURITY_VALUES))
            if not _text(c.get("summary")):
                v.add(cp + ".summary", "must be a non-empty string")
        if not v.problems:
            _tree(v, p + ".commands", [c["path"] for c in commands], None)
    size = _compact_size(d)
    if size > INDEX_BUDGET_CHARS:
        v.add(
            p,
            f"the index is {size} characters; above {INDEX_BUDGET_CHARS} return groups instead "
            "(CLI-031)",
        )


def validate_reference_index(data: Any) -> None:
    """reference --index data (CLI-031): one form (commands or groups), unique
    paths and the index budget."""
    _run(lambda v, d: _reference_index(v, "data", d), data)


def _reference_command(v: _V, p: str, d: Any) -> None:
    if not _exact(v, p, d, ["tool", "version", "command", "schemas", "error_codes"]):
        return
    if not (isinstance(d.get("tool"), str) and TOOL_NAME.fullmatch(d["tool"])):
        v.add(p + ".tool", "must be a kebab-case tool name")
    if not (isinstance(d.get("version"), str) and SEMVER.fullmatch(d["version"])):
        v.add(p + ".version", "must be a semantic version")
    _command_entry(v, p + ".command", d.get("command"), d.get("tool"), None, None)
    schemas = d.get("schemas")
    if not isinstance(schemas, dict):
        v.add(p + ".schemas", "must be an object")
        schemas = {}
    for name, s in schemas.items():
        _schema_entry(v, _j(p + ".schemas", name), s)
    _error_table(v, p + ".error_codes", d.get("error_codes"), False)
    if v.problems:
        return
    cmd = d["command"]
    if set(schemas) != {cmd["output"]}:
        v.add(p + ".schemas", "holds exactly the entry the command's output names (CLI-032)")
    if set(d["error_codes"]) != set(cmd["error_codes"]):
        v.add(p + ".error_codes", "holds exactly the codes the command lists (CLI-026, CLI-032)")
    size = _compact_size(d)
    if size > COMMAND_BUDGET_CHARS:
        v.add(
            p,
            f"the command entry is {size} characters; the budget is {COMMAND_BUDGET_CHARS} "
            "(CLI-032)",
        )


def validate_reference_command(data: Any) -> None:
    """reference --command data (CLI-032): the entry, its links and its budget."""
    _run(lambda v, d: _reference_command(v, "data", d), data)


def _is_absolute(path: str) -> bool:
    return (
        path.startswith("/") or bool(re.match(r"[A-Za-z]:[\\/]", path)) or path.startswith("\\\\")
    )


def _context(v: _V, p: str, d: Any) -> None:
    from .contract_gen import CONTEXT_REQUIRED_KEYS

    if not isinstance(d, dict):
        v.add(p, "must be an object")
        return
    _need(v, p, d, CONTEXT_REQUIRED_KEYS)
    if "tool" in d and not (isinstance(d["tool"], str) and TOOL_NAME.fullmatch(d["tool"])):
        v.add(p + ".tool", "must be a kebab-case tool name")
    if "version" in d and not (isinstance(d["version"], str) and SEMVER.fullmatch(d["version"])):
        v.add(p + ".version", "must be a semantic version")
    if "spec_version" in d and not (
        isinstance(d["spec_version"], str)
        and SEMVER.fullmatch(d["spec_version"])
        and d["spec_version"].startswith("2.")
    ):
        v.add(p + ".spec_version", "must be a 2.x.y semantic version")
    for k in ("account", "host"):
        if k in d and d[k] is not None and not _text(d[k]):
            v.add(f"{p}.{k}", "must be a non-empty string or null")
    creds = d.get("credentials")
    if "credentials" in d:
        if not isinstance(creds, dict):
            v.add(p + ".credentials", "must be an object")
        else:
            _need(v, p + ".credentials", creds, ["configured", "source"])
            if "configured" in creds and not isinstance(creds["configured"], bool):
                v.add(p + ".credentials.configured", "must be a boolean")
            if creds.get("source") is not None and not _text(creds.get("source")):
                v.add(p + ".credentials.source", "must be a non-empty string or null")
    state = d.get("state")
    if "state" in d and _exact(
        v, p + ".state", state, ["dir", "available", "protection"], ["reason"]
    ):
        sd, avail, prot = state.get("dir"), state.get("available"), state.get("protection")
        if sd is not None and not _text(sd):
            v.add(p + ".state.dir", "is null when unknown, never empty")
        if not isinstance(avail, bool):
            v.add(p + ".state.available", "must be a boolean")
        if prot is not None and prot not in STATE_PROTECTION_VALUES:
            v.add(p + ".state.protection", "must be one of " + ", ".join(STATE_PROTECTION_VALUES))
        if avail is True and (not isinstance(sd, str) or not isinstance(prot, str)):
            v.add(p + ".state", "an available state store names its dir and protection")
        if "reason" in state and not _text(state["reason"]):
            v.add(p + ".state.reason", "must be a non-empty string")
        if isinstance(sd, str) and sd and not _is_absolute(sd):
            v.add(p + ".state.dir", "the state directory is reported as an absolute path (SEC-030)")
    auth = d.get("authorization")
    if "authorization" in d and _exact(
        v,
        p + ".authorization",
        auth,
        ["status", "scope_id", "path", "effects", "dangerous", "expires_at"],
    ):
        ap = p + ".authorization"
        status = auth.get("status")
        if status not in CONTEXT_AUTHORIZATION_STATUS:
            v.add(ap + ".status", "must be one of " + ", ".join(CONTEXT_AUTHORIZATION_STATUS))
        sid = auth.get("scope_id")
        if sid is not None and not _text(sid):
            v.add(ap + ".scope_id", "must be a non-empty string or null")
        path = auth.get("path")
        if path is not None and not _text(path):
            v.add(ap + ".path", "must be a non-empty string or null")
        elif isinstance(path, str) and not _is_absolute(path):
            v.add(ap + ".path", "the scope path is absolute (SEC-011)")
        effects = auth.get("effects")
        if not (
            isinstance(effects, list)
            and all(e in EFFECT_VALUES for e in effects)
            and len(set(effects)) == len(effects)
        ):
            v.add(ap + ".effects", "must be distinct effects")
            effects = []
        if not isinstance(auth.get("dangerous"), bool):
            v.add(ap + ".dangerous", "must be a boolean")
        exp = auth.get("expires_at")
        if exp is not None and not _time(exp):
            v.add(ap + ".expires_at", "must be an ISO 8601 UTC time or null")
        if status == "granted":
            if not isinstance(sid, str):
                v.add(ap + ".scope_id", "a granted scope is named")
            if not effects:
                v.add(ap + ".effects", "a granted scope grants at least one effect")
        elif status in ("missing", "invalid"):
            if sid is not None or effects or auth.get("dangerous") is not False or exp is not None:
                v.add(
                    ap,
                    "without a readable scope nothing is granted: scope_id and expires_at null, no "
                    "effects, not dangerous",
                )
        elif status in ("expired", "revoked", "budget_spent") and not isinstance(sid, str):
            v.add(ap + ".scope_id", f"a {status} scope was read, so it is named")
    if "profile" in d:
        _profile(v, p + ".profile", d["profile"])


PROFILE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


def _profile(v: _V, p: str, x: Any) -> None:
    """context.profile (context.schema.json $defs/profile, CLI-101)."""
    if not _exact(v, p, x, list(CONTEXT_PROFILE_KEYS)):
        return
    name, source, profiles, file = x["name"], x["source"], x["profiles"], x["file"]
    if name is not None and not (isinstance(name, str) and PROFILE_NAME.fullmatch(name)):
        v.add(p + ".name", "must be a profile name or null")
    if source not in PROFILE_SOURCE_VALUES:
        v.add(p + ".source", "must be one of " + ", ".join(PROFILE_SOURCE_VALUES))
    elif (source == "none") != (name is None):
        v.add(
            p + ".name",
            "is null exactly when source is none: a profile is named when one was chosen (CLI-101)",
        )
    if not (
        isinstance(profiles, list)
        and all(isinstance(s, str) and PROFILE_NAME.fullmatch(s) for s in profiles)
        and len(set(profiles)) == len(profiles)
    ):
        v.add(p + ".profiles", "must be distinct profile names")
    if file is not None and not _text(file):
        v.add(p + ".file", "must be a non-empty string or null")


def validate_context(data: Any) -> None:
    """context data (CLI-035)."""
    _run(lambda v, d: _context(v, "data", d), data)


def _doctor(v: _V, p: str, d: Any) -> None:
    if not _exact(v, p, d, ["checks"]):
        return
    checks = d.get("checks")
    if not isinstance(checks, list):
        v.add(p + ".checks", "must be an array")
        return
    seen = []
    for i, c in enumerate(checks):
        cp = _j(p + ".checks", i)
        if not _exact(v, cp, c, ["check", "status", "message", "fix"]):
            continue
        if not (isinstance(c.get("check"), str) and SNAKE.fullmatch(c["check"])):
            v.add(cp + ".check", "must be a snake_case name")
        if c.get("status") not in DOCTOR_STATUS_VALUES:
            v.add(cp + ".status", "must be one of " + ", ".join(DOCTOR_STATUS_VALUES))
        if not _text(c.get("message")):
            v.add(cp + ".message", "must be a non-empty string")
        if c.get("status") == "pass" and c.get("fix") is not None:
            v.add(cp + ".fix", "is null when the check passes")
        if c.get("status") == "fail" and not _text(c.get("fix")):
            v.add(cp + ".fix", "says how to fix a failed check")
        if c.get("fix") is not None and not _text(c.get("fix")):
            v.add(cp + ".fix", "must be a non-empty string or null")
        seen.append(c.get("check"))
    missing = sorted(set(DOCTOR_REQUIRED_CHECKS) - set(seen))
    if missing:
        v.add(p + ".checks", "missing required checks " + ", ".join(missing))
    repeated = sorted({str(n) for n in seen if seen.count(n) > 1})
    if repeated:
        v.add(p + ".checks", f"check names are unique; repeated: {', '.join(repeated)} (CLI-036)")


def validate_doctor(data: Any) -> None:
    """doctor data and its required checks (CLI-036)."""
    _run(lambda v, d: _doctor(v, "data", d), data)


def _changelog(v: _V, p: str, d: Any) -> None:
    """changelog data (CLI-037)."""
    if not _exact(v, p, d, list(CHANGELOG_REQUIRED_KEYS), list(CHANGELOG_OPTIONAL_KEYS)):
        return
    for k in ("current_version", "since"):
        if k in d and not (isinstance(d[k], str) and SEMVER.fullmatch(d[k])):
            v.add(f"{p}.{k}", "must be a semantic version")
    if "count" in d and not (_is_int(d["count"]) and d["count"] >= 0):
        v.add(p + ".count", "must be a non-negative integer")
    # A changelog cut to the size budget says so (CLI-041, CLI-042).
    if "truncated" in d and d["truncated"] is not True:
        v.add(p + ".truncated", "is true when present")
    if d.get("truncated") is True:
        if not _text(d.get("truncated_reason")):
            v.add(p + ".truncated_reason", "a truncated changelog says why (CLI-041)")
        omitted = d.get("omitted")
        if not (_is_int(omitted) and omitted >= 1):
            v.add(p + ".omitted", "counts the entries left out, a positive integer")
    elif "truncated_reason" in d or "omitted" in d:
        v.add(p, "truncated_reason and omitted come with truncated: true")
    entries = d.get("entries")
    if not isinstance(entries, list):
        v.add(p + ".entries", "must be an array")
        return
    for i, e in enumerate(entries):
        ep = _j(p + ".entries", i)
        if not _exact(v, ep, e, list(CHANGELOG_ENTRY_KEYS)):
            continue
        if not (isinstance(e.get("version"), str) and SEMVER.fullmatch(e["version"])):
            v.add(ep + ".version", "must be a semantic version")
        if e.get("date") is not None and not (
            isinstance(e["date"], str) and DATE.fullmatch(e["date"])
        ):
            v.add(ep + ".date", "must be YYYY-MM-DD or null")
        ch = e.get("changes")
        if not isinstance(ch, dict):
            v.add(ep + ".changes", "must be an object")
            continue
        for cat, lines in ch.items():
            if cat not in CHANGELOG_CATEGORIES:
                v.add(_j(ep + ".changes", cat), "is not a Keep a Changelog category")
            elif not (isinstance(lines, list) and all(_text(line) for line in lines)):
                v.add(_j(ep + ".changes", cat), "must be an array of non-empty strings")
    if v.problems:
        return
    keys = [_semver_key(e["version"]) for e in entries]
    if keys != sorted(keys, reverse=True) or len({e["version"] for e in entries}) != len(keys):
        v.add(p + ".entries", "entries are unique and newest first (CLI-037)")
    current = _semver_key(d["current_version"])
    if any(k > current for k in keys):
        v.add(p + ".entries", "no entry is newer than current_version")
    if "since" in d and any(k <= _semver_key(d["since"]) for k in keys):
        v.add(p + ".since", "--since returns only versions strictly newer than since (CLI-037)")
    if "count" in d and d["count"] != len(entries):
        v.add(p + ".count", "equals the number of entries")


def validate_changelog(data: Any) -> None:
    """changelog data (CLI-037): entries newest first, none newer than
    current_version, strictly newer than since, count equal to the entries."""
    _run(lambda v, d: _changelog(v, "data", d), data)


def _update(v: _V, p: str, d: Any) -> None:
    """update data (CLI-070): a successful update call."""
    if not _exact(v, p, d, list(UPDATE_REQUIRED_KEYS), list(UPDATE_OPTIONAL_KEYS)):
        return
    status = d.get("status")
    if status not in UPDATE_STATUS_VALUES or status == "partial":
        v.add(
            p + ".status",
            "must be up_to_date, updated or available (a partial update is E_PARTIAL)",
        )
    for k in ("current_version", "target_version", "previous_version"):
        if k in d and not (isinstance(d[k], str) and SEMVER.fullmatch(d[k])):
            v.add(f"{p}.{k}", "must be a semantic version")
    for k in ("update_available", "binary_replaced", "signature_verified", "checksum_verified"):
        if k in d and not isinstance(d[k], bool):
            v.add(f"{p}.{k}", "must be a boolean")
    if d.get("install_method") not in UPDATE_INSTALL_METHODS:
        v.add(p + ".install_method", "must be one of " + ", ".join(UPDATE_INSTALL_METHODS))
    if d.get("signature_status") not in UPDATE_SIGNATURE_STATUS_VALUES:
        v.add(
            p + ".signature_status", "must be one of " + ", ".join(UPDATE_SIGNATURE_STATUS_VALUES)
        )
    if d.get("skill_sync_status") not in UPDATE_SKILL_SYNC_STATUS_VALUES:
        v.add(
            p + ".skill_sync_status", "must be one of " + ", ".join(UPDATE_SKILL_SYNC_STATUS_VALUES)
        )
    if "stage" in d and d["stage"] not in UPDATE_STAGES:
        v.add(p + ".stage", "must be an update stage")
    if "skill_sync_command" in d and not _text(d["skill_sync_command"]):
        v.add(p + ".skill_sync_command", "must be a non-empty string")
    _skill_sync_command(v, p, d, False)
    if "command" in d:
        c = d["command"]
        if not (_text(c) or (isinstance(c, list) and c and all(_text(x) for x in c))):
            v.add(p + ".command", "must be the command text or its argv")
    if status == "updated":
        _need(v, p, d, ["previous_version"])
        if d.get("update_available") is not False:
            v.add(p + ".update_available", "is false once updated")
        if d.get("binary_replaced") is not True:
            v.add(p + ".binary_replaced", "is true once updated")
        # skipped: no Skill source (go, pip, brew) or no Skill installed anywhere
        if d.get("skill_sync_status") not in ("synced", "not_needed", "skipped"):
            v.add(p + ".skill_sync_status", "is synced, not_needed or skipped once updated")
    elif status == "up_to_date":
        if d.get("update_available") is not False:
            v.add(p + ".update_available", "is false when up to date")
        if d.get("binary_replaced") not in (None, False):
            v.add(p + ".binary_replaced", "is false when up to date")
    elif status == "available":
        if d.get("update_available") is not True:
            v.add(p + ".update_available", "is true when an update is available")
        if d.get("binary_replaced") not in (None, False):
            v.add(p + ".binary_replaced", "is false: update --check changes nothing")
    verified = d.get("signature_status") == "verified"
    if "signature_verified" in d and d["signature_verified"] is not verified:
        v.add(
            p + ".signature_verified", "is true exactly when signature_status is verified (SEC-050)"
        )
    if d.get("install_method") == "binary" and status == "updated":
        if d.get("signature_status") != "verified":
            v.add(p + ".signature_status", "is verified for an update on the binary path (SEC-050)")
        if "checksum_verified" in d and d["checksum_verified"] is not True:
            v.add(p + ".checksum_verified", "is true for an update on the binary path (SEC-050)")
    if v.problems:
        return
    from .selfupdate import compare_versions, parse_version

    cur, target = parse_version(d["current_version"]), parse_version(d["target_version"])
    if status in ("updated", "up_to_date") and d["current_version"] != d["target_version"]:
        v.add(
            p + ".current_version",
            f"{status} reports the final state: current_version equals target_version (CLI-070)",
        )
    # a downgrade with --allow-downgrade is available too
    if status == "available" and compare_versions(target, cur) == 0:
        v.add(
            p + ".target_version", "an available target differs from the current version (CLI-070)"
        )
    if status == "updated" and d.get("previous_version") == d["current_version"]:
        v.add(p + ".previous_version", "an update changes the installed version")


def validate_operation_view(data: Any) -> None:
    """An operation show view (BLUEPRINT 4.7)."""
    _run(lambda v, d: _operation(v, "data", d), data)
