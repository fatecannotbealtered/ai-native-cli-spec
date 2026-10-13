"""The write gate end to end through the test tool (port of gate_test.go)."""

from __future__ import annotations

import datetime as _dt
import os
import threading
from typing import Any

import pytest
from aicli_testapp import (
    THarness,
    TOut,
    Upstream,
    expect_conflict,
    expect_mismatch,
    list_has,
    notes_app,
)

import aicli
from aicli import Op, Preview, Result, Target, WriteSpec
from aicli._util import time_string
from aicli.conformance import _is_token as is_token
from aicli.token import TOKEN_LIFETIME


def test_bare_write_is_refused_with_preview_and_token(h: THarness) -> None:
    r = h.run("note", "update", "1", "--title", "New")
    assert r.exit == 5 and r.code() == "E_CONFIRMATION_REQUIRED", r
    d = r.details()
    assert d["problem"] == "confirmation_required" and d.get("preview") and d.get("operation_id"), r
    assert is_token(d.get("confirm_token")), r
    assert r.token() in d["next_action"], d["next_action"]
    assert h.up.writes() == 0, "a bare call wrote upstream"
    # The refusal's token confirms directly (no extra dry-run round trip).
    c = h.run("note", "update", "1", "--title", "New", "--confirm", r.token())
    assert c.exit == 0 and h.up.writes() == 1, c
    n = h.up.get("1")
    assert n is not None and n.title == "New"


def test_dry_run_returns_preview_and_changes_nothing(h: THarness) -> None:
    r = h.run("note", "update", "1", "--title", "New", "--dry-run")
    assert r.exit == 0 and is_token(r.token()), r
    p = r.data()["preview"]
    assert p["command"] == "note update" and p["can_apply"] is True and p.get("base") is not None, p
    assert p["inputs"]["title"] == "New" and p["inputs"]["id"] == "1", p["inputs"]
    assert h.up.writes() == 0
    assert list_has(r.env["meta"].get("untrusted"), "data.preview.changes[].before"), r.env["meta"]


def test_confirm_runs_once_then_token_used(h: THarness) -> None:
    tok = h.must_token("note", "create", "--title", "Hello")
    r = h.run("note", "create", "--title", "Hello", "--confirm", tok)
    assert r.exit == 0 and h.up.writes() == 1, r
    rec = r.data()["receipt"]
    out = rec["outcome"]
    assert out["execution"] == "completed" and out["change"] == "applied", rec
    assert out["verification"] == "passed" and rec["replayed"] is False, rec
    again = h.run("note", "create", "--title", "Hello", "--confirm", tok)
    expect_conflict(again, "token_used")
    assert h.up.writes() == 1, "a used token wrote again"
    show = h.run("operation", "show", r.data()["operation_id"])
    assert show.exit == 0 and show.data()["status"] == "applied" and show.data().get("receipt"), (
        show
    )


def test_concurrent_confirms_from_threads_write_once(h: THarness) -> None:
    tok = h.must_token("note", "update", "2", "--title", "Raced")
    n = 16
    results: list[TOut | None] = [None] * n
    start = threading.Event()

    def worker(i: int) -> None:
        start.wait()
        results[i] = h.run("note", "update", "2", "--title", "Raced", "--confirm", tok)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    start.set()
    for t in threads:
        t.join()
    ok = 0
    for r in results:
        assert r is not None
        if r.exit == 0:
            ok += 1
        else:
            assert r.code() == "E_CONFLICT" and r.details().get("conflict") == "token_used", r
    assert ok == 1 and h.up.writes() == 1, f"{ok} successes, {h.up.writes()} writes"


def test_token_expires(h: THarness) -> None:
    clock = [_dt.datetime.now(_dt.timezone.utc)]
    h.now = lambda: clock[0]
    tok = h.must_token("note", "update", "1", "--title", "Late")
    clock[0] += TOKEN_LIFETIME + _dt.timedelta(seconds=1)
    h.grant({"expires_at": time_string(clock[0] + _dt.timedelta(hours=1))})
    r = h.run("note", "update", "1", "--title", "Late", "--confirm", tok)
    expect_conflict(r, "token_expired")
    assert h.up.writes() == 0
    # The conflict carries a fresh plan, so one more call applies it.
    fresh = r.details().get("confirm_token")
    assert (
        fresh and fresh != tok and r.details().get("preview") and r.details().get("operation_id")
    ), r
    assert fresh in r.details()["next_action"], r
    r = h.run("note", "update", "1", "--title", "Late", "--confirm", fresh)
    assert r.exit == 0 and h.up.writes() == 1, r


def test_text_mode_cannot_bypass_the_gate(h: THarness) -> None:
    r = h.run("note", "update", "1", "--title", "X", "--format", "text")
    assert r.exit == 5 and h.up.writes() == 0, r
    assert "E_CONFIRMATION_REQUIRED" in r.stderr and "ct2_" in r.stderr, r.stderr
    for mode in (["--pretty"], ["--quiet"], ["--compact"], ["--json"], ["--format", "json"]):
        r = h.run("note", "update", "1", "--title", "X", *mode)
        assert r.exit == 5 and h.up.writes() == 0, (mode, r)
    r = h.run("note", "update", "1", "--title", "X", "--format", "raw")
    assert (
        r.exit == 2 and r.details().get("problem") == "unsupported_format" and h.up.writes() == 0
    ), r
    tok = h.must_token("note", "update", "1", "--title", "X")
    r = h.run("note", "update", "1", "--title", "X", "--confirm", tok, "--format", "text")
    assert r.exit == 0 and h.up.writes() == 1, r


def test_dangerous_needs_the_flag_in_both_steps(h: THarness) -> None:
    r = h.run("note", "delete", "1", "--dry-run")
    assert (
        r.exit == 5 and r.details().get("problem") == "dangerous_flag_required" and r.token() == ""
    ), r
    # The refusal shows the plan (read-only, no token) so it can go to the user at once.
    p = r.details().get("preview")
    assert (
        isinstance(p, dict) and p["command"] == "note delete" and "confirm_token" not in r.details()
    ), r
    assert "--dangerous" in r.details()["next_action"], r
    r = h.run("note", "delete", "1")
    assert r.exit == 5 and r.details().get("problem") == "dangerous_flag_required", r
    tok = h.must_token("note", "delete", "1", "--dangerous")
    r = h.run("note", "delete", "1", "--confirm", tok)
    assert (
        r.exit == 5
        and r.details().get("problem") == "dangerous_flag_required"
        and h.up.writes() == 0
    ), r
    r = h.run("note", "delete", "1", "--confirm", tok, "--dangerous")
    assert r.exit == 0 and h.up.writes() == 1, r
    r = h.run("note", "share", "2", "--to", "bob@example.com", "--dry-run")
    assert r.exit == 5 and r.details().get("problem") == "dangerous_flag_required", r


def test_state_drift_refuses_the_confirm(h: THarness) -> None:
    tok = h.must_token("note", "update", "1", "--title", "Mine")
    h.up.bump("1")
    r = h.run("note", "update", "1", "--title", "Mine", "--confirm", tok)
    expect_conflict(r, "state_drift")
    assert h.up.writes() == 0
    fresh = r.details().get("confirm_token")
    assert fresh and fresh != tok and r.details().get("preview"), r
    r = h.run("note", "update", "1", "--title", "Mine", "--confirm", tok)
    assert r.code() == "E_CONFLICT" and h.up.writes() == 0, "the drifted token must stay refused"
    r = h.run("note", "update", "1", "--title", "Mine", "--confirm", fresh)
    assert r.exit == 0 and h.up.writes() == 1, r


def test_scope_changed_refuses_the_confirm(h: THarness) -> None:
    tok = h.must_token("note", "update", "1", "--title", "Mine")
    h.grant({"notes": "edited"})
    r = h.run("note", "update", "1", "--title", "Mine", "--confirm", tok)
    expect_conflict(r, "scope_changed")
    assert h.up.writes() == 0


def test_dry_run_and_confirm_together_is_usage(h: THarness) -> None:
    tok = h.must_token("note", "update", "1", "--title", "X")
    r = h.run("note", "update", "1", "--title", "X", "--dry-run", "--confirm", tok)
    assert (
        r.exit == 2 and r.details().get("problem") == "conflicting_flags" and h.up.writes() == 0
    ), r
    assert r.details()["next_action"].startswith("Drop --dry-run"), r


def test_blocked_preview_issues_no_token(h: THarness) -> None:
    r = h.run("note", "update", "1", "--dry-run")
    assert r.exit == 0 and r.data()["confirm_token"] is None and r.data()["expires_at"] is None, r
    p = r.data()["preview"]
    assert p["can_apply"] is False and p["blocked_by"], p
    r = h.run("note", "update", "1")
    assert r.exit == 5 and r.details().get("confirm_token") is None, r


def test_preview_errors_keep_their_code_and_send_nothing(h: THarness) -> None:
    r = h.run("note", "update", "99", "--title", "X", "--dry-run")
    assert (
        r.exit == 3
        and r.code() == "E_NOT_FOUND"
        and r.details().get("dispatch_state") == "not_started"
    ), r


def _raise(exc: BaseException) -> Any:
    def hook(kind: str, nid: str) -> None:
        raise exc

    return hook


APPLY_CASES = [
    (
        "not dispatched",
        lambda k, i: aicli.not_dispatched(aicli.from_http(0, "The upstream cannot be reached.")),
        "E_NETWORK",
        7,
        "not_started",
        "not_applied",
    ),
    (
        "rejected",
        lambda k, i: aicli.rejected(
            aicli.from_http(422, "The title is too long.", "title too long")
        ),
        "E_VALIDATION",
        2,
        "dispatched",
        "not_applied",
    ),
    (
        "rejected without a code",
        lambda k, i: aicli.rejected(ValueError("refused")),
        "E_SERVER",
        7,
        "dispatched",
        "not_applied",
    ),
    (
        "connection reset",
        lambda k, i: ConnectionResetError("connection reset by peer"),
        "E_OUTCOME_UNKNOWN",
        6,
        "unknown",
        "unknown",
    ),
    (
        "plain HTTP 500",
        lambda k, i: aicli.from_http(500, "Server error."),
        "E_OUTCOME_UNKNOWN",
        6,
        "unknown",
        "unknown",
    ),
    ("panic", _raise(RuntimeError("boom")), "E_OUTCOME_UNKNOWN", 6, "unknown", "unknown"),
    (
        "deadline",
        lambda k, i: TimeoutError("deadline exceeded"),
        "E_OUTCOME_UNKNOWN",
        6,
        "unknown",
        "unknown",
    ),
    ("interrupt", _raise(KeyboardInterrupt()), "E_OUTCOME_UNKNOWN", 6, "unknown", "unknown"),
]


@pytest.mark.parametrize(
    ("name", "hook", "code", "exit_code", "dispatch", "status"),
    APPLY_CASES,
    ids=[c[0] for c in APPLY_CASES],
)
def test_apply_error_classification(
    h: THarness, name: str, hook: Any, code: str, exit_code: int, dispatch: str, status: str
) -> None:
    tok = h.must_token("note", "update", "1", "--title", "X")
    h.up.hook = hook
    r = h.run("note", "update", "1", "--title", "X", "--confirm", tok)
    assert (
        r.exit == exit_code and r.code() == code and r.details().get("dispatch_state") == dispatch
    ), r
    assert h.up.writes() == 0
    op_id = r.details().get("operation_id")
    assert op_id, r
    if code == "E_OUTCOME_UNKNOWN":
        assert "operation show --id " + op_id in r.details()["lookup"], r.details()["lookup"]
        # next_action names the read that shows whether the change landed.
        na = r.details()["next_action"]
        assert "`notes note get 1`" in na and "operation resolve --id " + op_id in na, na
    h.up.hook = None
    show = h.run("operation", "show", "--id", op_id)
    assert show.data()["status"] == status, show
    again = h.run("note", "update", "1", "--title", "X", "--confirm", tok)
    expect_conflict(again, "token_used")


def test_unknown_outcome_blocks_the_target_until_resolved(h: THarness) -> None:
    tok = h.must_token("note", "update", "1", "--title", "X")
    h.up.hook = lambda k, i: OSError("timeout after send")
    r = h.run("note", "update", "1", "--title", "X", "--confirm", tok)
    assert r.code() == "E_OUTCOME_UNKNOWN", r
    h.up.hook = None
    op_id = r.details()["operation_id"]
    blocked = h.run("note", "update", "1", "--title", "Y", "--dry-run")
    expect_conflict(blocked, "unresolved_operation")
    assert blocked.details().get("active_operation") == op_id, blocked
    other = h.run("note", "update", "2", "--title", "Y", "--dry-run")
    assert other.exit == 0, other
    lst = h.run("operation", "list", "--state", "unknown")
    assert lst.exit == 0 and lst.data()["count"] == 1, lst
    h.scope_on = False
    res = h.run(
        "operation", "resolve", op_id, "--outcome", "not_applied", "--evidence", "checked the note"
    )
    assert res.exit == 4 and res.code() == "E_FORBIDDEN", res
    h.scope_on = True
    res = h.run(
        "operation",
        "resolve",
        op_id,
        "--outcome",
        "not_applied",
        "--evidence",
        "the title is unchanged upstream",
    )
    assert res.exit == 0 and res.data()["status"] == "resolved", res
    res2 = h.run("operation", "resolve", op_id, "--outcome", "applied", "--evidence", "again")
    assert res2.exit != 0, res2
    again = h.run("note", "update", "1", "--title", "Y", "--dry-run")
    assert again.exit == 0, again


def test_resolve_refuses_while_the_lease_is_live(h: THarness) -> None:
    tok = h.must_token("note", "update", "1", "--title", "X")
    problems: list[str] = []

    def hook(kind: str, nid: str) -> None:
        lst = h.run("operation", "list", "--state", "dispatching")
        items = lst.data().get("items") or []
        if len(items) != 1:
            problems.append(f"want one dispatching operation, got {lst}")
            return None
        op_id = items[0]["operation_id"]
        r = h.run("operation", "resolve", op_id, "--outcome", "applied", "--evidence", "guess")
        if not (
            r.code() == "E_CONFLICT"
            and r.details().get("conflict") == "lock_held"
            and r.details().get("active_operation") == op_id
        ):
            problems.append(f"resolve during the lease: {r}")
        blocked = h.run("note", "update", "1", "--title", "Z", "--dry-run")
        if blocked.details().get("conflict") != "unresolved_operation":
            problems.append(f"a write during the lease must be blocked: {blocked}")
        return None

    h.up.hook = hook
    r = h.run("note", "update", "1", "--title", "X", "--confirm", tok)
    assert r.exit == 0, r
    assert not problems, problems


def test_expired_lease_reads_as_unknown(h: THarness) -> None:
    clock = [_dt.datetime.now(_dt.timezone.utc)]
    h.now = lambda: clock[0]
    tok = h.must_token("note", "update", "1", "--title", "X")
    problems: list[str] = []

    def hook(kind: str, nid: str) -> None:
        clock[0] += _dt.timedelta(minutes=10)  # the process "hangs" past its lease
        lst = h.run("operation", "list", "--state", "unknown")
        if lst.data().get("count") != 1:
            problems.append(f"an expired lease must read as unknown: {lst}")
        return None

    h.up.hook = hook
    r = h.run("note", "update", "1", "--title", "X", "--confirm", tok)
    assert r.exit == 0, r
    assert not problems, problems


def test_verification_from_read_back(h: THarness) -> None:
    tok = h.must_token("note", "update", "1", "--title", "Exact")
    r = h.run("note", "update", "1", "--title", "Exact", "--confirm", tok)
    assert r.data()["receipt"]["outcome"]["verification"] == "passed", r
    assert list_has(r.env["meta"].get("untrusted"), "data.receipt.observed"), r.env["meta"]


def test_verification_mismatch_is_verify_failed(h: THarness) -> None:
    tok = h.must_token("note", "update", "1", "--title", "A very long title")
    h.up.mangle = lambda s: s[:6]  # the upstream silently truncates
    r = h.run("note", "update", "1", "--title", "A very long title", "--confirm", tok)
    assert r.exit == 1 and r.code() == "E_VERIFY_FAILED" and h.up.writes() == 1, r
    rec = r.details()["receipt"]
    assert rec["outcome"]["change"] == "applied" and list_has(rec["mismatched"], "title"), rec
    # The record says what happened upstream: the change is there, so it is applied
    # (check-contract.py operation_status); the Go library's test still says failed.
    show = h.run("operation", "show", r.details()["operation_id"])
    assert show.data()["status"] == "applied", show


def test_idempotency_key_replays_the_original_receipt(h: THarness) -> None:
    args = ["note", "create", "--title", "Once", "--idempotency-key", "k-1"]
    tok = h.must_token(*args)
    # A token issued before the first confirm meets the applied operation at its own
    # confirm (a dry-run after it is answered with the replay at once, P4).
    tok2 = h.must_token(*args)
    first = h.run(*args, "--confirm", tok)
    assert first.exit == 0 and h.up.writes() == 1, first
    second = h.run(*args, "--confirm", tok2)
    assert second.exit == 0 and h.up.writes() == 1, second
    rec = second.data()["receipt"]
    assert (
        rec["replayed"] is True and second.data()["operation_id"] == first.data()["operation_id"]
    ), second
    other = h.run("note", "create", "--title", "Different", "--idempotency-key", "k-1", "--dry-run")
    expect_conflict(other, "idempotency_mismatch")


def _click_command(a: aicli.App, path: str) -> Any:
    c = a.root
    for w in path.split():
        c = c.commands[w]
    return c


def test_write_gate_requires_registered_write_command(h: THarness) -> None:
    def build(u: Upstream) -> aicli.App:
        a = notes_app(u)
        get = _click_command(a, "note get")

        def callback(nid: Any) -> None:
            def apply(op: Op) -> Result:
                u.write(lambda: None)
                return Result()

            a.write(
                None,
                WriteSpec(
                    target=Target("notes/1"), preview=lambda: Preview(can_apply=True), apply=apply
                ),
            )

        get.callback = callback
        return a

    h.build = build
    r = h.run(
        "note",
        "get",
        "1",
        "--confirm",
        "ct2_aaaaaaaaaaaaaaaaaaaaaaaaaa_00000000000000000000000000000000",
    )
    assert r.code() == "E_UNKNOWN" and h.up.writes() == 0, r


def test_ok_on_write_command_is_refused(h: THarness) -> None:
    def build(u: Upstream) -> aicli.App:
        a = notes_app(u)
        c = _click_command(a, "note create")
        c.callback = lambda **kw: a.ok(None, {"done": True})
        return a

    h.build = build
    r = h.run("note", "create", "--title", "X")
    assert r.code() == "E_UNKNOWN", r


def test_no_unkeyed_fallback_when_the_key_is_broken(h: THarness) -> None:
    os.makedirs(h.state, mode=0o700, exist_ok=True)
    for name, content in {"empty": b"", "short": b"short", "long": bytes(33)}.items():
        with open(os.path.join(h.state, "confirm.key"), "wb") as fh:
            fh.write(content)
        for extra in (["--dry-run"], []):
            r = h.run("note", "update", "1", "--title", "X", *extra)
            assert r.exit == 1 and r.code() == "E_IO" and "ct2_" not in r.stdout, (name, extra, r)
    assert h.up.writes() == 0


def test_key_removed_after_dry_run_refuses_the_confirm(h: THarness) -> None:
    tok = h.must_token("note", "update", "1", "--title", "X")
    key = os.path.join(h.state, "confirm.key")
    os.remove(key)
    r = h.run("note", "update", "1", "--title", "X", "--confirm", tok)
    expect_mismatch(r, "--confirm")
    assert not os.path.exists(key), "a confirm must not create a key"
    h.must_token("note", "update", "2", "--title", "Y")
    r = h.run("note", "update", "1", "--title", "X", "--confirm", tok)
    expect_mismatch(r, "--confirm")
    assert h.up.writes() == 0


def test_forged_token_for_another_record_is_rejected(h: THarness) -> None:
    t1 = h.must_token("note", "update", "1", "--title", "A")
    t2 = h.must_token("note", "update", "2", "--title", "B")
    for forged in (t2[:31] + t1[31:], t1[:31] + t2[31:]):
        for args in (["1", "--title", "A"], ["2", "--title", "B"]):
            r = h.run("note", "update", *args, "--confirm", forged)
            assert r.exit != 0, r
    assert h.up.writes() == 0


def test_tampered_record_fails_the_mac(h: THarness) -> None:
    r = h.run("note", "update", "1", "--title", "A", "--dry-run")
    tok = r.token()
    path = os.path.join(h.state, "operations", r.data()["operation_id"] + ".json")
    with open(path, "rb") as fh:
        b = fh.read()
    tampered = b.replace(b'"tool_version":"1.2.3"', b'"tool_version":"9.9.9"', 1)
    assert tampered != b, "record layout changed; update the test"
    with open(path, "wb") as fh:
        fh.write(tampered)
    out = h.run("note", "update", "1", "--title", "A", "--confirm", tok)
    expect_mismatch(out, "--confirm")


# ---------------------------------------------------------------- decisions beyond the Go tests


def test_replay_requires_the_same_target(h: THarness) -> None:
    """Decision 32: a replay needs the same command, key, inputs AND target key; the
    same inputs aimed at another target are idempotency_mismatch."""
    folder = ["inbox"]
    keys: list[str] = []

    def build(u: Upstream) -> aicli.App:
        a = notes_app(u)
        c = _click_command(a, "note create")

        def callback(title: str, body: str, body_file: Any, tag: Any, idempotency_key: Any) -> None:
            def apply(op: Op) -> Result:
                keys.append(op.idempotency_key)
                u.write(lambda: None)
                return Result(observed={"title": title}, data={"title": title})

            a.write(
                None,
                WriteSpec(
                    target=Target("folders/" + folder[0], "folder"),
                    preview=lambda: Preview(
                        action="Create a note",
                        can_apply=True,
                        changes=[aicli.FieldChange("title", after=title)],
                    ),
                    apply=apply,
                ),
            )

        c.callback = callback
        return a

    h.build = build
    args = ["note", "create", "--title", "Same", "--idempotency-key", "k-9"]
    first = h.run(*args, "--confirm", h.must_token(*args))
    assert first.exit == 0 and h.up.writes() == 1 and keys == ["k-9"], first
    replay = h.run(*args, "--dry-run")  # the replay is answered at once (P4)
    assert (
        replay.exit == 0 and replay.data()["receipt"]["replayed"] is True and h.up.writes() == 1
    ), replay
    folder[0] = "archive"  # same command, key and inputs; another target
    r = h.run(*args, "--dry-run")
    expect_conflict(r, "idempotency_mismatch")
    r = h.run(*args)
    expect_conflict(r, "idempotency_mismatch")
    assert h.up.writes() == 1


def test_rejected_conflict_carries_upstream_conflict(h: THarness) -> None:
    """Decision 14: an upstream 409 during apply is E_CONFLICT upstream_conflict, dispatched."""
    tok = h.must_token("note", "update", "1", "--title", "X")
    h.up.hook = lambda k, i: aicli.rejected(aicli.from_http(409, "The note is locked."))
    r = h.run("note", "update", "1", "--title", "X", "--confirm", tok)
    assert r.exit == 6 and r.code() == "E_CONFLICT", r
    assert (
        r.details().get("conflict") == "upstream_conflict"
        and r.details().get("dispatch_state") == "dispatched"
    ), r
    h.up.hook = None
    show = h.run("operation", "show", r.details()["operation_id"])
    assert show.data()["status"] == "not_applied", show


def test_from_http_is_not_started_outside_apply(h: THarness) -> None:
    """Decision 1: FromHTTP's dispatch_state is not_started; the gate decides after dispatch."""
    e = aicli.from_http(503, "Unavailable.")
    assert e.details["dispatch_state"] == "not_started" and e.details["http_status"] == 503
    assert aicli.from_http(0, "down").details == {"dispatch_state": "not_started"}
    assert aicli.from_http(413, "big").details["problem"] == "input_too_large"
    assert aicli.from_http(409, "c").details["conflict"] == "upstream_conflict"
    assert aicli.from_http(412, "c").details["conflict"] in ("state_drift", "upstream_conflict")
    assert aicli.from_http(429, "slow", "", 2.5).details["retry_after_ms"] == 2500


def test_malformed_token_says_what_to_drop(h: THarness) -> None:
    tok = h.must_token("note", "update", "1", "--title", "X")
    for bad in ("<" + tok + ">", tok.upper(), " " + tok + " ", '"' + tok + '"'):
        r = h.run("note", "update", "1", "--title", "X", "--confirm", bad)
        expect_conflict(r, "token_mismatch")
        assert "exactly as the preview issued it" in r.details()["next_action"], (bad, r)
    assert h.up.writes() == 0, "a wrapped token wrote"
    # Only the exact form is accepted, and it still applies once.
    r = h.run("note", "update", "1", "--title", "X", "--confirm", tok)
    assert r.exit == 0 and h.up.writes() == 1, r


def test_file_and_literal_mismatch_explains_the_bytes(h: THarness) -> None:
    f = os.path.join(h.dir, "body.txt")
    with open(f, "wb") as fh:
        fh.write(b"Hello\n")
    tok = h.must_token("note", "update", "1", "--body-file", f)
    r = h.run("note", "update", "1", "--body", "Hello", "--confirm", tok)
    expect_conflict(r, "token_mismatch")
    na = r.details()["next_action"]
    assert "6 bytes" in na and "--body-file" in na, r
    tok = h.must_token("note", "update", "1", "--body", "Hello")
    r = h.run("note", "update", "1", "--body-file", f, "--confirm", tok)
    expect_conflict(r, "token_mismatch")
    assert "literal --body" in r.details()["next_action"], r
