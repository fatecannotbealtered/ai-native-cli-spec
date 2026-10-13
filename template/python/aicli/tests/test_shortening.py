"""Size budgets for the library's own output (CLI-042, CLI-063): a batch preview
keeps its first targets and items and counts the rest in omitted; a batch result
drops the receipts of items that succeeded, then keeps failures first and counts
the rest in omitted. Every envelope here is also checked by validate_envelope."""

from __future__ import annotations

from aicli_testapp import THarness, TNote

import aicli
from aicli._util import Ordered
from aicli.contract_gen import ERROR_BUDGET_CHARS, SUCCESS_BUDGET_CHARS
from aicli.envelope import error_size, fit_error_budget, shortened_note


def seed(h: THarness, n: int) -> list[str]:
    ids = []
    with h.up.mu:
        for i in range(n):
            nid = str(1000 + i)
            h.up.notes[nid] = TNote(
                nid, "notes/" + nid, "t" + nid, "", [], 1, "2026-10-01T00:00:00Z"
            )
            ids.append(nid)
    return ids


def test_large_batch_refusal_counts_left_out_targets(h: THarness) -> None:
    ids = seed(h, 60)
    r = h.run("note", "bulk-tag", "--ids", ",".join(ids), "--tag", "x")
    assert r.exit == 5 and len(r.stdout.strip()) <= ERROR_BUDGET_CHARS, (len(r.stdout), r.exit)
    p = r.details()["preview"]
    assert p["total"] == 60 and len(p["targets"]) == len(p["items"]) >= 1, p
    assert p["total"] == len(p["targets"]) + p["omitted"], p
    assert [t["key"] for t in p["targets"]] == ["notes/" + i for i in ids[: len(p["targets"])]]
    assert "shortened" in r.details()["next_action"]
    # the token still covers every target
    c = h.run("note", "bulk-tag", "--ids", ",".join(ids), "--tag", "x", "--confirm", r.token())
    assert c.exit == 0 and h.up.writes() == 60, c


def test_large_batch_failure_lists_failures_first(h: THarness) -> None:
    ids = seed(h, 40)
    tok = h.must_token("note", "bulk-tag", "--ids", ",".join(ids), "--tag", "x")
    failing = {ids[5], ids[17], ids[33]}
    h.up.hook = lambda kind, nid: (
        aicli.rejected(aicli.from_http(422, "Bad tag.")) if nid in failing else None
    )
    r = h.run("note", "bulk-tag", "--ids", ",".join(ids), "--tag", "x", "--confirm", tok)
    assert (
        r.exit == 1 and r.code() == "E_PARTIAL" and len(r.stdout.strip()) <= ERROR_BUDGET_CHARS
    ), r
    d = r.details()
    assert d["summary"] == {
        "total": 40,
        "succeeded": 37,
        "failed": 3,
        "unknown": 0,
        "skipped": 0,
    }, d["summary"]
    items = d["items"]
    assert d["summary"]["total"] == len(items) + d.get("omitted", 0), d
    oks = [it["ok"] for it in items]
    assert oks == sorted(oks), "items with ok false come first"
    listed_failures = [it["target"] for it in items if not it["ok"]]
    if any(oks):
        assert sorted(listed_failures) == sorted("notes/" + i for i in failing), listed_failures
    for it in items:
        if it["ok"]:
            assert "receipt" not in it and "result" not in it, it


def test_large_dry_run_fits_the_success_budget(h: THarness) -> None:
    ids = seed(h, 300)
    r = h.run("note", "bulk-tag", "--ids", ",".join(ids), "--tag", "x", "--dry-run")
    assert r.exit == 0 and len(r.stdout.strip()) <= SUCCESS_BUDGET_CHARS, len(r.stdout)
    p = r.data()["preview"]
    assert (
        p["total"] == 300
        and p["omitted"] == 300 - len(p["targets"])
        and len(p["items"]) == len(p["targets"])
    ), p
    c = h.run("note", "bulk-tag", "--ids", ",".join(ids), "--tag", "x", "--confirm", r.token())
    assert c.exit == 0 and len(c.stdout.strip()) <= SUCCESS_BUDGET_CHARS, len(c.stdout)
    d = c.data()
    assert d["summary"]["total"] == d["summary"]["succeeded"] == 300 and d["omitted"] == 300 - len(
        d["items"]
    ), d["summary"]
    assert all(it["ok"] and "receipt" not in it for it in d["items"])


def test_operation_show_cuts_the_recorded_preview(h: THarness) -> None:
    ids = seed(h, 45)
    r = h.run("note", "bulk-tag", "--ids", ",".join(ids), "--tag", "x", "--dry-run")
    show = h.run("operation", "show", r.data()["operation_id"])
    assert show.exit == 0 and len(show.stdout.strip()) <= SUCCESS_BUDGET_CHARS, len(show.stdout)
    d = show.data()
    assert len(d["items"]) == 45, "the record lists every item"
    assert d["preview"]["total"] == 45 and d["preview"].get("omitted", 0) == 45 - len(
        d["preview"]["targets"]
    ), d["preview"]


def test_small_outputs_are_not_cut(h: THarness) -> None:
    r = h.run("note", "bulk-tag", "--ids", "1,2", "--tag", "x", "--dry-run")
    assert "omitted" not in r.data()["preview"], r
    c = h.run("note", "bulk-tag", "--ids", "1,2", "--tag", "x", "--confirm", r.token())
    assert "omitted" not in c.data() and all("receipt" in it for it in c.data()["items"]), c


def test_error_budget_never_leaves_a_token_without_its_preview() -> None:
    """When an error must drop its preview to fit the budget, a token for that preview
    goes too: the agent never gets a token for a plan it cannot see."""
    tok = "ct2_aaaaaaaaaaaaaaaaaaaaaaaaaa_00000000000000000000000000000000"
    preview = {
        "operation_id": "op_aaaaaaaaaaaaaaaaaaaaaaaaaa",
        "command": "note update",
        "action": "Update note 1",
        "effects": ["change"],
        "dangerous": False,
        "inputs": {"title": "t" * 500},
        "can_apply": True,
        "blocked_by": [],
        "target": {"key": "notes/1", "kind": "note"},
        "changes": [{"field": "title", "before": "b" * 500, "after": "a" * 500}],
    }
    details = {
        "conflict": "state_drift",
        "dispatch_state": "not_started",
        "next_action": "details.preview is a new plan: if it still matches what the user "
        "asked for, run the same command again with --confirm " + tok + ".",
        "confirm_token": tok,
        "expires_at": "2026-10-12T00:15:00Z",
        "operation_id": "op_aaaaaaaaaaaaaaaaaaaaaaaaaa",
        "preview": preview,
        # A long field that is never shortened forces the last stage.
        "lookup": "l" * 1500,
    }
    meta = Ordered(duration_ms=1)
    obj = Ordered(
        code="E_CONFLICT",
        message="The target changed since the preview; nothing was sent.",
        details=details,
    )
    out = fit_error_budget(obj, meta, "notes")
    assert error_size(out, meta) <= ERROR_BUDGET_CHARS
    d = out["details"]
    assert "preview" not in d, "the test should reach the last stage"
    assert "confirm_token" not in d and "expires_at" not in d, d
    assert "--dry-run" in d["next_action"], d["next_action"]
    # The same refusal that fits keeps both.
    small = Ordered(code=obj["code"], message=obj["message"], details=dict(details))
    del small["details"]["lookup"]
    out = fit_error_budget(small, meta, "notes")
    assert "confirm_token" in out["details"] and "preview" in out["details"], out


def test_shortened_note_names_where_the_full_details_are() -> None:
    """The items of a batch come from operation list --batch, a single operation
    from operation show."""
    batch = "opb_aaaaaaaaaaaaaaaaaaaaaaaaaa"
    cases = [
        (
            {"code": "E_CONFIRMATION_REQUIRED", "details": {"preview": {}}},
            "notes",
            "--dry-run returns the full preview",
        ),
        (
            {"code": "E_PARTIAL", "details": {"items": [], "operation_id": batch}},
            "notes",
            "(Details were shortened to fit; `notes operation list --batch " + batch + "` lists "
            "every item.)",
        ),
        (
            {"code": "E_PARTIAL", "details": {"items": [], "operation_id": batch}},
            "",
            "(Details were shortened to fit; `operation list --batch " + batch + "` lists every "
            "item.)",
        ),
        (
            {"code": "E_PARTIAL", "details": {"items": []}},
            "notes",
            "`operation show --id` returns them in full",
        ),
        (
            {
                "code": "E_VERIFY_FAILED",
                "details": {"receipt": {}, "operation_id": "op_aaaaaaaaaaaaaaaaaaaaaaaaaa"},
            },
            "notes",
            "`operation show --id` returns them in full",
        ),
        (
            {"code": "E_FORBIDDEN", "details": {"scope": {}}},
            "notes",
            "(Details were shortened to fit.)",
        ),
    ]
    for obj, tool, want in cases:
        assert want in shortened_note(Ordered(obj), tool), obj["code"]
