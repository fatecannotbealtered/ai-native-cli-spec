"""Batch writes through the gate (port of batch_test.go, plus decision 31)."""

from __future__ import annotations

import hashlib
import os
from typing import Any

import click
from aicli_testapp import THarness, TNote, Upstream, expect_conflict, notes_app

import aicli
from aicli import BatchItem, BatchSpec, FieldChange, Op, Preview, Result, Target
from aicli.conformance import _is_token as is_token
from aicli.contract_gen import ERROR_BUDGET_CHARS, SUCCESS_BUDGET_CHARS
from aicli.journal import Journal
from aicli.token import valid_operation_id


def summary_of(m: dict[str, Any]) -> dict[str, int]:
    s = m.get("summary")
    assert isinstance(s, dict), m
    for k, v in s.items():
        assert isinstance(v, int) and not isinstance(v, bool), f"summary.{k} is not a number: {v!r}"
    return s


def test_batch_all_succeed(h: THarness) -> None:
    dry = h.run("note", "bulk-tag", "--ids", "1,2,2,3", "--tag", "work", "--dry-run")
    assert dry.exit == 0, dry
    p = dry.data()["preview"]
    assert p["total"] == 3 and len(p["items"]) == 3, p
    assert dry.data()["operation_id"].startswith("opb_"), dry
    for it in p["items"]:
        assert valid_operation_id(it["operation_id"]), it
    r = h.run("note", "bulk-tag", "--ids", "1,2,2,3", "--tag", "work", "--confirm", dry.token())
    assert r.exit == 0 and h.up.writes() == 3, r
    s = summary_of(r.data())
    assert s == {"total": 3, "succeeded": 3, "failed": 0, "unknown": 0, "skipped": 0}, s
    for it in r.data()["items"]:
        assert it["ok"] is True, it
    again = h.run("note", "bulk-tag", "--ids", "1,2,2,3", "--tag", "work", "--confirm", dry.token())
    expect_conflict(again, "token_used")
    show = h.run("operation", "show", dry.data()["operation_id"])
    assert show.data()["status"] == "applied" and len(show.data()["items"]) == 3, show


def test_batch_partial_is_not_ok(h: THarness) -> None:
    tok = h.must_token("note", "bulk-tag", "--ids", "1,2,3", "--tag", "x")
    h.up.hook = lambda kind, nid: (
        aicli.rejected(aicli.from_http(403, "Note 2 is locked.", "locked")) if nid == "2" else None
    )
    r = h.run("note", "bulk-tag", "--ids", "1,2,3", "--tag", "x", "--confirm", tok)
    assert r.exit == 1 and r.code() == "E_PARTIAL", r
    s = summary_of(r.details())
    assert s["succeeded"] == 2 and s["failed"] == 1 and s["skipped"] == 0, s
    item = r.details()["items"][1]
    assert item["ok"] is False and item["error"]["code"] == "E_FORBIDDEN", item


def test_batch_all_failed_uses_the_first_items_code(h: THarness) -> None:
    tok = h.must_token("note", "bulk-tag", "--ids", "1,2", "--tag", "x")
    h.up.hook = lambda kind, nid: aicli.rejected(aicli.from_http(404, "Gone."))
    r = h.run("note", "bulk-tag", "--ids", "1,2", "--tag", "x", "--confirm", tok)
    assert (
        r.exit == 3
        and r.code() == "E_NOT_FOUND"
        and r.details().get("dispatch_state") == "dispatched"
    ), r


def test_batch_unknown_item_makes_the_batch_unknown(h: THarness) -> None:
    tok = h.must_token("note", "bulk-tag", "--ids", "1,2,3", "--tag", "x")

    def hook(kind: str, nid: str) -> BaseException | None:
        if nid == "2":
            return ConnectionResetError("connection reset after send")
        if nid == "3":
            return aicli.rejected(aicli.from_http(422, "Bad tag."))
        return None

    h.up.hook = hook
    r = h.run("note", "bulk-tag", "--ids", "1,2,3", "--tag", "x", "--confirm", tok)
    assert r.exit == 6 and r.code() == "E_OUTCOME_UNKNOWN" and r.details().get("lookup"), r
    s = summary_of(r.details())
    assert s["succeeded"] == 1 and s["unknown"] == 1 and s["failed"] == 1, s
    h.up.hook = None
    blocked = h.run("note", "update", "2", "--title", "Y", "--dry-run")
    expect_conflict(blocked, "unresolved_operation")
    ok = h.run("note", "update", "3", "--title", "Y", "--dry-run")
    assert ok.exit == 0, ok


def test_batch_stops_when_continue_on_error_is_false(h: THarness) -> None:
    args = ["note", "bulk-tag", "--ids", "1,2,3", "--tag", "x", "--continue-on-error=false"]
    tok = h.must_token(*args)
    h.up.hook = lambda kind, nid: (
        aicli.rejected(aicli.from_http(409, "Conflict.")) if nid == "1" else None
    )
    r = h.run(*args, "--confirm", tok)
    assert r.code() == "E_CONFLICT", r
    s = summary_of(r.details())
    assert s["failed"] == 1 and s["skipped"] == 2 and h.up.writes() == 0, (s, h.up.writes())


def test_batch_with_a_missing_target_cannot_apply(h: THarness) -> None:
    r = h.run("note", "bulk-tag", "--ids", "1,99", "--tag", "x", "--dry-run")
    assert r.exit == 0 and r.data()["confirm_token"] is None, r
    p = r.data()["preview"]
    blocked = p["blocked_by"]
    assert p["can_apply"] is False and len(blocked) == 1 and "notes/99" in blocked[0]["message"], p


def test_empty_batch_is_usage(h: THarness) -> None:
    r = h.run("note", "bulk-tag", "--tag", "x", "--dry-run")
    assert (
        r.exit == 2
        and r.details().get("problem") == "missing_parameter"
        and r.details().get("parameter") == "--ids"
    ), r


def test_batch_drift_refuses_the_whole_batch(h: THarness) -> None:
    tok = h.must_token("note", "bulk-tag", "--ids", "1,2", "--tag", "x")
    h.up.bump("2")
    r = h.run("note", "bulk-tag", "--ids", "1,2", "--tag", "x", "--confirm", tok)
    expect_conflict(r, "state_drift")
    assert h.up.writes() == 0


def test_large_batch_error_stays_within_budget(h: THarness) -> None:
    ids = []
    with h.up.mu:
        for i in range(10, 70):
            nid = "9" + str(i)
            h.up.notes[nid] = TNote(nid, "notes/" + nid, "t", "", [], 1, "")
            ids.append(nid)
    args = ["note", "bulk-tag", "--ids", ",".join(ids), "--tag", "x"]
    bare = h.run(*args)
    assert bare.exit == 5 and len(bare.stdout.strip()) <= ERROR_BUDGET_CHARS, (
        len(bare.stdout),
        bare.exit,
    )
    assert is_token(bare.details().get("confirm_token")), bare.stdout
    h.up.hook = lambda kind, nid: aicli.rejected(aicli.from_http(422, "Bad tag."))
    r = h.run(*args, "--confirm", bare.token())
    assert r.code() == "E_VALIDATION" and len(r.stdout.strip()) <= ERROR_BUDGET_CHARS, (
        len(r.stdout),
        r.code(),
    )


def test_batch_items_get_their_own_idempotency_keys(h: THarness) -> None:
    """CLI-062 (decision 46, A5): each item sends `<user key>/<first 16 hex of
    sha256(target key)>` upstream, the same for every dry-run and distinct per
    target; the user's key stays bound into the batch token."""
    sent: list[tuple[str, str]] = []

    def build(u: Upstream) -> aicli.App:
        a = notes_app(u)
        c = a.root.commands["note"].commands["bulk-tag"]
        c.params.append(click.Option(["--idempotency-key"], help="Repeat-safe key"))

        def callback(ids: Any, tag: str, idempotency_key: Any) -> None:
            items = []
            for nid in ids:

                def apply(op: Op, nid: str = nid) -> Result:
                    sent.append((op.id, op.idempotency_key))
                    u.write(lambda: None)
                    return Result(observed={"tags": [tag]})

                items.append(
                    BatchItem(
                        Target("notes/" + nid, "note"),
                        lambda: Preview(
                            action="Tag notes",
                            can_apply=True,
                            changes=[FieldChange("tags", add=[tag])],
                        ),
                        apply,
                    )
                )
            a.write_batch(None, BatchSpec(items=items))

        c.callback = callback
        return a

    h.build = build
    args = ["note", "bulk-tag", "--ids", "1,2", "--tag", "k", "--idempotency-key", "user-key"]
    tok = h.must_token(*args)
    other = h.run(
        "note",
        "bulk-tag",
        "--ids",
        "1,2",
        "--tag",
        "k",
        "--idempotency-key",
        "another",
        "--confirm",
        tok,
    )
    assert other.code() == "E_CONFLICT" and other.details().get("conflict") == "token_mismatch", (
        other
    )
    r = h.run(*args, "--confirm", tok)
    assert r.exit == 0, r
    item_ids = [it["operation_id"] for it in r.data()["items"]]

    def key(target: str) -> str:
        return "user-key/" + hashlib.sha256(target.encode("utf-8")).hexdigest()[:16]

    assert sent == [(item_ids[0], key("notes/1")), (item_ids[1], key("notes/2"))], sent
    # without a user key the items send none
    sent.clear()
    plain = ["note", "bulk-tag", "--ids", "3", "--tag", "k"]
    r = h.run(*plain, "--confirm", h.must_token(*plain))
    assert r.exit == 0 and sent == [(r.data()["items"][0]["operation_id"], "")], (r, sent)


def many_notes(h: THarness, n: int) -> list[str]:
    """Add n notes (ids 100..) and return their ids."""
    ids = []
    with h.up.mu:
        for i in range(n):
            nid = str(100 + i)
            h.up.notes[nid] = TNote(nid, "notes/" + nid, "t", "", [], 1, "")
            ids.append(nid)
    return ids


def test_large_batch_operation_show_lists_attention_first(h: THarness) -> None:
    """operation show of a batch still too large once its preview is shortened lists
    the items that need attention first, then the done ones, each group in input
    order, cut to fit; items_omitted counts the rest and a notice names operation
    list --batch, which pages through every item in input order
    (operation.shortening). An item whose record cannot be read is unknown, with a
    null target, so it needs attention too."""
    ids = many_notes(h, 300)
    args = ["note", "bulk-tag", "--ids", ",".join(ids), "--tag", "x"]
    tok = h.must_token(*args)
    attention = [ids[7], ids[20], ids[150], ids[299]]

    def hook(kind: str, nid: str) -> BaseException | None:
        if nid in (ids[7], ids[299]):
            return ConnectionResetError("connection reset after send")
        if nid == ids[150]:
            return aicli.rejected(aicli.from_http(422, "Bad tag."))
        return None

    h.up.hook = hook
    r = h.run(*args, "--confirm", tok)
    batch = r.details().get("operation_id", "")
    assert r.code() == "E_OUTCOME_UNKNOWN" and batch.startswith("opb_"), r.code()
    # The shortened batch error says where every item is listed.
    note = f"shortened to fit; `notes operation list --batch {batch}` lists every item.)"
    assert note in r.details()["next_action"], r.details()["next_action"]
    # The record of the applied item for ids[20] goes missing.
    rec, _ = Journal(os.path.join(h.state, "operations")).read(batch)
    assert rec is not None and rec.items
    lost = rec.items[20]
    os.remove(os.path.join(h.state, "operations", lost + ".json"))

    def key_of(m: dict[str, Any]) -> Any:
        """A listed item's target key, None when its record cannot be read."""
        return m["target"]["key"] if isinstance(m["target"], dict) else None

    def want_key(nid: str) -> Any:
        return None if nid == ids[20] else "notes/" + nid

    # A cached update notice shares meta with the items_omitted notice; both fit.
    h.app().record_update_check("1.3.0", "info")
    show = h.run("operation", "show", "--id", batch)
    size = len(show.stdout.strip())
    assert show.exit == 0 and size <= SUCCESS_BUDGET_CHARS, (show.exit, size)
    aicli.validate_envelope(show.raw)
    d = show.data()
    items = d["items"]
    assert d["status"] == "unknown" and len(items) > len(attention), d["status"]
    assert d["items_omitted"] == 300 - len(items), d["items_omitted"]
    want = attention + [i for i in ids if i not in attention]
    for i, it in enumerate(items):
        assert key_of(it) == want_key(want[i]), (i, it, "the attention items come first")
        assert (i < len(attention)) != (it["status"] == "applied"), (i, it)
        if want[i] == ids[20]:
            assert it["operation_id"] == lost and it["status"] == "unknown", it
    command = f"notes operation list --batch {batch}"
    notices = {n["type"]: n for n in show.env["meta"]["notices"]}
    assert notices["items_omitted"] == {
        "type": "items_omitted",
        "severity": "info",
        "message": f"operation show lists {len(items)} of the batch's 300 items, those that need "
        f"attention first; run `{command}` to page through all of them.",
        "command": command,
    }, notices
    assert "update_available" in notices, notices
    # operation list --batch pages through every item in input order. A page of 200
    # rows would break the budget, so each page holds the rows that fit, says it was
    # truncated, and next_offset continues after them.
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = h.run(
            "operation", "list", "--batch", batch.upper(), "--limit", "200", "--offset", str(offset)
        )
        got, scope = page.data()["items"], page.data()["scope"]
        assert page.exit == 0 and got and len(page.stdout.strip()) <= SUCCESS_BUDGET_CHARS, offset
        assert page.data()["sort"] == "input_order" and scope["journal"], page.data()
        assert scope["batch"] == batch and scope["state"] == "every item of the batch", scope
        assert page.data()["total"] == 300, page.data()["total"]
        rows += got
        if not page.data()["has_more"]:
            assert "truncated" not in page.data() and len(rows) == 300, (offset, len(rows))
            break
        assert page.data()["truncated"] is True and page.data()["truncated_reason"], page.data()
        assert page.data()["next_offset"] == offset + len(got), page.data()
        offset = page.data()["next_offset"]
    assert [key_of(row) for row in rows] == [want_key(i) for i in ids]
    assert all(row["batch_id"] == batch and row["command"] == "note bulk-tag" for row in rows)
    assert len({row["operation_id"] for row in rows}) == 300
    assert rows[20]["operation_id"] == lost and rows[20]["status"] == "unknown", rows[20]
    # --state filters the rows, and total counts the filtered list.
    unknown = h.run("operation", "list", "--batch", batch, "--state", "unknown")
    assert [row["operation_id"] for row in unknown.data()["items"]] == [
        rows[7]["operation_id"],
        lost,
        rows[299]["operation_id"],
    ], unknown
    assert unknown.data()["total"] == 3 and unknown.data()["scope"]["state"] == "unknown", unknown
    # A batch is resolved through its items: resolve points at the unknown ones.
    res = h.run(
        "operation", "resolve", "--id", batch, "--outcome", "applied", "--evidence", "checked"
    )
    assert res.code() == "E_VALIDATION", res
    assert res.details()["next_action"] == (
        f"Run `notes operation list --batch {batch} --state unknown` to see the items to resolve."
    ), res


def test_small_batch_operation_show_lists_every_item_in_input_order(h: THarness) -> None:
    """A batch whose view fits lists every item in input order, an item that needs
    attention in its place, without items_omitted or a notice."""
    tok = h.must_token("note", "bulk-tag", "--ids", "1,2,3", "--tag", "x")
    h.up.hook = lambda kind, nid: (
        aicli.rejected(aicli.from_http(422, "Bad tag.")) if nid == "2" else None
    )
    r = h.run("note", "bulk-tag", "--ids", "1,2,3", "--tag", "x", "--confirm", tok)
    show = h.run("operation", "show", "--id", r.details()["operation_id"])
    items = show.data()["items"]
    assert show.exit == 0 and len(items) == 3, show
    assert "items_omitted" not in show.data() and "notices" not in show.env["meta"], show
    for i, nid in enumerate(("1", "2", "3")):
        assert items[i]["target"]["key"] == "notes/" + nid, items
        assert (nid == "2") != (items[i]["status"] == "applied"), items


def test_operation_list_batch_refusals_and_unreadable_items(h: THarness) -> None:
    """operation list --batch takes a batch id only: an item id, a confirm token or a
    malformed id is E_USAGE, an unknown batch E_NOT_FOUND and an unreadable batch
    record E_IO. An item record that cannot be read is a row with status unknown and
    a null target; without --state a planned batch lists its planned items."""
    dry = h.run("note", "bulk-tag", "--ids", "1,2,3", "--tag", "x", "--dry-run")
    batch = dry.data()["operation_id"]
    item = dry.data()["preview"]["items"][1]["operation_id"]
    for given in (item, dry.token(), "../../etc/passwd", ""):
        r = h.run("operation", "list", "--batch", given)
        d = r.details()
        assert r.exit == 2 and r.code() == "E_USAGE" and d["problem"] == "invalid_value", (given, r)
        assert d["parameter"] == "--batch", (given, r)
        assert d["expected"] == "a batch id: opb_ followed by 26 characters a-z and 2-7", r
        assert "`notes operation list --limit 5`" in d["next_action"], r
    r = h.run("operation", "list", "--batch", "opb_zzzzzzzzzzzzzzzzzzzzzzzzzz")
    assert r.exit == 3 and r.code() == "E_NOT_FOUND", r
    assert r.details()["next_action"] == (
        "Run `notes operation list --limit 5` to see recorded operations."
    ), r
    r = h.run("operation", "list", "--batch", batch)
    assert len(r.data()["items"]) == 3 and r.data()["has_more"] is False, r
    assert r.data()["total"] == 3, r
    os.remove(os.path.join(h.state, "operations", item + ".json"))
    r = h.run("operation", "list", "--batch", batch)
    rows = r.data()["items"]
    assert len(rows) == 3, r
    row = rows[1]
    assert row["operation_id"] == item and row["status"] == "unknown" and row["target"] is None, row
    assert row["batch_id"] == batch and row["command"] == "note bulk-tag", row
    r = h.run("operation", "list", "--batch", batch, "--state", "planned")
    assert len(r.data()["items"]) == 2 and r.data()["total"] == 2, r
    with open(os.path.join(h.state, "operations", batch + ".json"), "wb") as fh:
        fh.write(b"{not json")
    r = h.run("operation", "list", "--batch", batch)
    assert r.code() == "E_IO", r
    assert f"`notes operation show --id {batch}`" in r.details()["next_action"], r


def test_operation_show_of_unreadable_batch_records_validates(h: THarness) -> None:
    """operation show of a batch with an item record that cannot be read lists that
    item with a null target and status unknown, and the batch status follows from the
    readable items; operation show of a batch record that cannot be read has null
    items and status unknown (CLI-058). Every such view passes validate_envelope."""
    operations = os.path.join(h.state, "operations")

    def show(op_id: str) -> dict[str, Any]:
        r = h.run("operation", "show", "--id", op_id)
        assert r.exit == 0, r
        aicli.validate_envelope(r.raw)
        return r.data()

    # A planned batch whose second item record is missing.
    dry = h.run("note", "bulk-tag", "--ids", "1,2,3", "--tag", "x", "--dry-run")
    planned = dry.data()["operation_id"]
    missing = dry.data()["preview"]["items"][1]["operation_id"]
    os.remove(os.path.join(operations, missing + ".json"))
    d = show(planned)
    items = d["items"]
    assert d["status"] == "planned" and len(items) == 3, d
    assert items[1]["target"] is None and items[1]["status"] == "unknown", items
    assert items[0]["status"] == "planned", items
    # A dispatched batch whose first item record is corrupt.
    tok = h.must_token("note", "bulk-tag", "--ids", "1,2", "--tag", "y")
    r = h.run("note", "bulk-tag", "--ids", "1,2", "--tag", "y", "--confirm", tok)
    batch = r.data()["operation_id"]
    corrupt = r.data()["items"][0]["operation_id"]
    with open(os.path.join(operations, corrupt + ".json"), "wb") as fh:
        fh.write(b"{not json")
    d = show(batch)
    items = d["items"]
    assert d["status"] == "applied", d
    assert items[0]["target"] is None and items[0]["status"] == "unknown", items
    assert items[1]["status"] == "applied", items
    # The batch record itself cannot be read.
    with open(os.path.join(operations, batch + ".json"), "wb") as fh:
        fh.write(b"{not json")
    d = show(batch)
    assert d["status"] == "unknown" and "items" in d and d["items"] is None, d
    assert d["command"] == "unknown", d
    # Resolving it needs a scope covering every write effect of the tool. The item list
    # stays lost: the resolve output and a later operation show have null items and
    # status resolved, and operation list --batch cannot list them (CLI-061).
    h.grant({"effects": ["change", "deliver", "artifact", "persist"]})
    res = h.run(
        "operation",
        "resolve",
        "--id",
        batch,
        "--outcome",
        "applied",
        "--evidence",
        "checked notes 1 and 2",
    )
    assert res.exit == 0, res
    aicli.validate_envelope(res.raw)
    for d in (res.data(), show(batch)):
        assert d["status"] == "resolved" and "items" in d and d["items"] is None, d
        assert d["resolution"]["outcome"] == "applied", d
    r = h.run("operation", "list", "--batch", batch)
    assert r.code() == "E_IO", r
    assert f"`notes operation show --id {batch}`" in r.details()["next_action"], r
    listed = h.run("operation", "list").data()["items"]
    rows = [row for row in listed if row["operation_id"] == batch]
    assert len(rows) == 1 and "item_count" not in rows[0], rows
