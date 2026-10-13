# Vendored from ai-native-cli-spec template/python/aicli.
# Do not edit in tool repos; run scripts/sync-spec.js.
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sean Guo

"""Receipts (CLI-059): outcome dimensions, the ordered receipt rules from
contract.json and the read-back comparison."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ._util import json_equal, pick, to_generic
from .contract_gen import (
    CHANGE_VALUES,
    DELIVERY_VALUES,
    EXECUTION_VALUES,
    FAILED_STAGE_VALUES,
    PERSISTENCE_VALUES,
    VERIFICATION_VALUES,
)

if TYPE_CHECKING:
    from .registry import Meta


@dataclass
class Outcome:
    """The observed result of a write along separate dimensions (CLI-059). Empty
    fields get defaults from the command's effects when apply succeeds:
    execution completed, change applied (change/configure), delivery delivered
    (deliver), persistence saved (artifact/persist), and verification computed
    from Result.observed against the preview's changes (not_run when nothing
    could be compared). Unknown values are treated as unknown (fail closed)."""

    execution: str = ""
    change: str = ""
    delivery: str = ""
    persistence: str = ""
    verification: str = ""
    failed_stage: str = ""


@dataclass
class Result:
    """What apply observed. observed is the read-back of the fields the write
    meant to change (from the write response or a re-read); mismatched names
    fields whose read-back disagrees; data is returned as data.result; untrusted
    lists paths relative to data that carry external content. Observed values are
    always declared untrusted."""

    outcome: Outcome = field(default_factory=Outcome)
    observed: dict[str, Any] | None = None
    mismatched: list[str] = field(default_factory=list)
    data: Any = None
    untrusted: list[str] = field(default_factory=list)


def normalize_outcome(o: Outcome, m: Meta) -> Outcome:
    """Defaults for a successful apply; values outside the contract enums become
    the fail-closed value."""
    o = dataclasses.replace(o)
    if not o.execution:
        o.execution = "completed"
    if not o.change:
        o.change = pick(m.has_effect("change", "configure"), "applied", "not_applicable")
    if not o.delivery:
        o.delivery = pick(m.has_effect("deliver"), "delivered", "not_applicable")
    if not o.persistence:
        o.persistence = pick(m.has_effect("artifact", "persist"), "saved", "not_applicable")
    return sanitize_outcome(o)


def sanitize_outcome(o: Outcome) -> Outcome:
    """Values outside the contract enums: unknown for the outcome dimensions,
    incomplete for verification."""
    o = dataclasses.replace(o)
    if o.execution not in EXECUTION_VALUES:
        o.execution = "unknown"
    if o.change not in CHANGE_VALUES:
        o.change = "unknown"
    if o.delivery not in DELIVERY_VALUES:
        o.delivery = "unknown"
    if o.persistence not in PERSISTENCE_VALUES:
        o.persistence = "unknown"
    if not o.verification:
        o.verification = "not_run"
    if o.verification not in VERIFICATION_VALUES:
        o.verification = "incomplete"
    if o.execution in ("failed", "interrupted"):
        if o.failed_stage not in FAILED_STAGE_VALUES:
            o.failed_stage = "apply"
    else:
        o.failed_stage = ""
    return o


def derive_receipt(o: Outcome, cause: str) -> tuple[bool, str]:
    """contract.json receipt_rules in order (first match wins): whether the write
    is ok and, if not, its error code. cause is the code of the error the apply,
    rollback or delivery step reported, used by rules 4 to 6."""
    failed_exec = o.execution in ("failed", "interrupted", "not_started")
    lasting = o.change == "applied" or o.delivery == "delivered" or o.persistence == "saved"
    # 1. anything unknown, or still running
    if "unknown" in (o.execution, o.change, o.delivery, o.persistence) or o.execution == "running":
        return False, "E_OUTCOME_UNKNOWN"
    # 2. anything partial
    if "partial" in (o.change, o.delivery, o.persistence):
        return False, "E_PARTIAL"
    # 3. execution failed but something lasting happened
    if failed_exec and lasting:
        return False, "E_PARTIAL"
    # 4. execution failed without lasting effect
    if failed_exec:
        return False, cause or "E_SERVER"
    # 5. the change was rolled back
    if o.change == "rolled_back":
        return False, cause or "E_VERIFY_FAILED"
    # 6. delivery rejected
    if o.delivery == "rejected":
        return False, cause or "E_FORBIDDEN"
    # 7. verification failed
    if o.verification == "failed":
        return False, "E_VERIFY_FAILED"
    # 8. verification incomplete
    if o.verification == "incomplete":
        return False, "E_VERIFY_INCOMPLETE"
    # 9. otherwise
    return True, ""


def record_status(ok: bool, code: str, o: Outcome) -> str:
    """A receipt's derived result as the operation record status: a change that
    took effect is applied even when its read-back failed or was incomplete (the
    receipt says so); a rolled-back change failed; anything else that left no
    lasting effect was not applied."""
    if ok:
        return "applied"
    if code == "E_OUTCOME_UNKNOWN":
        return "unknown"
    if code == "E_PARTIAL":
        return "partial"
    if o.change == "applied" or o.delivery == "delivered" or o.persistence == "saved":
        return "applied"
    if o.change == "rolled_back":
        return "failed"
    return "not_applied"


def _as_list(v: Any) -> list[Any]:
    g = to_generic(v)
    return g if isinstance(g, list) else [g]


def _list_has(container: Any, v: Any) -> bool:
    return any(json_equal(x, v) for x in _as_list(container))


def auto_verify(changes: list[Any], observed: dict[str, Any]) -> tuple[str, list[str]]:
    """Compare the read-back with the planned changes: after must equal, add must
    be present, remove must be absent. Fields missing from observed are skipped."""
    compared = 0
    mismatched: list[str] = []
    for raw in changes:
        if not isinstance(raw, dict):
            continue
        fld = raw.get("field")
        if not isinstance(fld, str) or not fld or fld not in observed:
            continue
        got = observed[fld]
        bad = False
        if "after" in raw:
            compared += 1
            if not json_equal(raw["after"], got):
                bad = True
        if "add" in raw:
            compared += 1
            for v in _as_list(raw["add"]):
                if not _list_has(got, v):
                    bad = True
        if "remove" in raw:
            compared += 1
            for v in _as_list(raw["remove"]):
                if _list_has(got, v):
                    bad = True
        if bad:
            mismatched.append(fld)
    if compared == 0:
        return "not_run", []
    if mismatched:
        return "failed", mismatched
    return "passed", []
