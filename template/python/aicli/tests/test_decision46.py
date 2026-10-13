"""Regression tests for the fixes before v2.0.0-rc.1 (decision 46, FIXES-RC1.md
sections A to D and F): one test per item, named after it. The update fixes
(section E) are tested in test_update.py, next to its fake release server."""

from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import json
import os
import threading
import urllib.parse
import uuid
from typing import Any

import click
import pytest
from aicli_testapp import (
    THarness,
    TNote,
    TOut,
    Upstream,
    default_scope,
    expect_conflict,
    expect_mismatch,
    notes_app,
)
from test_settings import settings_app, settings_file

import aicli
from aicli import (
    CHANGE,
    OBSERVE,
    App,
    FieldChange,
    Meta,
    Op,
    Preview,
    Result,
    SchemaDef,
    Setting,
    Target,
    WriteSpec,
)
from aicli._util import parse_time
from aicli.builtins import redact_host
from aicli.token import TOKEN_LIFETIME

WINDOWS = os.name == "nt"


def op_of(tok: str) -> str:
    """The operation id a confirm token names."""
    return "op_" + tok[4:30]


def journal_text(h: THarness) -> str:
    out = []
    for root, _dirs, files in os.walk(h.state):
        for name in files:
            with open(os.path.join(root, name), "rb") as fh:
                out.append(fh.read().decode("utf-8", "replace"))
    return "\n".join(out)


def clocked(h: THarness) -> list[_dt.datetime]:
    clock = [_dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)]
    h.now = lambda: clock[0]
    return clock


def touch_app(u: Upstream, data: Any = None, outcome: Any = "default") -> App:
    """The test tool with `note touch`, whose apply returns the given data (and
    outcome, when not "default")."""
    a = notes_app(u)
    note = a.root.commands["note"]

    @note.command("touch")
    @click.option("--id", "nid", required=True, help="Note id")
    def touch(nid: str) -> None:
        def preview() -> Preview:
            return Preview(
                action="Touch", can_apply=True, changes=[FieldChange("touched", after=1)]
            )

        def apply(op: Op) -> Result:
            u.write(lambda: None)
            r = Result(data=data(nid) if callable(data) else data)
            if outcome != "default":
                r.outcome = outcome
            return r

        a.write(
            None, WriteSpec(target=Target("notes/" + nid, "note"), preview=preview, apply=apply)
        )

    a.register(
        touch,
        Meta(
            summary="Touch a note",
            effects=[CHANGE],
            output="note_write",
            positional="id",
            examples=["notes note touch 1 --dry-run", "notes note touch 1 --confirm <token>"],
        ),
    )
    return a


def keyed_app(u: Upstream) -> App:
    """The test tool with --idempotency-key on bulk-tag and comment add too."""
    a = notes_app(u)
    note = a.root.commands["note"]
    for cmd in (note.commands["bulk-tag"], note.commands["comment"].commands["add"]):
        cmd.params.append(click.Option(["--idempotency-key"], expose_value=False, help="Key"))
    return a


# ---------------------------------------------------------------- A. write gate


def test_a1_used_token_after_expiry(h: THarness) -> None:
    """A used token is token_used whatever its age: never token_expired with a fresh
    plan that would send the write again."""
    clock = clocked(h)
    args = ("note", "share", "1", "--to", "bob@example.com", "--dangerous")
    tok = h.must_token(*args)
    assert h.run(*args, "--confirm", tok).exit == 0 and h.up.writes() == 1
    clock[0] += TOKEN_LIFETIME + _dt.timedelta(minutes=1)
    r = h.run(*args, "--confirm", tok)
    expect_conflict(r, "token_used")
    assert "confirm_token" not in r.details() and "preview" not in r.details(), r
    assert h.up.shared == ["1->bob@example.com"] and h.up.writes() == 1


def test_a1_used_batch_token_after_expiry(h: THarness) -> None:
    clock = clocked(h)
    args = ("note", "bulk-tag", "--ids", "1,2", "--tag", "x")
    tok = h.must_token(*args)
    assert h.run(*args, "--confirm", tok).exit == 0 and h.up.writes() == 2
    clock[0] += TOKEN_LIFETIME + _dt.timedelta(minutes=1)
    expect_conflict(h.run(*args, "--confirm", tok), "token_used")
    assert h.up.writes() == 2


def test_a2_unencodable_result_keeps_the_receipt(h: THarness) -> None:
    """A result that cannot be encoded (a UUID) is replaced by {}; the receipt, the
    record and ok stand, with a result_unencodable notice and a defect."""
    apps: list[App] = []

    def build(u: Upstream) -> App:
        a = touch_app(u, lambda nid: {"id": nid, "request_id": uuid.uuid4()})
        apps.append(a)
        return a

    h.build = build
    tok = h.must_token("note", "touch", "1")
    r = h.run("note", "touch", "1", "--confirm", tok)
    assert r.exit == 0 and h.up.writes() == 1, r
    assert r.data()["result"] == {} and r.data()["operation_id"] == op_of(tok), r
    assert r.data()["receipt"]["outcome"]["change"] == "applied", r
    assert {
        "type": "result_unencodable",
        "severity": "warning",
        "message": "The change was dispatched, but the tool's result could not be encoded, so "
        f"data.result is empty; run `notes operation show --id {op_of(tok)}` for the receipt.",
    } in r.env["meta"]["notices"], r.env["meta"]
    assert any("cannot be encoded" in d for d in apps[-1].defects), apps[-1].defects
    show = h.run("operation", "show", "--id", op_of(tok))
    assert show.data()["status"] == "applied" and show.data()["receipt"] is not None, show


def test_a2_failure_after_dispatch_is_outcome_unknown(h: THarness) -> None:
    """A failure the gate cannot classify after the dispatching record is
    E_OUTCOME_UNKNOWN with the operation id and its lookup, never not_started."""
    h.build = lambda u: touch_app(u, outcome=None)  # not an Outcome: classify fails
    tok = h.must_token("note", "touch", "1")
    r = h.run("note", "touch", "1", "--confirm", tok)
    assert r.code() == "E_OUTCOME_UNKNOWN" and h.up.writes() == 1, r
    d = r.details()
    assert d["dispatch_state"] == "unknown" and d["operation_id"] == op_of(tok), d
    assert d["lookup"] == f"notes operation show --id {op_of(tok)}", d
    # the record stays dispatching: its lease blocks the target, then it reads unknown
    assert h.run("operation", "show", "--id", op_of(tok)).data()["status"] == "dispatching"
    expect_conflict(h.run("note", "touch", "1", "--dry-run"), "unresolved_operation")


def test_a3_lock_held_while_another_confirm_checks(
    h: THarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A confirm waits for the target's lock (at most 10 s, shortened here), then
    answers lock_held naming the target and the holder; a stale lock is broken."""
    import aicli.gate as gate

    monkeypatch.setattr(gate, "TARGET_LOCK_WAIT", 0.3)
    tok = h.must_token("note", "update", "1", "--title", "Locked")
    holder = "op_" + "a" * 26
    d = os.path.join(h.state, "locks")
    os.makedirs(d, exist_ok=True)
    lock = os.path.join(d, hashlib.sha256(b"notes/1").hexdigest()[:32] + ".lock")
    with open(lock, "w", encoding="utf-8") as fh:
        fh.write(holder)  # the holder's operation id, as acquire_lock writes it
    r = h.run("note", "update", "1", "--title", "Locked", "--confirm", tok)
    expect_conflict(r, "lock_held")
    assert r.env["error"]["message"] == (
        "Another confirm on the same target is still running; nothing was sent."
    )
    assert r.details()["next_action"] == (
        "Run the same command again in a few seconds; the confirm token stays valid until it "
        "expires."
    )
    assert r.details()["target"] == "notes/1" and r.details()["active_operation"] == holder, r
    assert h.up.writes() == 0
    old = _dt.datetime.now().timestamp() - 60
    os.utime(lock, (old, old))  # a holder that died 60 s ago
    r = h.run("note", "update", "1", "--title", "Locked", "--confirm", tok)
    assert r.exit == 0 and h.up.writes() == 1, r
    assert os.listdir(d) == [], "the lock is released once the dispatching record is written"


def test_a3_concurrent_confirms_of_one_target_write_once(h: THarness) -> None:
    """Tokens planned against the same version of one target: the lock serialises
    their checks, so only the first is sent; the others see its record or the
    changed version."""
    n = 6
    toks = [h.must_token("note", "update", "2", "--title", f"T{i}") for i in range(n)]
    results: list[TOut | None] = [None] * n
    start = threading.Event()

    def worker(i: int) -> None:
        start.wait()
        results[i] = h.run("note", "update", "2", "--title", f"T{i}", "--confirm", toks[i])

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
            continue
        assert r.code() == "E_CONFLICT", r
        assert r.details()["conflict"] in ("unresolved_operation", "state_drift"), r
    assert ok == 1 and h.up.writes() == 1, f"{ok} successes, {h.up.writes()} writes"


def write_scope(h: THarness, **change: Any) -> None:
    sc = default_scope()
    sc.update(change)
    with open(h.scope, "w", encoding="utf-8") as fh:
        json.dump(sc, fh)


def usage(h: THarness) -> dict[str, Any]:
    with open(os.path.join(h.state, "scope-usage.json"), encoding="utf-8") as fh:
        return json.load(fh)


def test_a4_budget_survives_pruning(h: THarness) -> None:
    """limits.max_operations counts from scope-usage.json, which pruning never
    touches: a pruned record does not give its operation back."""
    clock = clocked(h)
    write_scope(h, expires_at=None, limits={"max_operations": 2})
    first = h.must_token("note", "update", "1", "--title", "A")
    assert h.run("note", "update", "1", "--title", "A", "--confirm", first).exit == 0
    assert usage(h) == {"v": 1, "scopes": {"scp_test": 1}}
    clock[0] += _dt.timedelta(days=31)
    tok = h.must_token("note", "update", "2", "--title", "B")  # the plan prunes the journal
    assert not os.path.exists(os.path.join(h.state, "operations", op_of(first) + ".json"))
    assert h.run("note", "update", "2", "--title", "B", "--confirm", tok).exit == 0
    r = h.run("note", "update", "3", "--title", "C", "--dry-run")
    assert r.code() == "E_FORBIDDEN" and r.details()["scope"]["status"] == "budget_spent", r
    assert h.up.writes() == 2 and usage(h)["scopes"] == {"scp_test": 2}


def test_a4_batch_counts_each_dispatched_item(h: THarness) -> None:
    write_scope(h, limits={"max_operations": 3})
    h.up.hook = lambda kind, nid: (
        aicli.rejected(aicli.from_http(409, "The note is locked.")) if nid == "1" else None
    )
    args = ("note", "bulk-tag", "--ids", "1,2", "--tag", "x", "--continue-on-error=false")
    r = h.run(*args, "--confirm", h.must_token(*args))
    assert r.details()["summary"]["skipped"] == 1, r
    assert usage(h)["scopes"] == {"scp_test": 1}, "the skipped item gives its budget back"
    h.up.hook = None
    args = ("note", "bulk-tag", "--ids", "2,3", "--tag", "y")
    assert h.run(*args, "--confirm", h.must_token(*args)).exit == 0
    assert usage(h)["scopes"] == {"scp_test": 3}
    r = h.run("note", "update", "1", "--title", "Z", "--dry-run")
    assert r.code() == "E_FORBIDDEN" and r.details()["scope"]["status"] == "budget_spent", r


def lost_reply(h: THarness, *, sent: bool) -> Any:
    """A create hook: the request reaches the upstream (sent) or not, and the
    reply is lost either way."""

    def hook(kind: str, nid: str) -> Exception | None:
        if kind != "create":
            return None
        h.up.hook = None
        if sent:

            def do() -> None:
                n = str(h.up.next)
                h.up.next += 1
                h.up.notes[n] = TNote(n, "notes/" + n, "Hello", "", [], 1, "2026-10-12T00:00:00Z")

            h.up.write(do)
        return ConnectionResetError("connection reset")

    return hook


def test_a5_key_resolved_as_applied_is_not_resent(h: THarness) -> None:
    h.up.hook = lost_reply(h, sent=True)
    args = ("note", "create", "--title", "Hello", "--idempotency-key", "K1")
    later = h.must_token(*args)  # issued before the first confirm
    r = h.run(*args, "--confirm", h.must_token(*args))
    assert r.code() == "E_OUTCOME_UNKNOWN", r
    op = r.details()["operation_id"]
    res = h.run("operation", "resolve", "--id", op, "--outcome", "applied", "--evidence", "seen")
    assert res.exit == 0, res
    # at confirm, and at dry-run before Preview (P4)
    for r in (h.run(*args, "--confirm", later), h.run(*args, "--dry-run")):
        expect_conflict(r, "idempotency_resolved")
        assert r.env["error"]["message"] == (
            "A request with this idempotency key was already resolved by hand; nothing was sent."
        )
        assert r.details()["next_action"] == (
            f"Run `notes operation show --id {op}` to see what was recorded; if the change is "
            "missing, repeat the request with a new --idempotency-key."
        )
        assert r.details()["operation_id"] == op and h.up.writes() == 1, r


def test_a5_key_resolved_as_not_applied_may_be_sent_again(h: THarness) -> None:
    h.up.hook = lost_reply(h, sent=False)
    args = ("note", "create", "--title", "Hello", "--idempotency-key", "K2")
    r = h.run(*args, "--confirm", h.must_token(*args))
    op = r.details()["operation_id"]
    res = h.run("operation", "resolve", "--id", op, "--outcome", "not_applied", "--evidence", "no")
    assert res.exit == 0, res
    r = h.run(*args, "--confirm", h.must_token(*args))
    assert r.exit == 0 and h.up.writes() == 1, r


def test_a5_same_key_on_another_command_is_a_mismatch(h: THarness) -> None:
    h.build = keyed_app
    args = ("note", "create", "--title", "Hello", "--idempotency-key", "shared")
    first = h.run(*args, "--confirm", h.must_token(*args))
    assert first.exit == 0, first
    r = h.run("note", "comment", "add", "1", "--text", "Hi", "--idempotency-key", "shared")
    expect_conflict(r, "idempotency_mismatch")
    assert r.details()["operation_id"] == first.data()["operation_id"], r
    assert h.up.writes() == 1


def test_a5_replay_of_a_replay_returns_the_original(h: THarness) -> None:
    args = ("note", "create", "--title", "Once", "--idempotency-key", "R1")
    tokens = [h.must_token(*args) for _ in range(3)]  # issued before the first confirm
    first = h.run(*args, "--confirm", tokens[0])
    assert first.exit == 0, first
    # each later confirm is recorded as a replay; the last one meets a replay of a
    # replay; a dry-run afterwards is answered at once (P4)
    runs = [h.run(*args, "--confirm", t) for t in tokens[1:]] + [h.run(*args, "--dry-run")]
    for again in runs:
        assert again.exit == 0, again
        assert again.data()["operation_id"] == first.data()["operation_id"], again
        assert again.data()["result"] == first.data()["result"], again
        assert again.data()["receipt"]["replayed"] is True, again
    assert h.up.writes() == 1


def test_a5_repeated_batch_is_replayed(h: THarness) -> None:
    h.build = keyed_app
    args = ("note", "bulk-tag", "--ids", "1,2", "--tag", "k", "--idempotency-key", "B1")
    first = h.run(*args, "--confirm", h.must_token(*args))
    assert first.exit == 0 and h.up.writes() == 2, first
    # answered at dry-run, before any Preview (P4)
    again = h.run(*args, "--dry-run")
    assert again.exit == 0 and h.up.writes() == 2, again
    assert again.data()["operation_id"] == first.data()["operation_id"], again
    assert [it["operation_id"] for it in again.data()["items"]] == [
        it["operation_id"] for it in first.data()["items"]
    ]
    assert all(it["receipt"]["replayed"] is True for it in again.data()["items"]), again


def test_a5_key_of_a_partial_batch_is_resolved(h: THarness) -> None:
    h.build = keyed_app
    h.up.hook = lambda kind, nid: (
        aicli.rejected(aicli.from_http(409, "The note is locked.")) if nid == "2" else None
    )
    args = ("note", "bulk-tag", "--ids", "1,2", "--tag", "k", "--idempotency-key", "B2")
    first = h.run(*args, "--confirm", h.must_token(*args))
    assert first.code() == "E_PARTIAL" and h.up.writes() == 1, first
    h.up.hook = None
    r = h.run(*args, "--dry-run")
    expect_conflict(r, "idempotency_resolved")
    assert r.details()["operation_id"] == first.details()["operation_id"], r
    assert h.up.writes() == 1


def partial_touch_app(u: Upstream) -> App:
    """`note touch` with --idempotency-key, whose apply reports a partial change."""
    a = touch_app(u, outcome=aicli.Outcome(change="partial"))
    touch = a.root.commands["note"].commands["touch"]
    touch.params.append(click.Option(["--idempotency-key"], expose_value=False, help="Key"))
    return a


def test_a5_single_write_that_ended_partial_is_resolved(h: THarness) -> None:
    """A single write that ended partial answers its key with idempotency_resolved:
    sending it again could apply that part twice."""
    h.build = partial_touch_app
    args = ("note", "touch", "1", "--idempotency-key", "P1")
    first = h.run(*args, "--confirm", h.must_token(*args))
    assert first.code() == "E_PARTIAL" and h.up.writes() == 1, first
    r = h.run(*args, "--dry-run")
    expect_conflict(r, "idempotency_resolved")
    assert r.details()["operation_id"] == first.details()["operation_id"], r
    assert h.up.writes() == 1


def test_a5_batch_dry_run_refuses_a_mismatched_key(h: THarness) -> None:
    h.build = keyed_app
    args = ("note", "bulk-tag", "--ids", "1,2", "--tag", "k", "--idempotency-key", "B3")
    first = h.run(*args, "--confirm", h.must_token(*args))
    assert first.exit == 0, first
    r = h.run(
        "note", "bulk-tag", "--ids", "1,3", "--tag", "k", "--idempotency-key", "B3", "--dry-run"
    )
    expect_conflict(r, "idempotency_mismatch")
    assert r.details()["operation_id"] == first.data()["operation_id"], r
    assert h.up.writes() == 2


def test_a5_lookup_considers_every_earlier_record(h: THarness) -> None:
    """Every earlier operation with the key counts, not only the latest: one
    resolved by hand as applied wins over a later one that applied (unresolved
    first, then resolved, then replay)."""
    h.up.hook = lost_reply(h, sent=True)
    args = ("note", "create", "--title", "Hello", "--idempotency-key", "K3")
    op = h.run(*args, "--confirm", h.must_token(*args)).details()["operation_id"]
    res = h.run("operation", "resolve", "--id", op, "--outcome", "applied", "--evidence", "seen")
    assert res.exit == 0, res
    # a later record with the same key that applied: the latest alone would replay
    with open(os.path.join(h.state, "operations", op + ".json"), encoding="utf-8") as fh:
        rec = json.load(fh)
    later = "op_" + "z" * 26
    created = parse_time(rec["created_at"]) + _dt.timedelta(minutes=1)
    del rec["resolution"]
    rec.update(
        operation_id=later, status="applied", created_at=created.strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    with open(os.path.join(h.state, "operations", later + ".json"), "w", encoding="utf-8") as fh:
        json.dump(rec, fh)
    r = h.run(*args, "--dry-run")
    expect_conflict(r, "idempotency_resolved")
    assert r.details()["operation_id"] == op and h.up.writes() == 1, r


def test_a4_every_confirm_is_counted_without_a_limit(h: THarness) -> None:
    """scope-usage.json counts every confirm even when the scope sets no limit; a
    replay sends and counts nothing."""
    args = ("note", "create", "--title", "Count", "--idempotency-key", "C1")
    later = h.must_token(*args)  # issued before the first confirm
    assert h.run(*args, "--confirm", h.must_token(*args)).exit == 0
    assert usage(h) == {"v": 1, "scopes": {"scp_test": 1}}
    again = h.run(*args, "--confirm", later)
    assert again.exit == 0 and again.data()["receipt"]["replayed"] is True, again
    assert usage(h)["scopes"] == {"scp_test": 1} and h.up.writes() == 1


def test_a3_running_batch_blocks_its_targets(h: THarness) -> None:
    """A batch that is still dispatching blocks every target of its preview, the
    items not yet sent included; the conflict names the batch."""
    seen: dict[str, Any] = {}

    def hook(kind: str, nid: str) -> None:
        if nid == "1":
            seen["r"] = h.run("note", "update", "2", "--title", "Z", "--dry-run")
        return None

    h.up.hook = hook
    args = ("note", "bulk-tag", "--ids", "1,2", "--tag", "x")
    r = h.run(*args, "--confirm", h.must_token(*args))
    assert r.exit == 0, r
    expect_conflict(seen["r"], "unresolved_operation")
    assert seen["r"].details()["active_operation"] == r.data()["operation_id"], seen["r"]


def test_a6_resolution_while_apply_runs_is_kept(h: THarness) -> None:
    """The lease is the timeout plus 5 s; a record resolved while apply still ran
    keeps its resolution and the confirm reports E_OUTCOME_UNKNOWN."""
    clock = clocked(h)
    args = ("note", "update", "1", "--title", "Slow")
    tok = h.must_token(*args)
    op = op_of(tok)
    seen: dict[str, Any] = {}

    def hook(kind: str, nid: str) -> None:
        with open(os.path.join(h.state, "operations", op + ".json"), encoding="utf-8") as fh:
            rec = json.load(fh)
        seen["lease"] = parse_time(rec["lease_until"]) - parse_time(rec["dispatched_at"])
        clock[0] += _dt.timedelta(seconds=200)
        res = h.run(
            "operation", "resolve", "--id", op, "--outcome", "not_applied", "--evidence", "x"
        )
        seen["resolve"] = res.exit
        return None

    h.up.hook = hook
    r = h.run(*args, "--confirm", tok)
    assert seen["lease"] == _dt.timedelta(seconds=125) and seen["resolve"] == 0, seen
    assert r.code() == "E_OUTCOME_UNKNOWN", r
    assert r.env["error"]["message"] == (
        "The operation was resolved by hand while it was still running; its outcome is unknown."
    )
    assert r.details()["next_action"] == (
        f"Run `notes operation show --id {op}` and re-read the target before anything else; do "
        "not resend."
    )
    assert r.details()["operation_id"] == op and op in r.details()["lookup"], r
    show = h.run("operation", "show", "--id", op).data()
    assert show["status"] == "resolved" and show["resolution"]["outcome"] == "not_applied", show


def test_a7_batch_items_are_pruned_with_their_batch(h: THarness) -> None:
    clock = clocked(h)
    t0 = clock[0]

    def hook(kind: str, nid: str) -> None:
        clock[0] += _dt.timedelta(hours=1)
        return None

    write_scope(h, expires_at=None)
    h.up.hook = hook
    args = ("note", "bulk-tag", "--ids", "1,2", "--tag", "w")
    r = h.run(*args, "--confirm", h.must_token(*args))
    assert r.exit == 0, r
    batch = r.data()["operation_id"]
    h.up.hook = None
    # item 1 finished an hour before the batch: it stays while the batch stays
    clock[0] = t0 + _dt.timedelta(days=30, minutes=90)
    h.must_token("note", "update", "3", "--title", "Z")  # the plan prunes the journal
    items = h.run("operation", "show", "--id", batch).data()["items"]
    assert [it["status"] for it in items] == ["applied", "applied"], items
    clock[0] = t0 + _dt.timedelta(days=31)
    h.must_token("note", "update", "3", "--title", "Z")
    assert h.run("operation", "show", "--id", batch).code() == "E_NOT_FOUND"
    for it in items:
        assert not os.path.exists(os.path.join(h.state, "operations", it["operation_id"] + ".json"))


def test_a8_record_without_required_keys_is_unreadable(h: THarness) -> None:
    for key, value in (("command", None), ("effects", None), ("status", 5)):
        h.up.reset()
        op = h.run("note", "update", "1", "--title", "X", "--dry-run").data()["operation_id"]
        path = os.path.join(h.state, "operations", op + ".json")
        with open(path, encoding="utf-8") as fh:
            obj = json.load(fh)
        if value is None:
            del obj[key]
        else:
            obj[key] = value
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        blocked = h.run("note", "update", "2", "--title", "Y", "--dry-run")
        expect_conflict(blocked, "unresolved_operation")
        assert blocked.details()["active_operation"] == op, (key, blocked)
        show = h.run("operation", "show", "--id", op).data()
        assert show["status"] == "unknown" and show["command"] == "unknown", (key, show)
        os.remove(path)


# ---------------------------------------------------------------- B. binding and secrets


def test_b1_profile_and_settings_are_bound(h: THarness) -> None:
    h.build = settings_app
    settings_file(
        h,
        {
            "profiles": {
                "work": {"host": "https://notes.example"},
                "home": {"host": "https://notes.example", "region": "us"},
            }
        },
    )
    h.env["NOTES_PROFILE"] = "work"
    h.env["NOTES_API_TOKEN"] = "api-token-from-env"
    args = ("note", "update", "1", "--title", "X")
    dry = h.run(*args, "--dry-run")
    inputs = dry.data()["preview"]["inputs"]
    assert inputs["profile"] == "work", inputs
    assert inputs["setting"] == {
        "host": "https://notes.example",
        "region": "eu",
        "page_size": "20",
        "api_token": "***",
    }, inputs
    assert "api-token-from-env" not in journal_text(h) + dry.stdout
    h.env["NOTES_PROFILE"] = "home"
    expect_mismatch(h.run(*args, "--confirm", dry.token()), "profile")
    h.env["NOTES_PROFILE"] = "work"
    h.env["NOTES_REGION"] = "ap"
    expect_mismatch(h.run(*args, "--confirm", dry.token()), "setting.region")
    del h.env["NOTES_REGION"]
    h.env["NOTES_API_TOKEN"] = "another-token"
    expect_mismatch(h.run(*args, "--confirm", dry.token()), "setting.api_token")
    h.env["NOTES_API_TOKEN"] = "api-token-from-env"
    assert h.run(*args, "--confirm", dry.token()).exit == 0


def test_b2_secret_twins_and_secret_setting_flags(h: THarness) -> None:
    def build(u: Upstream) -> App:
        a = notes_app(u)
        a.settings = [Setting("api_token", flag="token", secret=True)]
        upd = a.root.commands["note"].commands["update"]
        upd.params.append(click.Option(["--token"], expose_value=False, help="API token"))
        create = a.root.commands["note"].commands["create"]
        a.register(create, dataclasses.replace(a._reg.meta[create], secrets=["body-file"]))
        return a

    h.build = build
    r = h.run("note", "update", "1", "--title", "X", "--token", "SETTINGFLAGSECRET", "--dry-run")
    assert r.exit == 0 and r.data()["preview"]["inputs"]["token"] == "***", r
    assert "SETTINGFLAGSECRET" not in r.stdout + journal_text(h)
    params = h.run("reference", "--command", "note update").data()["command"]["params"]
    assert next(p for p in params if p["name"] == "token").get("secret") is True, params
    # the literal twin of a secret file flag is secret too: shown as ***, bound by a
    # keyed digest (the tool's own preview shows what it chooses to)
    r = h.run("note", "create", "--title", "T", "--body", "LITERALTWINSECRET", "--dry-run")
    assert r.exit == 0 and r.data()["preview"]["inputs"]["body"] == "***", r
    path = os.path.join(h.state, "operations", r.data()["operation_id"] + ".json")
    with open(path, encoding="utf-8") as fh:
        claims = json.load(fh)["claims"]["inputs"]
    plain = "sha256:" + hashlib.sha256(b"LITERALTWINSECRET").hexdigest()
    unkeyed = "sha256:" + hashlib.sha256(plain.encode("utf-8")).hexdigest()
    assert claims["body"] != unkeyed and "LITERALTWINSECRET" not in json.dumps(claims), claims
    params = h.run("reference", "--command", "note create").data()["command"]["params"]
    assert next(p for p in params if p["name"] == "body").get("secret") is True, params


def test_b3_click_file_on_a_write_is_a_tree_defect() -> None:
    a = notes_app(Upstream())
    note = a.root.commands["note"]

    @note.command("set-body")
    @click.option("--id", "nid", required=True, help="Note id")
    @click.option("--body-file", type=click.File("r"), required=True, help="Body")
    @click.option("--from", "src", type=click.Path(exists=True, dir_okay=False), help="Body")
    def set_body(nid: str, body_file: Any, src: Any) -> None:
        pass

    a.register(
        set_body,
        Meta(
            summary="Set a body",
            effects=[CHANGE],
            output="note_write",
            positional="id",
            examples=["notes note set-body 1 --dry-run", "notes note set-body 1 --confirm <t>"],
        ),
    )
    a.install_builtins()
    problems = a.check_tree()
    assert (
        '"note set-body": --body-file is a click.File the tool opens itself, so the write gate '
        "cannot bind its content; declare it in Meta.file_inputs and read it with "
        "App.read_file_input"
    ) in problems, problems
    assert any("--from is a click.Path of an existing file" in p for p in problems), problems


def test_b4_shared_bound_names_are_tree_defects() -> None:
    a = notes_app(Upstream())
    create = a.root.commands["note"].commands["create"]
    create.params.append(click.Option(["--title-file"], help="Title from a file"))
    create.params.append(click.Option(["--tags-file"], help="Tags from a file"))
    files = {"body-file": "body", "title-file": "body", "tags-file": "tag"}
    a.register(create, dataclasses.replace(a._reg.meta[create], file_inputs=files))
    a.install_builtins()
    problems = a.check_tree()
    assert (
        '"note create": Meta.file_inputs maps --body-file and --title-file to the same name "body"'
    ) in problems, problems
    assert (
        '"note create": Meta.file_inputs maps --tags-file onto --tag, which is not a plain value '
        "flag the file can stand in for"
    ) in problems, problems


# ---------------------------------------------------------------- C. scope and local files


def export_grant(h: THarness, **kw: Any) -> None:
    h.grant({"effects": ["artifact", "persist"], "destinations": None, **kw})


def test_c1_overwrite_needs_roots(h: THarness, tmp_path: Any) -> None:
    outdir = tmp_path / "reports"
    outdir.mkdir()
    victim = outdir / "q3-final.tsv"
    victim.write_text("hand-edited, keep\n", encoding="utf-8")
    export_grant(h, outputs=[str(outdir)])
    r = h.run("note", "export", "--output", str(victim), "--dry-run")
    assert r.code() == "E_FORBIDDEN", r
    scope = r.details()["scope"]
    assert scope["status"] == "insufficient" and scope["missing"]["roots"] == [str(victim)], scope
    assert "outputs" not in scope["missing"], scope
    assert scope["grant_example"]["roots"] == [str(outdir)], scope
    assert victim.read_text(encoding="utf-8") == "hand-edited, keep\n"
    export_grant(h, roots=[str(outdir)])
    tok = h.must_token("note", "export", "--output", str(victim))
    r = h.run("note", "export", "--output", str(victim), "--confirm", tok)
    assert r.exit == 0 and "hand-edited" not in victim.read_text(encoding="utf-8"), r


def test_c1_new_file_needs_outputs(h: THarness, tmp_path: Any) -> None:
    cfg = tmp_path / "config"
    cfg.mkdir()
    planted = cfg / "planted.tsv"
    export_grant(h, roots=[str(cfg)])
    r = h.run("note", "export", "--output", str(planted), "--dry-run")
    scope = r.details()["scope"]
    assert r.code() == "E_FORBIDDEN" and scope["missing"]["outputs"] == [str(planted)], r
    assert scope["grant_example"]["outputs"] == [str(cfg)], scope
    assert not planted.exists()


def test_c1_file_that_appears_before_the_write_is_refused(h: THarness, tmp_path: Any) -> None:
    outdir = tmp_path / "out"
    outdir.mkdir()
    late = outdir / "late.tsv"
    export_grant(h, outputs=[str(outdir)])
    tok = h.must_token("note", "export", "--output", str(late))

    def hook(kind: str, nid: str) -> None:
        if kind == "export":
            late.write_text("someone else's file", encoding="utf-8")
        return None

    h.up.hook = hook
    r = h.run("note", "export", "--output", str(late), "--confirm", tok)
    assert r.code() == "E_FORBIDDEN" and r.details()["dispatch_state"] == "not_started", r
    assert r.env["error"]["message"] == (
        "The output file already exists and the scope does not allow overwriting it; nothing "
        "was written."
    )
    assert r.details()["next_action"] == (
        "Ask the user to add the directory to the scope's roots, or choose a new file name."
    )
    assert r.details()["scope"]["missing"]["roots"] == [str(late)], r
    assert late.read_text(encoding="utf-8") == "someone else's file"


def test_c1_file_that_changes_before_the_write_is_drift(h: THarness, tmp_path: Any) -> None:
    """An existing file whose content changed after the confirm's preview is not
    overwritten: state_drift, nothing written."""
    outdir = tmp_path / "out"
    outdir.mkdir()
    out = outdir / "notes.tsv"
    out.write_text("v1\n", encoding="utf-8")
    export_grant(h, roots=[str(outdir)])
    tok = h.must_token("note", "export", "--output", str(out))

    def hook(kind: str, nid: str) -> None:
        if kind == "export":
            out.write_text("v2 by someone else\n", encoding="utf-8")
        return None

    h.up.hook = hook
    r = h.run("note", "export", "--output", str(out), "--confirm", tok)
    expect_conflict(r, "state_drift")
    assert r.details()["dispatch_state"] == "not_started", r
    assert out.read_text(encoding="utf-8") == "v2 by someone else\n"


def _link_dir(target: Any, link: Any) -> None:
    try:
        os.symlink(str(target), str(link), target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        if not WINDOWS:
            pytest.skip("cannot create a symbolic link here")
    try:
        import _winapi  # type: ignore[import-not-found]

        _winapi.CreateJunction(str(target), str(link))
    except (ImportError, OSError) as exc:
        pytest.skip(f"cannot create a junction here: {exc}")


def test_c2_link_that_cannot_be_resolved_is_refused(
    h: THarness, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    _link_dir(real, link)
    export_grant(h, outputs=[str(link)])
    monkeypatch.setattr(os.path, "realpath", lambda p, **_kw: p)  # resolution fails
    r = h.run("note", "export", "--output", str(link / "a.tsv"), "--dry-run")
    assert r.code() == "E_FORBIDDEN" and r.details()["scope"]["status"] == "invalid", r
    assert f"a junction or other reparse point is in the path {link}" in r.env["error"]["message"]
    # an output path through the link, under a root without one
    export_grant(h, outputs=[str(real)])
    out = str(link / "a.tsv")
    r = h.run("note", "export", "--output", out, "--dry-run")
    assert r.code() == "E_FORBIDDEN", r
    scope = r.details()["scope"]
    assert scope["status"] == "insufficient" and scope["missing"]["outputs"] == [out], scope
    assert "grant_example" not in scope, scope
    assert r.env["error"]["message"] == (
        "The output path cannot be checked against the scope (a junction or other reparse point "
        f"is in the path {out}), so nothing was sent."
    )
    assert r.details()["next_action"] == (
        "Write to a path without junctions or other reparse points; do not change the scope to "
        "work around it."
    )


def test_c4_relative_scope_and_settings_paths_are_invalid(h: THarness) -> None:
    h.scope = "relative-scope.json"
    r = h.run("note", "update", "1", "--title", "X", "--dry-run")
    assert r.code() == "E_FORBIDDEN" and r.details()["scope"]["status"] == "invalid", r
    assert "NOTES_SCOPE_FILE must be an absolute path" in r.env["error"]["message"], r
    h.build = settings_app
    h.env["NOTES_SETTINGS_FILE"] = "settings.json"
    r = h.run("probe", "--name", "host")
    assert r.code() == "E_CONFIG", r
    assert r.env["error"]["message"] == (
        "The settings file settings.json is invalid: NOTES_SETTINGS_FILE must be an absolute path."
    ), r


@pytest.mark.skipif(WINDOWS, reason="POSIX file modes and owners")
def test_c4_files_others_can_write_are_refused(h: THarness) -> None:
    os.chmod(h.scope, 0o664)
    r = h.run("note", "update", "1", "--title", "X", "--dry-run")
    assert r.code() == "E_FORBIDDEN" and r.details()["scope"]["status"] == "invalid", r
    assert (
        f"{h.scope} is writable by other users; only its owner may write it"
        in r.env["error"]["message"]
    ), r
    os.chmod(h.scope, 0o600)
    assert h.run("note", "update", "1", "--title", "X", "--dry-run").exit == 0
    h.build = settings_app
    path = settings_file(h, {"profiles": {"work": {}}})
    os.chmod(path, 0o646)
    r = h.run("probe", "--name", "host")
    assert r.code() == "E_CONFIG" and "is writable by other users" in r.env["error"]["message"], r


# ---------------------------------------------------------------- D. settings, debug, redaction


def leak_app(u: Upstream) -> App:
    """The test tool with `ping`, which fails with every secret in its error text."""
    a = notes_app(u)
    a.settings = [Setting("hook_secret", secret=True)]
    a.schema("ping_result", SchemaDef(shape="object"))

    @a.root.command("ping")
    @click.option("--token", help="Access token")
    @click.option("--keyring", help="A value to register as a secret")
    def ping(token: Any, keyring: Any) -> None:
        sec, _ = a.setting("hook_secret")
        a.add_secret(keyring)
        a.add_secret("abc")  # shorter than 4 characters: ignored
        raise RuntimeError(
            f"GET https://h.example/hook/{sec}?private_token={urllib.parse.quote_plus(token)} "
            f"({token}) failed; keyring {keyring} {urllib.parse.quote(keyring)}; abc"
        )

    a.register(
        ping,
        Meta(
            summary="Ping",
            effects=[OBSERVE],
            output="ping_result",
            secrets=["token"],
            examples=["notes ping"],
        ),
    )
    return a


def test_d1_errors_are_redacted(h: THarness) -> None:
    h.build = leak_app
    h.env["NOTES_HOOK_SECRET"] = "HOOKSECRET123"
    r = h.run("ping", "--token", "glpat/TOKEN+SECRET", "--keyring", "keyring value/1")
    assert r.code() == "E_UNKNOWN", r
    for secret in (
        "glpat/TOKEN+SECRET",
        "glpat%2FTOKEN%2BSECRET",
        "HOOKSECRET123",
        "keyring value/1",
        "keyring%20value/1",
    ):
        assert secret not in r.stdout, (secret, r.stdout)
    cause = r.details()["cause"]
    assert "***" in cause and cause.endswith("; abc"), cause


def test_d1_structural_keys_are_not_redacted(h: THarness) -> None:
    """The values of the library's structural detail keys (ids, tokens, enum values)
    are kept even when a secret happens to match them; the rest is redacted."""

    def build(u: Upstream) -> App:
        a = notes_app(u)
        a.schema("ping_result", SchemaDef(shape="object"))

        @a.root.command("ping")
        @click.option("--token", help="Access token")
        def ping(token: Any) -> None:
            raise aicli.new_error(
                "E_CONFLICT",
                f"The upstream refused {token}.",
                {
                    "conflict": "state_drift",
                    "dispatch_state": "not_started",
                    "next_action": f"Do not send {token} again.",
                },
            )

        a.register(
            ping,
            Meta(
                summary="Ping",
                effects=[OBSERVE],
                output="ping_result",
                secrets=["token"],
                examples=["notes ping"],
            ),
        )
        return a

    h.build = build
    r = h.run("ping", "--token", "state_drift")
    assert r.details()["conflict"] == "state_drift", r
    assert r.details()["dispatch_state"] == "not_started", r
    assert r.env["error"]["message"] == "The upstream refused ***.", r
    assert r.details()["next_action"] == "Do not send *** again.", r


def test_d3_profile_accessor(h: THarness) -> None:
    def build(u: Upstream) -> App:
        a = settings_app(u)

        @a.root.command("whoami")
        def whoami() -> None:
            name, source = a.profile()
            a.ok(None, {"name": name, "value": a.settings_path(), "source": source})

        a.register(
            whoami,
            Meta(
                summary="Who", effects=[OBSERVE], output="setting_value", examples=["notes whoami"]
            ),
        )
        return a

    h.build = build
    assert h.run("whoami").data()["name"] == "" and h.run("whoami").data()["source"] == "none"
    path = settings_file(h, {"profiles": {"work": {}, "home": {}}})
    h.env["NOTES_PROFILE"] = "home"
    assert h.run("whoami").data() == {"name": "home", "value": path, "source": "env"}


def test_d4_unknown_profile_stops_tool_commands_only(h: THarness) -> None:
    h.build = settings_app
    path = settings_file(h, {"profiles": {"work": {}}})
    h.env["NOTES_PROFILE"] = "nope"
    r = h.run("note", "list")
    assert r.code() == "E_CONFIG" and r.details()["source"] == "env", r
    assert r.details()["did_you_mean"] == ["work"], "no close name: the file's first names"
    for builtin in (["context"], ["doctor"], ["reference", "--index"], ["changelog"]):
        assert h.run(*builtin).exit == 0, builtin
    assert h.run("operation", "list").exit == 0
    os.remove(path)
    r = h.run("note", "list")
    assert r.env["error"]["message"] == (
        "The requested profile does not exist: there is no settings file."
    ), r
    assert r.details()["next_action"] == (
        f"Create {path} with this profile, or run without --profile."
    ), r
    del h.env["NOTES_PROFILE"]
    r = h.run("note", "list", "--profile", "nope")
    assert r.code() == "E_CONFIG" and r.details()["source"] == "flag", r
    assert r.details()["profile"] == "nope" and "nope" not in r.env["error"]["message"], r


def login_app(u: Upstream) -> App:
    a = notes_app(u)
    a.schema("login_result", SchemaDef(shape="object"))

    @a.root.command("login")
    @click.option("--token", "-k", help="Access token")
    @click.option("--host", help="Host")
    def login(token: Any, host: Any) -> None:
        a.ok(None, {})

    a.register(
        login,
        Meta(
            summary="Log in",
            effects=[OBSERVE],
            output="login_result",
            secrets=["token"],
            examples=["notes login"],
        ),
    )
    return a


def test_d6_suggestions_never_echo_secrets(h: THarness) -> None:
    h.build = login_app
    cases = [
        (["-k", "SECRET0001"], "SECRET0001"),
        (["-k=SECRET0002"], "SECRET0002"),
        (["-kSECRET0003"], "SECRET0003"),
        (["--token", "-DASHSECRET4"], "-DASHSECRET4"),
        (["--token", "SECRET0005"], "SECRET0005"),
        (["--token=SECRET0006"], "SECRET0006"),
    ]
    for given, secret in cases:
        # before the mistake (parsed, also redacted) and after it (never parsed)
        for args in (["login", *given, "--hostt", "x"], ["login", "--hostt", "x", *given]):
            r = h.run(*args)
            assert r.code() == "E_USAGE", (args, r)
            assert "--host" in r.details()["next_action"], (args, r.details())
            assert secret not in r.stdout, (args, r.stdout)


def auth_app(u: Upstream) -> App:
    a = notes_app(u)
    a.schema("auth_result", SchemaDef(shape="object"))

    @a.root.command("auth")
    @click.option("--token", help="Access token")
    @click.option("--token-file", help="Read the token from a file")
    def auth(token: Any, token_file: Any) -> None:
        content = a.read_file_input(None, "token-file")
        a.debug(f"token from file: {content.decode() if content else token}")
        a.debug("line one\nline two\rline three")
        a.ok(None, {})

    a.register(
        auth,
        Meta(
            summary="Auth",
            effects=[OBSERVE],
            output="auth_result",
            secrets=["token"],
            file_inputs={"token-file": "token"},
            examples=["notes auth"],
        ),
    )
    return a


def test_d8_debug_redacts_secret_file_inputs_and_d11_escapes_lines(h: THarness) -> None:
    h.build = auth_app
    p = os.path.join(h.dir, "tok.txt")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("FILESECRET999\n")
    h.env["NOTES_DEBUG"] = "1"
    r = h.run("auth", "--token-file", p)
    assert r.exit == 0 and "FILESECRET999" not in r.stderr, r.stderr
    assert "debug: token from file: ***" in r.stderr, r.stderr
    assert "debug: line one\\nline two\\rline three\n" in r.stderr, r.stderr
    assert all(line.startswith("debug: ") for line in r.stderr.splitlines()), r.stderr


def test_d9_text_mode_never_raises_on_encoding(h: THarness) -> None:
    h.build = settings_app
    settings_file(h, b'{"profiles": {"work": {"\\ud800": 1}}}')
    r = h.run("doctor", "--format", "text")
    assert r.exit == 0 and "settings" in r.stdout, r
    r = h.run("probe", "--name", "host", "--format", "text")
    assert r.exit == 4 and "error E_CONFIG" in r.stderr, r


def test_d10_parity_details(h: THarness) -> None:
    h.build = settings_app

    def settings_check() -> dict[str, Any]:
        return next(c for c in h.run("doctor").data()["checks"] if c["check"] == "settings")

    settings_file(h, {"default_profile": "work", "profiles": {"work": {}, "home": {}}})
    h.env["NOTES_PROFILE"] = "home"
    ctx = h.run("context", "--profile", "").data()["profile"]
    assert ctx["name"] == "home" and ctx["source"] == "env", "--profile '' means not given"
    del h.env["NOTES_PROFILE"]
    path = settings_file(h, {"default_profile": "", "profiles": {"work": {}}})
    assert "default_profile `` is not one of its profiles" in settings_check()["message"]
    for v, on in ((" ", False), (" 0 ", False), ("1", True)):
        h.env["NOTES_DEBUG"] = v
        assert ("debug:" in h.run("context").stderr) == on, repr(v)
    del h.env["NOTES_DEBUG"]
    settings_file(h, '{"profiles": {"work": {"' + "k" * 100 + '": 1}}}')
    msg = settings_check()["message"]
    assert "`" + "k" * 40 + "…`" in msg and "k" * 41 not in msg, msg
    settings_file(h, '{"zzz": 1} {}')
    assert "it holds more than one JSON value" in settings_check()["message"]
    d = os.path.join(h.dir, "settingsdir")
    os.makedirs(d)
    h.env["NOTES_SETTINGS_FILE"] = d
    assert "it is not a regular file" in settings_check()["message"]
    link = os.path.join(h.dir, "settings-link.json")
    try:
        os.symlink(path, link)
    except (OSError, NotImplementedError):
        pass
    else:
        h.env["NOTES_SETTINGS_FILE"] = link
        assert "it must be a regular file, not a link" in settings_check()["message"]


def test_d10_deprecation_text_is_trimmed_and_noticed_after_positional_binding(
    h: THarness,
) -> None:
    def build(u: Upstream) -> App:
        a = notes_app(u)
        get = a.root.commands["note"].commands["get"]
        meta = dataclasses.replace(
            a._reg.meta[get], deprecated="  use `note view`.  ", deprecated_flags={"id": " x. "}
        )
        a.register(get, meta)
        return a

    h.build = build
    notices = [n["message"] for n in h.run("note", "get", "1").env["meta"]["notices"]]
    assert notices == [
        "`note get` is deprecated: use `note view`. It will be removed in notes 2.0.0.",
        "`--id` is deprecated: x.",
    ], notices


def test_d11_redact_host_without_a_scheme() -> None:
    assert redact_host("user:tok@host.example/api") == "host.example/api"
    assert redact_host("https://u:p@h.example/x?y=1#z") == "https://h.example/x"
    assert redact_host("git@github.com:org/repo") == "github.com:org/repo"


# ---------------------------------------------------------------- F. built-ins


def test_f4_operation_list_filters_with_state(h: THarness) -> None:
    page = h.run("operation", "list", "--state", "all")
    assert page.exit == 0 and page.data()["scope"]["state"] == "all", page
    assert "status" not in page.data()["scope"], page
    r = h.run("operation", "list", "--status", "unknown")
    assert r.details()["problem"] == "unknown_flag" and "--state" in r.details()["did_you_mean"], r
    assert r.details()["next_action"] == "Run `notes operation list --state unknown`.", r
    cmd = h.run("reference", "--command", "operation list").data()["command"]
    assert "state" in [p["name"] for p in cmd["params"]], cmd["params"]
    assert cmd["examples"] == ["notes operation list --state unknown"], cmd


def test_f10_doctor_points_a_missing_skill_at_the_readme(h: THarness) -> None:
    check = next(c for c in h.run("doctor").data()["checks"] if c["check"] == "version")
    assert check["status"] == "warn", check
    assert check["fix"] == (
        "Install the Skill with the install command in the tool's README; update does not "
        "install a missing Skill."
    ), check
