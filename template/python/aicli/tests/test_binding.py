"""Token binding: what a confirm must repeat exactly (port of binding_test.go)."""

from __future__ import annotations

import os
from typing import Any

import pytest
from aicli_testapp import THarness, Upstream, expect_mismatch, notes_app

import aicli
from aicli._util import sha256_hex


def write_file(d: str, name: str, content: str) -> str:
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)
    return p


def test_positional_and_flag_bind_identically(h: THarness) -> None:
    tok = h.must_token("note", "update", "1", "--title", "Same")
    r = h.run("note", "update", "--id", "1", "--title", "Same", "--confirm", tok)
    assert r.exit == 0 and h.up.writes() == 1, r
    tok = h.must_token("note", "update", "--id", "2", "--title", "Same")
    r = h.run("note", "update", "2", "--title", "Same", "--confirm", tok)
    assert r.exit == 0 and h.up.writes() == 2, r


def test_body_and_body_file_bind_identically(h: THarness) -> None:
    f = write_file(h.dir, "body.txt", "Hello from a file")
    tok = h.must_token("note", "update", "1", "--body", "Hello from a file")
    r = h.run("note", "update", "1", "--body-file", f, "--confirm", tok)
    assert r.exit == 0, r
    tok = h.must_token("note", "update", "2", "--body-file", f)
    r = h.run("note", "update", "2", "--body", "Hello from a file", "--confirm", tok)
    assert r.exit == 0, r
    f2 = write_file(h.dir, "body2.txt", "Hello from a file\n")
    tok = h.must_token("note", "update", "3", "--body", "Hello from a file")
    r = h.run("note", "update", "3", "--body-file", f2, "--confirm", tok)
    expect_mismatch(r, "--body")
    h.stdin = "piped body"
    tok = h.must_token("note", "update", "3", "--body-file", "-")
    h.stdin = ""
    r = h.run("note", "update", "3", "--body", "piped body", "--confirm", tok)
    assert r.exit == 0, r
    n = h.up.get("3")
    assert n is not None and n.body == "piped body", n


def test_file_content_changed_after_dry_run_is_refused(h: THarness) -> None:
    f = write_file(h.dir, "body.txt", "approved text")
    tok = h.must_token("note", "update", "1", "--body-file", f)
    write_file(h.dir, "body.txt", "swapped text")
    r = h.run("note", "update", "1", "--body-file", f, "--confirm", tok)
    expect_mismatch(r, "--body")
    assert h.up.writes() == 0
    assert "swapped text" not in r.stdout, r


def test_body_and_body_file_together_is_usage(h: THarness) -> None:
    f = write_file(h.dir, "b.txt", "x")
    r = h.run("note", "update", "1", "--body", "x", "--body-file", f, "--dry-run")
    assert r.exit == 2 and r.details().get("problem") == "conflicting_flags", r


def test_missing_file_input_is_usage(h: THarness) -> None:
    r = h.run("note", "update", "1", "--body-file", os.path.join(h.dir, "nope.txt"), "--dry-run")
    assert (
        r.exit == 2
        and r.details().get("problem") == "invalid_value"
        and r.details().get("parameter") == "--body-file"
    ), r


MISMATCH_CASES = [
    (
        "changed flag",
        ["note", "update", "1", "--title", "A"],
        ["note", "update", "1", "--title", "B"],
        "--title",
    ),
    (
        "changed positional",
        ["note", "update", "1", "--title", "A"],
        ["note", "update", "2", "--title", "A"],
        "--id",
    ),
    (
        "added flag",
        ["note", "update", "1", "--title", "A"],
        ["note", "update", "1", "--title", "A", "--body", "x"],
        "--body",
    ),
    (
        "dropped flag",
        ["note", "update", "1", "--title", "A", "--body", "x"],
        ["note", "update", "1", "--title", "A"],
        "--body",
    ),
    (
        "slice order",
        ["note", "create", "--title", "A", "--tag", "x,y"],
        ["note", "create", "--title", "A", "--tag", "y,x"],
        "--tag",
    ),
    (
        "slice element",
        ["note", "create", "--title", "A", "--tag", "x", "--tag", "y"],
        ["note", "create", "--title", "A", "--tag", "x"],
        "--tag",
    ),
    (
        "explicit default",
        ["note", "create", "--title", "A"],
        ["note", "create", "--title", "A", "--body", ""],
        "--body",
    ),
    (
        "other command",
        ["note", "update", "1", "--title", "A"],
        ["note", "comment", "add", "1", "--text", "A"],
        "command",
    ),
    (
        "secret",
        ["note", "share", "1", "--to", "bob@example.com", "--token", "s3cret-one", "--dangerous"],
        ["note", "share", "1", "--to", "bob@example.com", "--token", "s3cret-two", "--dangerous"],
        "--token",
    ),
    (
        "idempotency key",
        ["note", "create", "--title", "A", "--idempotency-key", "k1"],
        ["note", "create", "--title", "A", "--idempotency-key", "k2"],
        "--idempotency-key",
    ),
]


@pytest.mark.parametrize(
    ("name", "dry", "confirm", "param"), MISMATCH_CASES, ids=[c[0] for c in MISMATCH_CASES]
)
def test_mismatch_names_the_parameter(
    h: THarness, name: str, dry: list[str], confirm: list[str], param: str
) -> None:
    tok = h.must_token(*dry)
    r = h.run(*confirm, "--confirm", tok)
    expect_mismatch(r, param)
    assert h.up.writes() == 0


def test_equivalent_spellings_bind_identically(h: THarness) -> None:
    cases = [
        (["note", "update", "1", "--title", "A"], ["note", "update", "1", "--title=A"]),
        (["note", "update", "1", "--title", "A"], ["note", "update", "1", "-t", "A"]),
        (["note", "update", "1", "--title", "A"], ["note", "update", "1", "--subject", "A"]),
        (["note", "update", "1", "--title", "A"], ["notes", "update", "1", "--title", "A"]),
        (["note", "delete", "1", "--dangerous"], ["note", "rm", "1", "--dangerous"]),
        (["note", "comment", "add", "1", "--text", "hi"], ["comment", "add", "1", "--text", "hi"]),
        (
            ["note", "comment", "add", "1", "--text", "hi"],
            ["comments", "create", "1", "--text", "hi"],
        ),
    ]
    for dry, confirm in cases:
        h.up.reset()
        tok = h.must_token(*dry)
        r = h.run(*confirm, "--confirm", tok)
        assert r.exit == 0, f"{confirm} should confirm a token from {dry}: {r}"


def test_presentation_flags_are_not_bound(h: THarness) -> None:
    tok = h.must_token("note", "update", "1", "--title", "A", "--pretty", "--fields", "title")
    r = h.run(
        "note", "update", "1", "--title", "A", "--quiet", "--format", "text", "--confirm", tok
    )
    assert r.exit == 0, r


def test_secrets_never_leave(h: THarness) -> None:
    secret = "tok-SUPER-SECRET-123"
    args = ["note", "share", "1", "--to", "bob@example.com", "--token", secret, "--dangerous"]
    dry = h.run(*args, "--dry-run")
    assert dry.exit == 0, dry
    assert dry.data()["preview"]["inputs"]["token"] == "***", dry
    bare = h.run(*args)
    conf = h.run(*args, "--confirm", dry.token())
    assert conf.exit == 0, conf
    ref = h.run("reference", "--command", "note share")
    for out in (dry, bare, conf, ref):
        assert secret not in out.stdout + out.stderr, out
    unkeyed = sha256_hex(secret.encode())
    for root, _dirs, files in os.walk(h.state):
        for name in files:
            with open(os.path.join(root, name), "rb") as fh:
                b = fh.read()
            assert secret.encode() not in b and unkeyed.encode() not in b, (
                f"{name} stores the secret or its unkeyed hash"
            )
    params = ref.data()["command"]["params"]
    assert any(p["name"] == "token" and p.get("secret") is True for p in params), params


def test_usage_errors_do_not_echo_secrets(h: THarness) -> None:
    secret = "s3cr3t-in-a-typo"
    r = h.run("note", "share", "1", "--to", "bob@example.com", "--tokn", secret, "--dangerous")
    assert r.exit == 2 and secret not in r.stdout + r.stderr, r


def test_batch_targets_are_bound(h: THarness) -> None:
    tok = h.must_token("note", "bulk-tag", "--ids", "1,2", "--tag", "work")
    for ids in ("1,3", "2,1", "1", "1,2,3"):
        r = h.run("note", "bulk-tag", "--ids", ids, "--tag", "work", "--confirm", tok)
        expect_mismatch(r, "--ids")
    r = h.run("note", "bulk-tag", "--ids", "1,2", "--tag", "home", "--confirm", tok)
    expect_mismatch(r, "--tag")
    assert h.up.writes() == 0
    r = h.run("note", "bulk-tag", "--ids", "1", "--ids", "2", "--tag", "work", "--confirm", tok)
    assert r.exit == 0 and h.up.writes() == 2, f"repeated --ids must bind like the comma form: {r}"


def test_set_resolved_binds_the_resolved_value(h: THarness) -> None:
    def build(u: Upstream) -> aicli.App:
        a = notes_app(u)
        note = a.root.commands["note"]
        upd = note.commands["update"]
        orig_update = upd.callback

        def update(nid: Any, **kw: Any) -> None:
            if a.changed(None, "id"):
                nid = nid.removeprefix("notes/")
                a.set_resolved(None, "id", nid)
            orig_update(nid=nid, **kw)

        upd.callback = update
        bulk = note.commands["bulk-tag"]
        orig_bulk = bulk.callback

        def bulk_tag(ids: Any, **kw: Any) -> None:
            if ids:
                ids = [i.removeprefix("notes/") for i in ids]
                aicli.set_resolved(None, "ids", *ids)
            orig_bulk(ids=ids, **kw)

        bulk.callback = bulk_tag
        return a

    h.build = build
    tok = h.must_token("note", "update", "notes/1", "--title", "A")
    r = h.run("note", "update", "1", "--title", "A", "--confirm", tok)
    assert r.exit == 0, f"a ref and an id must bind identically after set_resolved: {r}"
    tok = h.must_token("note", "bulk-tag", "--ids", "notes/1,2", "--tag", "x")
    r = h.run("note", "bulk-tag", "--ids", "1,notes/2", "--tag", "x", "--confirm", tok)
    assert r.exit == 0, f"resolved slices must bind identically: {r}"


def test_set_resolved_needs_a_defined_flag(h: THarness) -> None:
    seen: list[str] = []

    def build(u: Upstream) -> aicli.App:
        a = notes_app(u)
        get = a.root.commands["note"].commands["get"]

        def callback(nid: Any) -> None:
            for flag, values in (("nope", ("1",)), ("id", ())):
                try:
                    a.set_resolved(None, flag, *values)
                except aicli.Error as e:
                    seen.append(e.code)
            a.ok(None, {"id": nid})

        get.callback = callback
        return a

    h.build = build
    r = h.run("note", "get", "1")
    assert r.exit == 0 and seen == ["E_UNKNOWN", "E_UNKNOWN"], (r, seen)
