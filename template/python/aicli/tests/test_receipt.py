"""Receipt rules and verification (port of receipt_test.go)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from aicli import CHANGE, DELIVER, Meta, Outcome
from aicli.contract_gen import RECEIPT_RULE_COUNT
from aicli.receipt import auto_verify, derive_receipt, normalize_outcome

OK = Outcome(
    execution="completed",
    change="applied",
    delivery="not_applicable",
    persistence="not_applicable",
    verification="passed",
)


def w(**kw: str) -> Outcome:
    return dataclasses.replace(OK, **kw)


RULES = [
    # rule 1: anything unknown, or running
    ("r1 execution unknown", w(execution="unknown"), "", False, "E_OUTCOME_UNKNOWN"),
    ("r1 change unknown", w(change="unknown"), "", False, "E_OUTCOME_UNKNOWN"),
    ("r1 delivery unknown", w(delivery="unknown"), "", False, "E_OUTCOME_UNKNOWN"),
    ("r1 persistence unknown", w(persistence="unknown"), "", False, "E_OUTCOME_UNKNOWN"),
    ("r1 running", w(execution="running"), "", False, "E_OUTCOME_UNKNOWN"),
    ("r1 beats r2", w(execution="unknown", change="partial"), "", False, "E_OUTCOME_UNKNOWN"),
    # rule 2: anything partial
    ("r2 change partial", w(change="partial"), "", False, "E_PARTIAL"),
    ("r2 delivery partial", w(delivery="partial"), "", False, "E_PARTIAL"),
    ("r2 persistence partial", w(persistence="partial"), "", False, "E_PARTIAL"),
    # rule 3: failed execution with a lasting effect
    ("r3 failed + applied", w(execution="failed"), "E_SERVER", False, "E_PARTIAL"),
    (
        "r3 interrupted + delivered",
        w(execution="interrupted", change="not_applied", delivery="delivered"),
        "",
        False,
        "E_PARTIAL",
    ),
    (
        "r3 not_started + saved",
        w(execution="not_started", change="not_applied", persistence="saved"),
        "",
        False,
        "E_PARTIAL",
    ),
    # rule 4: failed execution without a lasting effect
    (
        "r4 failed with cause",
        w(execution="failed", change="not_applied"),
        "E_VALIDATION",
        False,
        "E_VALIDATION",
    ),
    ("r4 failed default", w(execution="failed", change="not_applied"), "", False, "E_SERVER"),
    (
        "r4 not_started",
        w(execution="not_started", change="not_applied"),
        "E_NETWORK",
        False,
        "E_NETWORK",
    ),
    (
        "r4 interrupted rolled back",
        w(execution="interrupted", change="rolled_back"),
        "",
        False,
        "E_SERVER",
    ),
    # rule 5: the change was rolled back (decision 14)
    ("r5 rolled back default", w(change="rolled_back"), "", False, "E_VERIFY_FAILED"),
    ("r5 rolled back with cause", w(change="rolled_back"), "E_CONFLICT", False, "E_CONFLICT"),
    ("r5 beats r6", w(change="rolled_back", delivery="rejected"), "", False, "E_VERIFY_FAILED"),
    # rule 6: delivery rejected
    (
        "r6 rejected default",
        w(change="not_applicable", delivery="rejected"),
        "",
        False,
        "E_FORBIDDEN",
    ),
    (
        "r6 rejected with cause",
        w(change="not_applicable", delivery="rejected"),
        "E_VALIDATION",
        False,
        "E_VALIDATION",
    ),
    # rules 7, 8: verification
    ("r7 verify failed", w(verification="failed"), "", False, "E_VERIFY_FAILED"),
    ("r8 verify incomplete", w(verification="incomplete"), "", False, "E_VERIFY_INCOMPLETE"),
    # rule 9: otherwise ok (base profile: not_run with applied is ok)
    ("r9 passed", OK, "", True, ""),
    ("r9 not_run", w(verification="not_run"), "", True, ""),
    (
        "r9 not_applicable",
        w(change="not_applicable", delivery="delivered", verification="not_applicable"),
        "",
        True,
        "",
    ),
]


@pytest.mark.parametrize(("name", "o", "cause", "ok", "code"), RULES, ids=[r[0] for r in RULES])
def test_receipt_rules(name: str, o: Outcome, cause: str, ok: bool, code: str) -> None:
    assert derive_receipt(o, cause) == (ok, code)


def test_receipt_rule_count_matches_the_contract() -> None:
    p = Path(__file__).resolve().parents[3] / "common" / "contract" / "contract.json"
    if not p.exists():
        pytest.skip("contract.json not available here")
    rules = json.loads(p.read_text(encoding="utf-8"))["receipt_rules"]["rules"]
    assert len(rules) == RECEIPT_RULE_COUNT == 9


def test_outcome_normalisation_fails_closed() -> None:
    m = Meta(effects=[CHANGE, DELIVER])
    o = normalize_outcome(Outcome(change="done", delivery="sent", verification="ok"), m)
    assert (o.change, o.delivery, o.verification) == ("unknown", "unknown", "incomplete"), o
    assert derive_receipt(o, "") == (False, "E_OUTCOME_UNKNOWN")
    d = normalize_outcome(Outcome(), m)
    assert (d.execution, d.change, d.delivery, d.persistence) == (
        "completed",
        "applied",
        "delivered",
        "not_applicable",
    ), d


def test_auto_verify() -> None:
    changes = [
        {"field": "title", "after": "New"},
        {"field": "tags", "add": ["x"]},
        {"field": "labels", "remove": ["old"]},
    ]
    assert auto_verify(changes, {"title": "New", "tags": ["a", "x"], "labels": ["keep"]}) == (
        "passed",
        [],
    )
    v, mm = auto_verify(changes, {"title": "Old", "tags": ["a"], "labels": ["old"]})
    assert v == "failed" and len(mm) == 3, (v, mm)
    assert auto_verify(changes, {})[0] == "not_run"
