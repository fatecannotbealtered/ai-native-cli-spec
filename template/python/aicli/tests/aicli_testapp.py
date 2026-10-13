"""The test tool: a small notes CLI over an in-memory upstream that counts the write
requests it receives. Every library test drives it through App.execute (a port of
the Go library's testapp_test.go)."""

from __future__ import annotations

import datetime as _dt
import io
import json
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import click

import aicli
from aicli import (
    ARTIFACT,
    CHANGE,
    DELIVER,
    EXPERIMENTAL,
    OBSERVE,
    PERSIST,
    App,
    BatchItem,
    BatchSpec,
    Block,
    FieldChange,
    Identity,
    Meta,
    Op,
    OutputSpec,
    Page,
    Preview,
    Result,
    SchemaDef,
    Target,
    WriteSpec,
)
from aicli._util import time_string


@dataclass
class TNote:
    id: str
    ref: str
    title: str
    body: str
    tags: list[str]
    version: int
    updated_at: str


class Upstream:
    def __init__(self) -> None:
        self.mu = threading.Lock()
        self.hook: Callable[[str, str], Any] | None = None
        self.mangle: Callable[[str], str] | None = None
        self.reset()

    def reset(self) -> None:
        with self.mu:
            self.notes: dict[str, TNote] = {}
            for i, title in enumerate(["Groceries", "Ideas", "Travel"]):
                nid = str(i + 1)
                self.notes[nid] = TNote(
                    nid, "notes/" + nid, title, "body " + nid, ["seed"], 1, "2026-10-01T00:00:00Z"
                )
            self.next = 4
            self._writes = 0
            self.shared: list[str] = []
            self.comments: dict[str, list[str]] = {}

    def writes(self) -> int:
        with self.mu:
            return self._writes

    def get(self, nid: str) -> TNote | None:
        with self.mu:
            n = self.notes.get(nid)
            if n is None:
                return None
            return TNote(n.id, n.ref, n.title, n.body, list(n.tags), n.version, n.updated_at)

    def bump(self, nid: str) -> None:
        """A note changed behind the tool's back (state drift)."""
        with self.mu:
            n = self.notes.get(nid)
            if n is not None:
                n.version += 1
                n.title += " (edited elsewhere)"

    def run_hook(self, kind: str, nid: str) -> None:
        with self.mu:
            hook = self.hook
        if hook is not None:
            err = hook(kind, nid)
            if err is not None:
                raise err

    def write(self, fn: Callable[[], Any]) -> Any:
        with self.mu:
            self._writes += 1
            return fn()


TEST_CHANGELOG = """# Changelog

## [Unreleased]
### Added
- Something not released yet.

## [1.2.3] - 2026-10-01
### Added
- Batch tagging.
### Fixed
- Share retries.

## [1.2.0] - 2026-09-01
### Changed
- Compact JSON by default.
  It is also documented.

## [1.0.0] - 2026-01-01
### Added
- First release.
"""


def note_fields() -> list[str]:
    return ["id", "ref", "title", "body", "tags", "version", "updated_at"]


def notes_app(u: Upstream) -> App:
    root = click.Group("notes", short_help="A test notes tool")
    a = App(
        name="notes",
        version="1.2.3",
        risk_tier="T2",
        env_prefix="NOTES",
        root=root,
        changelog=TEST_CHANGELOG,
        min_skill_version="1.0.0",
        skill_dirs=[],
        shortcuts={"comment": ["note", "comment"], "comments": ["note", "comment"]},
        short_flags={"t": "title"},
        flag_aliases={"subject": "title"},
        flag_synonyms={"all": ["limit"], "text": ["body"]},
        verb_synonyms={"view": ["get"], "show": ["get"], "remove": ["delete"]},
        identity=lambda: Identity("tester", "https://notes.example", {"credentials_source": "env"}),
    )
    a.schema("note", SchemaDef(shape="object", fields=note_fields(), untrusted=["title", "body"]))
    a.schema(
        "note_page",
        SchemaDef(
            shape="page",
            fields=note_fields(),
            default_fields=["ref", "title", "tags"],
            untrusted=["title", "body"],
        ),
    )
    a.schema(
        "note_write", SchemaDef(shape="object", fields=note_fields(), untrusted=["title", "body"])
    )
    a.schema("share_result", SchemaDef(shape="object", fields=["to"]))
    a.schema("tag_result", SchemaDef(shape="object", fields=["id", "tags"]))
    a.schema("comment_result", SchemaDef(shape="object", fields=["id", "text"]))

    note = click.Group("note")
    a.group(note, "Work with notes", aliases=["notes"])
    root.add_command(note)

    @note.command("list")
    @click.option("--limit", type=int, default=20, help="Maximum notes to return")
    @click.option("--tag", default="", help="Only notes with this tag")
    def list_cmd(limit: int, tag: str) -> None:
        with u.mu:
            items = sorted(
                (n for n in u.notes.values() if not tag or tag in n.tags), key=lambda n: n.id
            )
        p = Page(items=items, scope={"tag": tag}, sort="id_asc")
        if len(items) > limit:
            p.items = items[:limit]
            p.has_more, p.next_offset = True, limit
        a.list(None, p)

    a.register(
        list_cmd,
        Meta(
            summary="List notes",
            effects=[OBSERVE],
            output="note_page",
            maturity=EXPERIMENTAL,
            examples=["notes note list --tag seed"],
        ),
    )

    @note.command("get")
    @click.option("--id", "nid", required=True, help="Note id")
    def get_cmd(nid: str | None) -> None:
        n = u.get(nid or "")
        if n is None:
            raise aicli.not_found("No note has that id.", "Run `notes note list` to find the note.")
        a.ok(None, n)

    a.register(
        get_cmd,
        Meta(
            summary="Show one note",
            effects=[OBSERVE],
            output="note",
            positional="id",
            error_codes=["E_NOT_FOUND"],
            examples=["notes note get 1"],
        ),
    )

    @note.command("create")
    @click.option("--title", required=True, help="Title")
    @click.option("--body", default="", help="Body text")
    @click.option("--body-file", help="Read the body from a file (- for stdin)")
    @click.option("--tag", multiple=True, help="Tags")
    @click.option("--idempotency-key", help="Repeat-safe key")
    def create_cmd(
        title: str, body: str, body_file: Any, tag: tuple[str, ...], idempotency_key: Any
    ) -> None:
        b = a.read_file_input(None, "body-file")
        if b is not None:
            body = b.decode("utf-8", "replace")
        tags = list(tag)

        def preview() -> Preview:
            return Preview(
                action="Create a note",
                can_apply=True,
                changes=[
                    FieldChange("title", after=title),
                    FieldChange("body", after=body),
                    FieldChange("tags", after=tags),
                ],
            )

        def apply(op: Op) -> Result:
            u.run_hook("create", "")

            def do() -> TNote:
                nid = str(u.next)
                u.next += 1
                nn = TNote(nid, "notes/" + nid, title, body, list(tags), 1, "2026-10-12T00:00:00Z")
                u.notes[nid] = nn
                return TNote(
                    nn.id, nn.ref, nn.title, nn.body, list(nn.tags), nn.version, nn.updated_at
                )

            n = u.write(do)
            return Result(
                observed={"title": n.title, "body": n.body, "tags": n.tags},
                data=n,
                untrusted=["title", "body"],
            )

        a.write(
            None,
            WriteSpec(target=Target("notes", "collection", "notes"), preview=preview, apply=apply),
        )

    a.register(
        create_cmd,
        Meta(
            summary="Create a note",
            effects=[CHANGE],
            output="note_write",
            file_inputs={"body-file": "body"},
            examples=[
                "notes note create --title Hello --dry-run",
                "notes note create --title Hello --confirm <token>",
            ],
        ),
    )

    @note.command("update")
    @click.option("--id", "nid", required=True, help="Note id")
    @click.option("--title", help="New title")
    @click.option("--body", help="New body")
    @click.option("--body-file", help="Read the new body from a file (- for stdin)")
    def update_cmd(nid: Any, title: Any, body: Any, body_file: Any) -> None:
        nid = nid or ""
        title_set = a.changed(None, "title")
        body_set = a.changed(None, "body")
        b = a.read_file_input(None, "body-file")
        if b is not None:
            body, body_set = b.decode("utf-8", "replace"), True

        def preview() -> Preview:
            n = u.get(nid)
            if n is None:
                raise aicli.not_found(
                    "No note has that id.", "Run `notes note list` to find the note."
                )
            ch = []
            if title_set:
                ch.append(FieldChange("title", before=n.title, after=title))
            if body_set:
                ch.append(FieldChange("body", before=n.body, after=body))
            pv = Preview(
                action="Update note " + nid,
                changes=ch,
                base_version=str(n.version),
                base_source="version",
                can_apply=True,
                untrusted=["changes[].before"],
            )
            if not ch:
                pv.blocked_by = [Block("no_change", "Give --title or --body to change.")]
            return pv

        def apply(op: Op) -> Result:
            u.run_hook("update", nid)

            def do() -> TNote:
                nn = u.notes[nid]
                if title_set:
                    nn.title = u.mangle(title) if u.mangle is not None else title
                if body_set:
                    nn.body = body
                nn.version += 1
                return TNote(
                    nn.id, nn.ref, nn.title, nn.body, list(nn.tags), nn.version, nn.updated_at
                )

            n = u.write(do)
            obs: dict[str, Any] = {}
            if title_set:
                obs["title"] = n.title
            if body_set:
                obs["body"] = n.body
            return Result(observed=obs, data=n)

        a.write(
            None,
            WriteSpec(
                target=Target("notes/" + nid, "note"),
                preview=preview,
                apply=apply,
                read_back="note get " + nid,
            ),
        )

    a.register(
        update_cmd,
        Meta(
            summary="Change a note",
            effects=[CHANGE],
            output="note_write",
            positional="id",
            file_inputs={"body-file": "body"},
            error_codes=["E_NOT_FOUND"],
            examples=[
                "notes note update 1 --title New --dry-run",
                "notes note update 1 --title New --confirm <token>",
            ],
        ),
    )

    @note.command("delete")
    @click.option("--id", "nid", required=True, help="Note id")
    def delete_cmd(nid: Any) -> None:
        nid = nid or ""

        def preview() -> Preview:
            n = u.get(nid)
            if n is None:
                raise aicli.not_found("No note has that id.")
            return Preview(
                action="Delete note " + nid,
                can_apply=True,
                base_version=str(n.version),
                changes=[FieldChange("note", before=n.title, note="deleted permanently")],
            )

        def apply(op: Op) -> Result:
            u.run_hook("delete", nid)

            def do() -> bool:
                if nid not in u.notes:
                    return True
                del u.notes[nid]
                return False

            if u.write(do):
                raise aicli.rejected(aicli.from_http(404, "No note has that id."))
            return Result()

        a.write(
            None, WriteSpec(target=Target("notes/" + nid, "note"), preview=preview, apply=apply)
        )

    a.register(
        delete_cmd,
        Meta(
            summary="Delete a note",
            effects=[CHANGE],
            dangerous=True,
            output="note_write",
            positional="id",
            aliases=["rm"],
            examples=[
                "notes note delete 1 --dangerous --dry-run",
                "notes note delete 1 --dangerous --confirm <token>",
            ],
        ),
    )

    @note.command("share")
    @click.option("--id", "nid", required=True, help="Note id")
    @click.option("--to", required=True, help="Recipient address")
    @click.option("--token", help="Access token for the sharing service")
    def share_cmd(nid: Any, to: str, token: Any) -> None:
        nid = nid or ""

        def preview() -> Preview:
            if u.get(nid) is None:
                raise aicli.not_found("No note has that id.")
            return Preview(
                action="Send note " + nid,
                can_apply=True,
                destinations=[to],
                changes=[FieldChange("recipient", after=to)],
            )

        def apply(op: Op) -> Result:
            u.run_hook("share", nid)
            u.write(lambda: u.shared.append(nid + "->" + to))
            return Result(data={"to": to})

        a.write(
            None, WriteSpec(target=Target("notes/" + nid, "note"), preview=preview, apply=apply)
        )

    a.register(
        share_cmd,
        Meta(
            summary="Send a note to someone",
            effects=[DELIVER],
            output="share_result",
            positional="id",
            secrets=["token"],
            examples=[
                "notes note share 1 --to bob@example.com --dangerous --dry-run",
                "notes note share 1 --to bob@example.com --dangerous --confirm <token>",
            ],
        ),
    )

    @note.command("bulk-tag")
    @click.option("--ids", multiple=True, help="Note ids (comma-separated or repeated)")
    @click.option("--tag", required=True, help="Tag to add")
    def bulk_cmd(ids: Any, tag: str) -> None:
        ids_list = list(ids)
        items = []
        for nid in ids_list:

            def preview(nid: str = nid) -> Preview:
                n = u.get(nid)
                if n is None:
                    raise aicli.not_found("No note has that id.")
                return Preview(
                    action="Tag notes",
                    can_apply=True,
                    base_version=str(n.version),
                    changes=[FieldChange("tags", add=[tag])],
                )

            def apply(op: Op, nid: str = nid) -> Result:
                u.run_hook("tag", nid)

                def do() -> list[str]:
                    n = u.notes[nid]
                    if tag not in n.tags:
                        n.tags.append(tag)
                    n.version += 1
                    return list(n.tags)

                tags = u.write(do)
                return Result(observed={"tags": tags}, data={"id": nid, "tags": tags})

            items.append(BatchItem(Target("notes/" + nid, "note"), preview, apply))
        a.write_batch(None, BatchSpec(items=items))

    a.register(
        bulk_cmd,
        Meta(
            summary="Add a tag to several notes",
            effects=[CHANGE],
            output="tag_result",
            batch_flag="ids",
            examples=[
                "notes note bulk-tag --ids 1,2 --tag work --dry-run",
                "notes note bulk-tag --ids 1,2 --tag work --confirm <token>",
            ],
        ),
    )

    # note tail streams the notes as NDJSON (CLI-006); --fail-after ends the stream
    # with an error after that many items.
    @note.command("tail")
    @click.option(
        "--fail-after", type=int, default=0, help="End with an error after this many notes (tests)"
    )
    def tail_cmd(fail_after: int) -> None:
        s = a.stream(None)
        with u.mu:
            items = sorted(
                (
                    TNote(n.id, n.ref, n.title, n.body, list(n.tags), n.version, n.updated_at)
                    for n in u.notes.values()
                ),
                key=lambda n: n.id,
            )
        for i, n in enumerate(items):
            if fail_after > 0 and i == fail_after:
                raise aicli.from_http(503, "The notes service stopped answering.")
            s.item(n)
        s.summary({"count": s.count(), "has_more": False})

    a.register(
        tail_cmd,
        Meta(
            summary="Stream every note as NDJSON, one line per note",
            effects=[OBSERVE],
            output="note",
            examples=["notes note tail"],
        ),
    )

    # note export writes every note to a local file through the gate (CLI-042).
    @note.command("export")
    @click.option("--output", required=True, help="File to write")
    def export_cmd(output: str) -> None:
        def produce() -> bytes:
            u.run_hook("export", "")
            with u.mu:
                return "".join(f"{nid}\t{u.notes[nid].title}\n" for nid in sorted(u.notes)).encode(
                    "utf-8"
                )

        a.write_output(None, OutputSpec(path=output, produce=produce, summary={"format": "tsv"}))

    a.schema(
        "export_result", SchemaDef(shape="object", fields=["path", "bytes", "sha256", "format"])
    )
    a.register(
        export_cmd,
        Meta(
            summary="Write every note to a local file",
            effects=[ARTIFACT, PERSIST],
            output="export_result",
            examples=[
                "notes note export --output notes.tsv --dry-run",
                "notes note export --output notes.tsv --confirm <token>",
            ],
        ),
    )

    comment = click.Group("comment")
    a.group(comment, "Comments on notes")
    note.add_command(comment)

    @comment.command("add")
    @click.option("--id", "nid", required=True, help="Note id")
    @click.option("--text", required=True, help="Comment text")
    def add_cmd(nid: Any, text: str) -> None:
        nid = nid or ""

        def preview() -> Preview:
            if u.get(nid) is None:
                raise aicli.not_found("No note has that id.")
            return Preview(
                action="Comment on note " + nid,
                can_apply=True,
                changes=[FieldChange("comments", add=text)],
            )

        def apply(op: Op) -> Result:
            u.write(lambda: u.comments.setdefault(nid, []).append(text))
            return Result(data={"id": nid, "text": text}, untrusted=["text"])

        a.write(
            None,
            WriteSpec(
                target=Target("notes/" + nid + "#comments", "comments"),
                preview=preview,
                apply=apply,
            ),
        )

    a.register(
        add_cmd,
        Meta(
            summary="Comment on a note",
            effects=[CHANGE],
            output="comment_result",
            positional="id",
            aliases=["create"],
            examples=[
                "notes note comment add 1 --text Hi --dry-run",
                "notes note comment add 1 --text Hi --confirm <token>",
            ],
        ),
    )
    return a


# ---------------------------------------------------------------- harness


def default_scope() -> dict[str, Any]:
    now = _dt.datetime.now(_dt.timezone.utc)
    return {
        "scope_id": "scp_test",
        "tool": "notes",
        "granted_by": "tester",
        "granted_at": time_string(now - _dt.timedelta(hours=1)),
        "expires_at": time_string(now + _dt.timedelta(hours=1)),
        "effects": ["change", "deliver"],
        "dangerous": True,
        "destinations": ["bob@example.com", "https://hooks.example.com/notes"],
    }


@dataclass
class TOut:
    exit: int
    stdout: str
    stderr: str
    raw: bytes = b""
    env: dict[str, Any] | None = None
    lines: list[dict[str, Any]] = field(default_factory=list)

    def code(self) -> str:
        e = (self.env or {}).get("error")
        return e.get("code", "") if isinstance(e, dict) else ""

    def details(self) -> dict[str, Any]:
        e = (self.env or {}).get("error")
        d = e.get("details") if isinstance(e, dict) else None
        return d if isinstance(d, dict) else {}

    def data(self) -> dict[str, Any]:
        d = (self.env or {}).get("data")
        return d if isinstance(d, dict) else {}

    def token(self) -> str:
        t = self.data().get("confirm_token")
        if isinstance(t, str):
            return t
        t = self.details().get("confirm_token")
        return t if isinstance(t, str) else ""

    def __str__(self) -> str:
        return f"exit={self.exit} stdout={self.stdout} stderr={self.stderr}"


# Every envelope a test produces is kept here (and validated), so the suite can
# also check them with check-contract.py (VER-002).
ENVELOPES: list[bytes] = []
STREAMS: list[bytes] = []


@dataclass
class THarness:
    dir: str
    up: Upstream = field(default_factory=Upstream)
    scope_on: bool = True
    now: Callable[[], _dt.datetime] | None = None
    stdin: str = ""
    env: dict[str, str] = field(default_factory=dict)
    build: Callable[[Upstream], App] = notes_app

    def __post_init__(self) -> None:
        self.state = os.path.join(self.dir, "state")
        self.scope = os.path.join(self.dir, "scope.json")
        self.grant(None)

    def grant(self, change: dict[str, Any] | None) -> None:
        """Write the scope file: the default scope with change applied (None values delete keys)."""
        obj = default_scope()
        for k, v in (change or {}).items():
            if v is None:
                obj.pop(k, None)
            else:
                obj[k] = v
        with open(self.scope, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(obj, fh, indent=2)

    def write_scope_raw(self, raw: bytes) -> None:
        with open(self.scope, "wb") as fh:
            fh.write(raw)

    def app(self) -> App:
        a = self.build(self.up)
        a.state_dir = self.state
        scope = self.scope if self.scope_on else ""
        env = dict(self.env)
        a.env = lambda k: scope if k == "NOTES_SCOPE_FILE" else env.get(k, "")
        if self.now is not None:
            a.now = self.now
        return a

    def run(self, *args: str) -> TOut:
        a = self.app()
        out, err = io.BytesIO(), io.BytesIO()
        a.stdout, a.stderr = out, err
        a.stdin = io.BytesIO(self.stdin.encode("utf-8"))
        code = a.execute(list(args))
        raw = out.getvalue()
        r = TOut(
            code, raw.decode("utf-8", "replace"), err.getvalue().decode("utf-8", "replace"), raw
        )
        if r.stdout.strip().startswith("{"):
            first = r.stdout.split("\n", 1)[0]
            if (
                '"type":' in first
                and r.stdout.count("\n") >= 1
                and json.loads(first).get("type") in ("item", "summary")
            ):
                # an NDJSON stream (CLI-006): the last line is the summary
                r.lines = [json.loads(line) for line in r.stdout.splitlines() if line.strip()]
                r.env = r.lines[-1]
                STREAMS.append(raw)
                try:
                    aicli.validate_stream(raw)
                except aicli.ContractViolation as exc:
                    raise AssertionError(
                        f"stream of {' '.join(args)!r} does not validate: {exc}\n{r.stdout}"
                    ) from None
                return r
            r.env = json.loads(r.stdout)
            ENVELOPES.append(raw)
            try:
                aicli.validate_envelope(raw)
            except aicli.ContractViolation as exc:
                raise AssertionError(
                    f"envelope of {' '.join(args)!r} does not validate: {exc}\n{r.stdout}"
                ) from None
        return r

    def must_token(self, *args: str) -> str:
        r = self.run(*args, "--dry-run")
        tok = r.token()
        assert r.exit == 0 and tok, f"dry-run {args} failed: {r}"
        return tok


def expect_conflict(r: TOut, conflict: str) -> None:
    assert r.exit == 6 and r.code() == "E_CONFLICT" and r.details().get("conflict") == conflict, (
        f"want E_CONFLICT {conflict}, got {r}"
    )


def expect_mismatch(r: TOut, param: str) -> None:
    expect_conflict(r, "token_mismatch")
    assert r.details().get("parameter") == param, (
        f"token_mismatch should name {param!r}, named {r.details().get('parameter')}"
    )


def list_has(x: Any, want: str) -> bool:
    return isinstance(x, list) and want in x
