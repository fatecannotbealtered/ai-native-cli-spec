"""NDJSON streams (CLI-006) and the --output helper (CLI-042, decision 20)."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

import click
from aicli_testapp import THarness, Upstream, expect_conflict, notes_app

import aicli
from aicli import ARTIFACT, OBSERVE, PERSIST, Meta, OutputSpec, SchemaDef


def stream_app(u: Upstream) -> aicli.App:
    a = notes_app(u)
    a.schema("note_line", SchemaDef(shape="object", fields=["id", "title"], untrusted=["title"]))
    a.schema(
        "export_result", SchemaDef(shape="object", fields=["path", "bytes", "sha256", "notes"])
    )
    note = a.root.commands["note"]

    @note.command("tail")
    @click.option("--limit", type=int, default=3, help="Notes to emit")
    @click.option("--fail", is_flag=True, help="End with an upstream error")
    def tail(limit: int, fail: bool) -> None:
        s = a.stream(None)
        for n in sorted(u.notes.values(), key=lambda n: n.id)[:limit]:
            s.item({"id": n.id, "title": n.title})
        if fail:
            raise aicli.from_http(503, "The upstream went away.")
        s.summary({"count": limit})

    a.register(
        tail,
        Meta(
            summary="Follow notes as NDJSON",
            effects=[OBSERVE],
            output="note_line",
            examples=["notes note tail --limit 2"],
        ),
    )

    @note.command("export")
    @click.option("--output", help="File to write")
    def export(output: Any) -> None:
        def produce() -> str:
            return json.dumps(
                [
                    {"id": n.id, "title": n.title}
                    for n in sorted(u.notes.values(), key=lambda n: n.id)
                ]
            )

        a.write_output(
            None, OutputSpec(path=output or "", produce=produce, summary={"notes": len(u.notes)})
        )

    a.register(
        export,
        Meta(
            summary="Export the notes to a file",
            effects=[ARTIFACT, PERSIST],
            output="export_result",
            examples=[
                "notes note export --output notes.json --dry-run",
                "notes note export --output notes.json --confirm <token>",
            ],
        ),
    )
    return a


def harness(h: THarness) -> THarness:
    h.build = stream_app
    out = os.path.join(h.dir, "out")
    os.makedirs(out, exist_ok=True)
    h.grant(
        {"effects": ["change", "deliver", "artifact", "persist"], "outputs": [out], "roots": [out]}
    )
    return h


def test_stream_writes_items_then_a_summary(h: THarness) -> None:
    harness(h)
    r = h.run("note", "tail", "--limit", "2")
    assert r.exit == 0 and [line["type"] for line in r.lines] == ["item", "item", "summary"], r
    assert r.lines[0]["data"] == {"id": "1", "title": "Groceries"}, r.lines[0]
    assert r.lines[0]["meta"]["untrusted"] == ["data.title"], r.lines[0]
    assert r.lines[-1]["data"] == {"count": 2} and r.lines[-1]["ok"] is True
    assert list(r.lines[0]) == ["ok", "schema_version", "type", "data", "meta"]


def test_stream_ends_with_the_error_summary(h: THarness) -> None:
    harness(h)
    r = h.run("note", "tail", "--limit", "2", "--fail")
    assert r.exit == 7 and [line["type"] for line in r.lines] == ["item", "item", "summary"], r
    last = r.lines[-1]
    assert last["ok"] is False and last["error"]["code"] == "E_SERVER", last


def test_stream_in_text_mode(h: THarness) -> None:
    harness(h)
    r = h.run("note", "tail", "--limit", "1", "--format", "text")
    assert r.exit == 0 and "title: Groceries" in r.stdout and not r.stdout.startswith("{"), r.stdout


def test_stream_fields_projection(h: THarness) -> None:
    harness(h)
    r = h.run("note", "tail", "--limit", "1", "--fields", "title")
    assert r.lines[0]["data"] == {"title": "Groceries"}, r.lines[0]


def test_write_output_goes_through_the_gate(h: THarness) -> None:
    harness(h)
    path = os.path.join(h.dir, "out", "notes.json")
    dry = h.run("note", "export", "--output", path, "--dry-run")
    assert dry.exit == 0, dry
    p = dry.data()["preview"]
    assert (
        p["outputs"] == [path]
        and p["effects"] == ["artifact", "persist"]
        and p["base"]["version"] == "absent"
    ), p
    assert not os.path.exists(path)
    conf = h.run("note", "export", "--output", path, "--confirm", dry.token())
    assert conf.exit == 0, conf
    with open(path, "rb") as fh:
        content = fh.read()
    res = conf.data()["result"]
    assert res == {
        "path": path,
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "notes": 3,
    }, res
    assert conf.data()["receipt"]["outcome"]["persistence"] == "saved", conf.data()["receipt"]
    # overwriting (persist) is a new preview with the file's digest as its base
    again = h.run("note", "export", "--output", path, "--dry-run")
    assert (
        again.data()["preview"]["base"]["version"]
        == "sha256:" + hashlib.sha256(content).hexdigest()
    ), again


def test_write_output_outside_the_scope_is_refused(h: THarness) -> None:
    harness(h)
    path = os.path.join(h.dir, "elsewhere.json")
    r = h.run("note", "export", "--output", path, "--dry-run")
    assert (
        r.exit == 4
        and r.code() == "E_FORBIDDEN"
        and r.details()["scope"]["status"] == "insufficient"
    ), r
    assert not os.path.exists(path)


def test_write_output_refuses_a_file_that_appeared(h: THarness) -> None:
    harness(h)
    path = os.path.join(h.dir, "out", "late.json")
    dry = h.run("note", "export", "--output", path, "--dry-run")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("someone else's file")
    r = h.run("note", "export", "--output", path, "--confirm", dry.token())
    expect_conflict(r, "state_drift")
    with open(path, encoding="utf-8") as fh:
        assert fh.read() == "someone else's file"


def test_write_command_cannot_stream(h: THarness) -> None:
    def build(u: Upstream) -> aicli.App:
        a = stream_app(u)
        a.root.commands["note"].commands["create"].callback = lambda **kw: a.stream(None)
        return a

    h.build = build
    r = h.run("note", "create", "--title", "x", "--dry-run")
    assert r.code() == "E_UNKNOWN", r
