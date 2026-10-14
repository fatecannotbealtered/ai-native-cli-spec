"""Regression tests for the fixes before v2.0.0-rc.2 (FIXES-RC2.md, decision 47),
found by the gitlab-cli pilot's command groups: one or more per item, named after it
(port of rc2_test.go)."""

from __future__ import annotations

import datetime as _dt
import io
import json
import os
import shutil
import threading
import urllib.request
from collections.abc import Callable, Iterator
from typing import Any

import click
import pytest
from aicli_testapp import (
    THarness,
    TNote,
    TOut,
    Upstream,
    expect_conflict,
    expect_mismatch,
    note_fields,
    notes_app,
)
from test_conformance import EXPORT_DIR, harness_for
from test_settings import settings_app, settings_file

import aicli
from aicli import (
    ARTIFACT,
    CHANGE,
    OBSERVE,
    PERSIST,
    App,
    BatchItem,
    BatchSpec,
    FieldChange,
    ItemOutcome,
    Meta,
    Notice,
    Op,
    OutputSpec,
    Page,
    Preview,
    Recorder,
    Result,
    SchemaDef,
    Target,
    WriteSpec,
)
from aicli import validate as V
from aicli._util import time_string
from aicli.conformance import default_invocation, over_success_budget
from aicli.contract_gen import COMMAND_BUDGET_CHARS, SUCCESS_BUDGET_CHARS
from aicli.update import _AppProxyHandler, app_proxies

WINDOWS = os.name == "nt"


@pytest.fixture(scope="module", autouse=True)
def _conformance_export_dir() -> Iterator[None]:
    """The conformance sample's --output directory (test_conformance.EXPORT_DIR),
    which test_conformance removes once its own tests ran."""
    created = not os.path.isdir(EXPORT_DIR)
    os.makedirs(EXPORT_DIR, exist_ok=True)
    yield
    if created:
        shutil.rmtree(EXPORT_DIR, ignore_errors=True)


def note_group(a: App) -> click.Group:
    g = a.root.commands["note"]  # type: ignore[union-attr]
    assert isinstance(g, click.Group)
    return g


def node_meta(a: App, path: str) -> Meta:
    m = a.meta_for(a.command_by_path(path))
    assert m is not None, path
    return m


def notice_of_type(r: TOut, kind: str) -> dict[str, Any] | None:
    for n in (r.env or {}).get("meta", {}).get("notices", []) or []:
        if n.get("type") == kind:
            return n
    return None


def message(r: TOut) -> str:
    e = (r.env or {}).get("error")
    return e.get("message", "") if isinstance(e, dict) else ""


# ---------------------------------------------------------------- Q1 resolve_target


def remove_by_title(lookups: list[int]) -> Callable[[Upstream], App]:
    """`note remove --title T`: its target is the note with that title, found with
    resolve_target, which counts its reads."""

    def build(u: Upstream) -> App:
        a = notes_app(u)

        @note_group(a).command("remove")
        @click.option("--title", default="", help="Title of the note")
        def remove(title: str) -> None:
            found: list[str] = []

            def resolve() -> Target:
                lookups[0] += 1
                with u.mu:
                    for n in u.notes.values():
                        if n.title == title:
                            found[:] = [n.id]
                            return Target("notes/" + n.id, "note")
                raise aicli.not_found(
                    "No note has that title.", "Run `notes note list` to see the titles."
                )

            def preview() -> Preview:
                return Preview(
                    action="Remove the note titled " + title,
                    can_apply=True,
                    base_version="1",
                    changes=[FieldChange("note", before=title, note="removed")],
                )

            def apply(op: Op) -> Result:
                u.write(lambda: u.notes.pop(found[0], None))
                return Result()

            a.write(None, WriteSpec(resolve_target=resolve, preview=preview, apply=apply))

        a.register(
            remove,
            Meta(
                summary="Remove a note by its title",
                effects=[CHANGE],
                output="note_write",
                examples=[
                    "notes note remove --title Ideas --dry-run",
                    "notes note remove --title Ideas --confirm <token>",
                ],
            ),
        )
        return a

    return build


def test_q1_resolve_target_runs_inside_the_gate(h: THarness) -> None:
    """A target resolved with WriteSpec.resolve_target is looked up at the dry-run
    before the scope check, and at confirm only after the token checks, so a used
    token is token_used before any tool code runs."""
    lookups = [0]
    h.build = remove_by_title(lookups)
    args = ("note", "remove", "--title", "Ideas")
    dry = h.run(*args, "--dry-run")
    assert dry.exit == 0 and lookups[0] == 1, dry
    assert dry.data()["preview"]["target"]["key"] == "notes/2", dry
    r = h.run(*args, "--confirm", dry.token())
    assert r.exit == 0 and lookups[0] == 2 and h.up.writes() == 1, r
    # the note is gone: a repeat is token_used, and the lookup never ran
    expect_conflict(h.run(*args, "--confirm", dry.token()), "token_used")
    assert lookups[0] == 2
    # the lookup's own error comes back as the tool's, nothing sent
    r = h.run("note", "remove", "--title", "Nope", "--dry-run")
    assert r.code() == "E_NOT_FOUND" and r.details()["dispatch_state"] == "not_started", r
    # a target that resolves differently at confirm is token_mismatch (target)
    tok = h.must_token("note", "remove", "--title", "Travel")
    with h.up.mu:
        h.up.notes["3"].title = "Elsewhere"
        h.up.notes["9"] = TNote("9", "notes/9", "Travel", "", [], 1, "2026-10-01T00:00:00Z")
    expect_mismatch(h.run("note", "remove", "--title", "Travel", "--confirm", tok), "target")
    assert h.up.writes() == 1


def test_q1_resolver_crash_and_missing_key(h: THarness) -> None:
    """A resolver that crashes is an internal error with its text in details.cause;
    one that returns no key is a tool bug; App.write names resolve_target."""
    mode = ["crash"]

    def build(u: Upstream) -> App:
        a = notes_app(u)

        @note_group(a).command("remove")
        def remove() -> None:
            def resolve() -> Target:
                if mode[0] == "crash":
                    raise RuntimeError("lookup exploded")
                return Target("")

            spec = WriteSpec(
                resolve_target=resolve if mode[0] != "none" else None,
                preview=lambda: Preview(action="Remove", can_apply=True),
                apply=lambda op: Result(),
            )
            a.write(None, spec)

        a.register(
            remove,
            Meta(
                summary="Remove a note",
                effects=[CHANGE],
                output="note_write",
                examples=["notes note remove --dry-run", "notes note remove --confirm <token>"],
            ),
        )
        return a

    h.build = build
    r = h.run("note", "remove", "--dry-run")
    assert r.code() == "E_UNKNOWN" and r.details()["dispatch_state"] == "not_started", r
    assert message(r) == "The write target could not be resolved; nothing was sent.", r
    assert "lookup exploded" in r.details()["cause"], r
    mode[0] = "empty"
    r = h.run("note", "remove", "--dry-run")
    assert message(r) == "WriteSpec.resolve_target returned a target without a key.", r
    mode[0] = "none"
    r = h.run("note", "remove", "--dry-run")
    assert message(r) == (
        "App.write needs WriteSpec.target.key (or resolve_target), preview and apply."
    ), r


def test_q1_resolve_items_runs_inside_the_gate(h: THarness) -> None:
    """BatchSpec.resolve_items is resolved the same way."""
    lookups = [0]

    def build(u: Upstream) -> App:
        a = notes_app(u)
        bulk = note_group(a).commands["bulk-tag"]

        def cb(ids: Any, tag: str) -> None:
            ids_list = list(ids)

            def resolve() -> list[BatchItem]:
                lookups[0] += 1
                items = []
                for nid in ids_list:
                    if u.get(nid) is None:
                        raise aicli.not_found("No note has id " + nid + ".")

                    def apply(op: Op, nid: str = nid) -> Result:
                        u.write(lambda: u.notes.pop(nid, None))
                        return Result()

                    items.append(
                        BatchItem(
                            Target("notes/" + nid, "note"),
                            lambda: Preview(action="Tag", can_apply=True),
                            apply,
                        )
                    )
                return items

            a.write_batch(None, BatchSpec(resolve_items=resolve))

        bulk.callback = cb
        return a

    h.build = build
    args = ("note", "bulk-tag", "--ids", "1,2", "--tag", "x")
    tok = h.must_token(*args)
    r = h.run(*args, "--confirm", tok)
    assert r.exit == 0 and lookups[0] == 2, r
    expect_conflict(h.run(*args, "--confirm", tok), "token_used")
    assert lookups[0] == 2 and h.up.writes() == 2


# ---------------------------------------------------------------- Q2 proxy


def test_q2_proxy_comes_from_app_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """update's opener takes its proxy from App.env, not the process environment:
    upper case first, HTTP_PROXY not under CGI, NO_PROXY and localhost go direct."""
    monkeypatch.setenv("HTTPS_PROXY", "http://process.example:1")
    env: dict[str, str] = {}
    a = App(name="notes", env=lambda k: env.get(k, ""))
    assert app_proxies(a) == ({}, ""), "the process environment leaked in"
    env["HTTPS_PROXY"] = "http://proxy.example:3128"
    env["https_proxy"] = "http://lower.example:8080"
    assert app_proxies(a) == ({"https": "http://proxy.example:3128"}, "")
    env.clear()
    env.update({"https_proxy": "http://lower.example:8080", "http_proxy": "http://plain.example"})
    env["no_proxy"] = "gitlab.example"
    assert app_proxies(a) == (
        {"https": "http://lower.example:8080", "http": "http://plain.example"},
        "gitlab.example",
    )
    env["REQUEST_METHOD"] = "GET"  # CGI: HTTP_PROXY is not used
    assert app_proxies(a)[0] == {"https": "http://lower.example:8080"}

    handler = _AppProxyHandler({"https": "http://proxy.example:3128"}, "gitlab.example")

    def via(url: str) -> str:
        req = urllib.request.Request(url)
        handler.proxy_open(req, "http://proxy.example:3128", "https")
        return req.host

    assert via("https://other.example/api") == "proxy.example:3128"
    assert via("https://gitlab.example/api") == "gitlab.example", "NO_PROXY goes direct"
    assert via("https://localhost:8443/api") == "localhost:8443", "localhost goes direct"
    assert via("https://127.0.0.1/api") == "127.0.0.1"


# ---------------------------------------------------------------- Q3 profiles


def test_q3_default_profile_and_profiles(h: THarness) -> None:
    """set_profile_settings makes the profile it writes the default when the file has
    none and keeps the file's order; profiles() lists them in file order."""
    h.build = settings_app
    a = h.app()
    path = os.path.join(h.state, "settings.json")
    assert a.profiles() == []
    a.set_profile_settings("default", {"host": "https://a.example"})
    a.set_profile_settings("work", {"page_size": "50", "host": "https://w.example"})
    want = (
        '{\n  "default_profile": "default",\n  "profiles": {\n    "default": {\n'
        '      "host": "https://a.example"\n    },\n    "work": {\n'
        '      "host": "https://w.example",\n      "page_size": "50"\n    }\n  }\n}\n'
    )
    with open(path, "rb") as fh:
        assert fh.read().decode("utf-8") == want
    assert a.profiles() == ["default", "work"]
    # a file written by hand keeps its order; a profile without a default gets one
    settings_file(
        h, '{"profiles": {"zeta": {"page_size": 5}, "alpha": {"host": "https://al.example"}}}', path
    )
    assert a.profiles() == ["zeta", "alpha"]
    a.set_profile_settings("alpha", {"page_size": "7"})
    want = (
        '{\n  "default_profile": "alpha",\n  "profiles": {\n    "zeta": {\n'
        '      "page_size": 5\n    },\n    "alpha": {\n      "host": "https://al.example",\n'
        '      "page_size": "7"\n    }\n  }\n}\n'
    )
    with open(path, "rb") as fh:
        assert fh.read().decode("utf-8") == want
    settings_file(h, '{"profiles": {"Bad Name": {}}}', path)
    assert a.profiles() == []


# ---------------------------------------------------------------- Q4 allow_unknown_profile


def test_q4_allow_unknown_profile(h: THarness) -> None:
    """A command declared with Meta.allow_unknown_profile runs under a profile the
    settings file does not have (auth logout after a deleted one)."""

    def build(u: Upstream) -> App:
        a = settings_app(u)
        a.schema("logout_result", SchemaDef(shape="object", fields=["profile", "source"]))

        @a.root.command("logout")  # type: ignore[union-attr]
        def logout() -> None:
            name, source = a.profile()
            a.ok(None, {"profile": name, "source": source})

        a.register(
            logout,
            Meta(
                summary="Forget a profile's credentials",
                effects=[OBSERVE],
                output="logout_result",
                allow_unknown_profile=True,
                examples=["notes logout --profile gone"],
            ),
        )
        return a

    h.build = build
    settings_file(h, {"profiles": {"work": {"host": "https://w.example"}}})
    r = h.run("logout", "--profile", "gone")
    assert r.exit == 0 and r.data() == {"profile": "gone", "source": "flag"}, r
    assert h.run("probe", "--name", "host", "--profile", "gone").code() == "E_CONFIG"


# ---------------------------------------------------------------- Q5 default fields


def test_q5_reference_shows_the_commands_default_fields(h: THarness) -> None:
    """A command whose Meta.default_fields differ from its schema's shows them in
    reference (a copy of the schema under <output>_for_<path>) and in help."""

    def build(u: Upstream) -> App:
        a = notes_app(u)

        @note_group(a).command("search")
        def search() -> None:
            a.list(None, Page(items=[]))

        a.register(
            search,
            Meta(
                summary="Search notes",
                effects=[OBSERVE],
                output="note_page",
                default_fields=["ref", "title"],
                examples=["notes note search"],
            ),
        )
        return a

    h.build = build
    one = h.run("reference", "--command", "note search")
    entry, schemas = one.data()["command"], one.data()["schemas"]
    own = schemas["note_page_for_note_search"]
    assert one.exit == 0 and entry["output"] == "note_page_for_note_search", one
    assert own == {
        "shape": "page",
        "fields": note_fields(),
        "default_fields": ["ref", "title"],
        "untrusted_fields": ["title", "body"],
    }, own
    V.validate_reference_command(one.data())
    full = h.run("reference")
    V.validate_reference_full(full.data())
    assert "note_page_for_note_search" in full.data()["schemas"]
    assert full.data()["schemas"]["note_page"]["default_fields"] == ["ref", "title", "tags"]
    listing = h.run("reference", "--command", "note list")
    assert listing.data()["command"]["output"] == "note_page", listing
    assert "Fields: ref, title (default)" in h.run("note", "search", "--help").stdout


# ---------------------------------------------------------------- Q6 shortcuts


def test_q6_root_shortcuts_never_collide() -> None:
    """A shortcut word maps to one path; a nested noun under several parents needs
    no root shortcut, and a qualified word serves one under a single parent."""
    a = notes_app(Upstream())
    a.add_shortcut("comment", "note", "comment")  # the same mapping again is harmless
    a.add_shortcut("talk", "note", "comment")
    a.add_shortcut("talk", "note", "list")
    assert a.shortcuts["talk"] == ["note", "comment"], "the first mapping stays"
    problems = "\n".join(a.check_tree())
    assert (
        'App.add_shortcut("talk") maps it to "note list", but it already maps to "note comment"; '
        "a shortcut word names one path (the first is kept)"
    ) in problems, problems
    assert 'App.add_shortcut("comment")' not in problems
    # comment under note and under issue: no root word could name both
    b = notes_app(Upstream())
    b.shortcuts = {}
    issue = click.Group("issue")
    b.group(issue, "Work with issues")
    b.root.add_command(issue)  # type: ignore[union-attr]
    comment = click.Group("comment")
    b.group(comment, "Comments on issues")
    issue.add_command(comment)

    @comment.command("list")
    def list_cmd() -> None:
        b.ok(None, {})

    b.schema("comment_page", SchemaDef(shape="object"))
    b.register(
        list_cmd,
        Meta(
            summary="List comments",
            effects=[OBSERVE],
            output="comment_page",
            examples=["notes issue comment list"],
        ),
    )
    problems = "\n".join(b.check_tree())
    assert "not reachable from the root" not in problems, problems
    # a nested noun under one parent still needs one, and a qualified word serves
    c = notes_app(Upstream())
    c.shortcuts = {}
    problems = "\n".join(c.check_tree())
    assert (
        'nested group "note comment" is not reachable from the root; add '
        'App.shortcuts["comment"] (CLI-022)'
    ) in problems, problems
    c.add_shortcut("note-comment", "note", "comment")
    assert "not reachable" not in "\n".join(c.check_tree())


# ---------------------------------------------------------------- Q7 command budget


def test_q7_reference_command_budget(h: THarness) -> None:
    """reference --command has 6,000 characters; a write with many flags fits."""
    assert COMMAND_BUDGET_CHARS == 6000

    def build(u: Upstream) -> App:
        a = notes_app(u)
        create = note_group(a).commands["create"]
        text = "A sentence about this option. " * 4
        for i in range(15):
            create.params.append(click.Option([f"--option-{i:02d}"], default="", help=text))
        return a

    h.build = build
    r = h.run("reference", "--command", "note create")
    n = len(r.stdout.rstrip("\n"))
    assert r.exit == 0 and 4000 < n <= COMMAND_BUDGET_CHARS, (n, r)


# ---------------------------------------------------------------- Q8 open_output


def archive_app(
    persist: bool = False,
    overwrite: bool = False,
    open_path: Callable[[str], str] | None = None,
    abort: bool = False,
) -> Callable[[Upstream], App]:
    """`note archive --output <file>`: streams the notes into the file with
    App.open_output."""

    def build(u: Upstream) -> App:
        a = notes_app(u)

        @note_group(a).command("archive")
        @click.option("--output", default="", help="File to write")
        def archive(output: str) -> None:
            path = os.path.abspath(output)

            def preview() -> Preview:
                return Preview(
                    action="Archive the notes",
                    can_apply=True,
                    outputs=[path],
                    changes=[FieldChange("path", after=path)],
                )

            def apply(op: Op) -> Result:
                target = open_path(path) if open_path is not None else path
                try:
                    f = a.open_output(target, overwrite)
                except aicli.Error as e:
                    raise aicli.not_dispatched(e) from None
                with f:
                    for i in range(3):
                        f.write(f"line {i}\n".encode())
                    if abort:
                        f.abort()
                        raise aicli.not_dispatched(Exception("the archive was abandoned"))
                    try:
                        f.commit()
                    except aicli.Error as e:
                        raise aicli.not_dispatched(e) from None
                return Result(data={"path": f.path, "bytes": f.size, "sha256": f.sha256()})

            a.write(
                None,
                WriteSpec(target=Target("notes", "collection"), preview=preview, apply=apply),
            )

        a.register(
            archive,
            Meta(
                summary="Archive every note to a local file",
                effects=[ARTIFACT, PERSIST] if persist else [ARTIFACT],
                output="export_result",
                examples=[
                    "notes note archive --output notes.txt --dry-run",
                    "notes note archive --output notes.txt --confirm <token>",
                ],
            ),
        )
        return a

    return build


def grant_outputs(h: THarness, d: str) -> None:
    h.grant({"effects": ["change", "deliver", "artifact", "persist"], "outputs": [d], "roots": [d]})


def no_temp_outputs(d: str) -> None:
    left = [n for n in os.listdir(d) if n.startswith(".aicli-output-")]
    assert not left, f"a temporary output file was left: {left}"


def new_harness(tmp_path: Any, name: str) -> THarness:
    """A harness in its own directory under tmp_path."""
    d = os.path.join(str(tmp_path), name)
    os.makedirs(d, exist_ok=True)
    return THarness(d)


def confirmed(h: THarness, *args: str) -> TOut:
    return h.run(*args, "--confirm", h.must_token(*args))


def test_q8_open_output_streams_a_file(tmp_path: Any) -> None:
    """App.open_output streams a file inside apply: commit puts a new file in place,
    overwrites only with overwrite and persist, checks the scope at that moment, and
    abort leaves nothing."""
    d = str(tmp_path / "out")
    os.makedirs(d)
    file = os.path.join(d, "notes.txt")
    h = new_harness(tmp_path, "h1")
    grant_outputs(h, d)
    h.build = archive_app()
    args = ("note", "archive", "--output", file)
    r = confirmed(h, *args)
    res = r.data().get("result", {})
    with open(file, "rb") as fh:
        content = fh.read()
    assert r.exit == 0 and content == b"line 0\nline 1\nline 2\n", r
    assert res["bytes"] == 21 and res["path"] == file, r
    if not WINDOWS:
        assert os.stat(file).st_mode & 0o777 == 0o600
    no_temp_outputs(d)

    # a file that appears before commit: a command that only creates files refuses
    # it (Q9), even when it asked to overwrite
    other = os.path.join(d, "other.txt")
    for i, overwrite in enumerate((False, True)):
        if os.path.exists(other):
            os.remove(other)

        def appear(_: str) -> str:
            with open(other, "w", encoding="utf-8") as fh:
                fh.write("theirs")
            return other

        h2 = new_harness(tmp_path, f"h2{i}")
        grant_outputs(h2, d)
        h2.build = archive_app(overwrite=overwrite, open_path=appear)
        r = confirmed(h2, "note", "archive", "--output", os.path.join(d, "planned.txt"))
        with open(other, encoding="utf-8") as fh:
            assert fh.read() == "theirs"
        assert r.code() == "E_VALIDATION" and r.details()["path"] == other, r
        no_temp_outputs(d)

    # with persist and overwrite, an existing file under the scope's roots is replaced
    h3 = new_harness(tmp_path, "h3")
    grant_outputs(h3, d)
    h3.build = archive_app(persist=True, overwrite=True)
    with open(file, "w", encoding="utf-8") as fh:
        fh.write("old")
    r = confirmed(h3, *args)
    assert r.exit == 0, r
    with open(file, "rb") as fh:
        assert fh.read() == b"line 0\nline 1\nline 2\n"

    # the scope is checked at commit: a path outside the scope's outputs is refused
    outside = str(tmp_path / "elsewhere" / "elsewhere.txt")
    os.makedirs(os.path.dirname(outside))
    h4 = new_harness(tmp_path, "h4")
    grant_outputs(h4, d)
    h4.build = archive_app(open_path=lambda _: outside)
    r = confirmed(h4, "note", "archive", "--output", os.path.join(d, "fine.txt"))
    assert r.code() == "E_FORBIDDEN" and not os.path.exists(outside), r

    # abort removes the temporary file and writes nothing
    h5 = new_harness(tmp_path, "h5")
    grant_outputs(h5, d)
    h5.build = archive_app(abort=True)
    aborted = os.path.join(d, "aborted.txt")
    r = confirmed(h5, "note", "archive", "--output", aborted)
    assert r.exit != 0 and not os.path.exists(aborted), r
    no_temp_outputs(d)


def test_q8_open_output_outside_a_write() -> None:
    """open_output belongs to a write's apply."""
    a = notes_app(Upstream())
    with pytest.raises(aicli.Error) as info:
        a.open_output("x.txt")
    assert info.value.message == (
        "App.open_output must be called from a write's apply, inside App.execute."
    )


# ---------------------------------------------------------------- Q9 create-only


def save_app(produce_hook: Callable[[], None] | None = None) -> Callable[[Upstream], App]:
    """`note save --output <file>` declares artifact only: it never overwrites."""

    def build(u: Upstream) -> App:
        a = notes_app(u)

        @note_group(a).command("save")
        @click.option("--output", default="", help="File to write")
        def save(output: str) -> None:
            def produce() -> bytes:
                if produce_hook is not None:
                    produce_hook()
                return b"saved\n"

            a.write_output(None, OutputSpec(path=output, produce=produce))

        a.register(
            save,
            Meta(
                summary="Save the notes to a new local file",
                effects=[ARTIFACT],
                output="export_result",
                examples=[
                    "notes note save --output notes.txt --dry-run",
                    "notes note save --output notes.txt --confirm <token>",
                ],
            ),
        )
        return a

    return build


def expect_create_only(r: TOut, path: str) -> None:
    d = r.details()
    assert r.code() == "E_VALIDATION" and d["problem"] == "invalid_value", r
    assert d["path"] == path and d["dispatch_state"] == "not_started", r
    assert message(r) == (
        "The output file already exists, and this command never overwrites files; nothing was "
        "written."
    ), r
    assert d["next_action"] == "Choose a new file name, or move the existing file away first."


def test_q9_create_only_command_and_an_existing_file(tmp_path: Any) -> None:
    """An existing output of a command that only creates files is E_VALIDATION, not a
    scope refusal asking for roots, at the dry-run, at the confirm and at write time."""
    d = str(tmp_path / "out")
    os.makedirs(d)
    file = os.path.join(d, "notes.txt")

    def mine() -> None:
        with open(file, "w", encoding="utf-8") as fh:
            fh.write("mine")

    h = new_harness(tmp_path, "h1")
    grant_outputs(h, d)
    h.build = save_app()
    mine()
    args = ("note", "save", "--output", file)
    expect_create_only(h.run(*args, "--dry-run"), file)
    # the file appears between the dry-run and the confirm
    os.remove(file)
    tok = h.must_token(*args)
    mine()
    expect_create_only(h.run(*args, "--confirm", tok), file)
    # the file appears while the content is produced
    os.remove(file)
    h2 = new_harness(tmp_path, "h2")
    grant_outputs(h2, d)
    h2.build = save_app(mine)
    expect_create_only(confirmed(h2, *args), file)
    with open(file, encoding="utf-8") as fh:
        assert fh.read() == "mine"
    # a scope refusal for another reason does not ask for roots for it
    h3 = new_harness(tmp_path, "h3")
    h3.build = save_app()  # the default scope grants no artifact
    r = h3.run(*args, "--dry-run")
    missing = r.details()["scope"]["missing"]
    assert r.code() == "E_FORBIDDEN" and "effects" in missing and "roots" not in missing, r


# ---------------------------------------------------------------- Q10 renamed flags


def test_q10_renamed_flags(h: THarness) -> None:
    """Meta.renamed_flags keeps an old flag name working: it binds as the new name,
    adds the deprecated notice, refuses both names together, is listed in reference
    as a deprecated parameter and left out of help."""

    def build(u: Upstream) -> App:
        a = notes_app(u)
        node_meta(a, "note list").renamed_flags = {"label": "tag"}
        node_meta(a, "note update").renamed_flags = {"name": "title"}
        return a

    h.build = build
    r = h.run("note", "list", "--label", "seed")
    n = notice_of_type(r, "deprecated")
    assert r.exit == 0 and r.data()["count"] == 3, r
    assert n is not None and n["message"] == "`--label` is deprecated: use `--tag`.", r
    r = h.run("note", "list", "--label", "seed", "--tag", "seed")
    d = r.details()
    assert r.code() == "E_USAGE" and d["problem"] == "conflicting_flags", r
    assert d["parameter"] == "--label" and d["next_action"] == "Drop --label and keep --tag.", r
    assert message(r) == "--label is the old name of --tag; give only one of them.", r
    ref = h.run("reference", "--command", "note list")
    V.validate_reference_command(ref.data())
    label = [p for p in ref.data()["command"]["params"] if p["name"] == "label"]
    assert label and label[0]["description"] == (
        "Deprecated: use `--tag`. Only notes with this tag"
    ), label
    assert label[0]["type"] == "string" and label[0]["required"] is False
    assert "default" not in label[0], label
    assert "--label" not in h.run("note", "list", "--help").stdout
    # the old name binds as the new one
    tok = h.must_token("note", "update", "1", "--name", "Renamed")
    r = h.run("note", "update", "1", "--title", "Renamed", "--confirm", tok)
    assert r.exit == 0, r
    # the tree check reports a rename it cannot apply
    a = notes_app(Upstream())
    node_meta(a, "note get").renamed_flags = {"ident": "nope", "id": "title"}
    problems = "\n".join(a.check_tree())
    for want in (
        '"note get": Meta.renamed_flags maps --ident to --nope, which the command does not define',
        '"note get": Meta.renamed_flags maps --id to --title, which the command does not define',
    ):
        assert want in problems, problems
    a = notes_app(Upstream())
    node_meta(a, "note list").renamed_flags = {"limit": "tag"}
    assert (
        '"note list": Meta.renamed_flags names --limit, which the command still defines; keep '
        "only --tag"
    ) in "\n".join(a.check_tree())


# ---------------------------------------------------------------- Q11 flag aliases


def test_q11_per_command_flag_aliases(h: THarness) -> None:
    """Meta.flag_aliases accepts an alias on its command only, after
    App.flag_aliases."""

    def build(u: Upstream) -> App:
        a = notes_app(u)
        node_meta(a, "note list").flag_aliases = {"max": "limit"}
        node_meta(a, "note update").flag_aliases = {"subject": "body"}
        return a

    h.build = build
    r = h.run("note", "list", "--max", "1")
    assert r.exit == 0 and r.data()["count"] == 1, r
    r = h.run("note", "get", "1", "--max", "1")
    assert r.code() == "E_USAGE" and r.details()["problem"] == "unknown_flag", r
    # App.flag_aliases (subject -> title) applies first
    r = h.run("note", "update", "1", "--subject", "New", "--dry-run")
    changes = r.data()["preview"]["changes"]
    assert r.exit == 0 and [c["field"] for c in changes] == ["title"], r
    a = notes_app(Upstream())
    node_meta(a, "note get").flag_aliases = {"x": "nope", "id": "id"}
    problems = "\n".join(a.check_tree())
    for want in (
        '"note get": Meta.flag_aliases maps --x to --nope, which the command does not define',
        '"note get": Meta.flag_aliases names --id, which the command already defines',
    ):
        assert want in problems, problems


# ---------------------------------------------------------------- Q12 apply_all


def bulk_all_app(max_per_call: int, mode: list[str], calls: list[int]) -> Callable[[Upstream], App]:
    """note bulk-tag sending its items through BatchSpec.apply_all in calls of at
    most max_per_call items; mode picks the call's answer."""

    def build(u: Upstream) -> App:
        a = notes_app(u)
        bulk = note_group(a).commands["bulk-tag"]

        def cb(ids: Any, tag: str) -> None:
            items = [
                BatchItem(
                    Target("notes/" + nid, "note"), lambda: Preview(action="Tag", can_apply=True)
                )
                for nid in ids
            ]

            def apply_all(ops: list[Op]) -> list[ItemOutcome]:
                calls.append(len(ops))
                if mode[0] == "not sent":
                    raise aicli.not_dispatched(Exception("the queue is full"))
                if mode[0] == "rejected":
                    raise aicli.rejected(aicli.from_http(422, "Refused."))
                if mode[0] == "lost":
                    u.write(lambda: None)
                    raise ConnectionResetError("the connection was reset")
                outs = []
                for op in ops:
                    u.write(lambda: None)
                    outs.append(ItemOutcome(result=Result(data={"id": op.id})))
                if mode[0] == "short":
                    outs = outs[:-1]
                return outs

            a.write_batch(
                None, BatchSpec(items=items, max_per_call=max_per_call, apply_all=apply_all)
            )

        bulk.callback = cb
        return a

    return build


def test_q12_native_bulk_calls(tmp_path: Any) -> None:
    """BatchSpec.apply_all sends the items in chunks of max_per_call, one call per
    chunk, and maps each outcome onto its item."""
    mode, calls = ["ok"], []
    h = new_harness(tmp_path, "ok")
    h.build = bulk_all_app(2, mode, calls)
    with h.up.mu:
        h.up.notes["4"] = TNote("4", "notes/4", "Four", "", [], 1, "2026-10-01T00:00:00Z")
        h.up.notes["5"] = TNote("5", "notes/5", "Five", "", [], 1, "2026-10-01T00:00:00Z")
    r = confirmed(h, "note", "bulk-tag", "--ids", "1,2,3,4,5", "--tag", "x")
    items = r.data()["items"]
    assert r.exit == 0 and calls == [2, 2, 1] and len(items) == 5 and h.up.writes() == 5, r
    for it in items:
        assert it["ok"] is True and it["result"]["id"] == it["operation_id"], it
        assert it["receipt"]["operation_id"] == it["operation_id"], it
    for i, (m, code, key, dispatch) in enumerate(
        (
            ("short", "E_OUTCOME_UNKNOWN", "unknown", "unknown"),
            ("lost", "E_OUTCOME_UNKNOWN", "unknown", "unknown"),
            ("not sent", "", "failed", "not_started"),
            ("rejected", "", "failed", "dispatched"),
        )
    ):
        mode[0] = m
        calls.clear()
        hm = new_harness(tmp_path, f"m{i}")
        hm.build = bulk_all_app(0, mode, calls)
        r = confirmed(hm, "note", "bulk-tag", "--ids", "1,2", "--tag", "y")
        assert r.exit != 0 and calls == [2] and r.details()["summary"][key] == 2, (m, r)
        assert r.details()["dispatch_state"] == dispatch and (not code or r.code() == code), r
    # a call that returns the wrong number of outcomes is a defect
    mode[0] = "short"
    h2 = new_harness(tmp_path, "defect")
    h2.build = bulk_all_app(0, mode, calls)
    short = ["note", "bulk-tag", "--ids", "1,2", "--tag", "z"]
    tok = h2.must_token(*short)
    a = h2.app()
    a.stdout, a.stderr = io.BytesIO(), io.BytesIO()
    a.execute([*short, "--confirm", tok])
    assert (
        "BatchSpec.apply_all returned 1 outcomes for 2 items; every item of that call is "
        "reported unknown."
    ) in a.defects, a.defects


# ---------------------------------------------------------------- Q13 dangerous plans


def wipe_app(dangerous: list[bool]) -> Callable[[Upstream], App]:
    """`note wipe <id>`: a command that is not dangerous whose plan is when
    dangerous[0] holds (a commit that deletes a file)."""

    def build(u: Upstream) -> App:
        a = notes_app(u)

        @note_group(a).command("wipe")
        @click.option("--id", "nid", default="", help="Note id")
        def wipe(nid: str) -> None:
            def apply(op: Op) -> Result:
                u.write(lambda: setattr(u.notes[nid], "body", ""))
                return Result()

            a.write(
                None,
                WriteSpec(
                    target=Target("notes/" + nid, "note"),
                    preview=lambda: Preview(
                        action="Clear note " + nid,
                        can_apply=True,
                        dangerous=dangerous[0],
                        changes=[FieldChange("body", after="")],
                    ),
                    apply=apply,
                ),
            )

        a.register(
            wipe,
            Meta(
                summary="Clear a note's body",
                effects=[CHANGE],
                output="note_write",
                positional="id",
                examples=["notes note wipe 1 --dry-run", "notes note wipe 1 --confirm <token>"],
            ),
        )
        return a

    return build


def test_q13_a_plan_can_be_dangerous(h: THarness) -> None:
    """Preview.dangerous makes one plan dangerous: --dangerous in both steps, the
    preview says so, the scope must grant dangerous, and a plan whose danger changes
    is state_drift."""
    dangerous = [True]
    h.build = wipe_app(dangerous)
    r = h.run("note", "wipe", "1", "--dry-run")
    d = r.details()
    assert r.code() == "E_CONFIRMATION_REQUIRED", r
    assert d["problem"] == "dangerous_flag_required" and d["preview"]["dangerous"] is True, r
    assert d["next_action"] == (
        "details.preview shows the plan; run the same command again with --dangerous added to "
        "get its confirm token (keep --dry-run if you used it)."
    ), r
    dry = h.run("note", "wipe", "1", "--dangerous", "--dry-run")
    assert dry.token() and dry.data()["preview"]["dangerous"] is True, dry
    r = h.run("note", "wipe", "1", "--confirm", dry.token())
    assert r.details().get("problem") == "dangerous_flag_required" and h.up.writes() == 0, r
    r = h.run("note", "wipe", "1", "--dangerous", "--confirm", dry.token())
    assert r.exit == 0 and h.up.writes() == 1, r
    # the scope must grant dangerous
    h.grant({"dangerous": False})
    r = h.run("note", "wipe", "2", "--dangerous", "--dry-run")
    assert r.code() == "E_FORBIDDEN", r
    assert r.details()["scope"]["missing"]["dangerous"] is True, r
    h.grant(None)
    # a plan that becomes dangerous after the dry-run is another plan
    dangerous[0] = False
    tok = h.must_token("note", "wipe", "2")
    dangerous[0] = True
    r = h.run("note", "wipe", "2", "--confirm", tok)
    expect_conflict(r, "state_drift")
    assert message(r) == (
        "The target changed since the preview (it is now dangerous); nothing was sent."
    ), r
    assert h.up.writes() == 1
    ref = h.run("reference", "--command", "note wipe")
    assert ref.data()["command"]["dangerous"] is False, ref


# ---------------------------------------------------------------- Q14 output format


def test_q14_output_format(h: THarness) -> None:
    """output_format tells a command the format of this run."""

    def build(u: Upstream) -> App:
        a = notes_app(u)

        @a.root.command("show-format")  # type: ignore[union-attr]
        def show_format() -> None:
            f = aicli.output_format()
            if f == "raw":
                a.ok(None, "raw:" + f)
            else:
                a.ok(None, {"format": f})

        a.schema("format_result", SchemaDef(shape="object", fields=["format"]))
        a.register(
            show_format,
            Meta(
                summary="Show the output format",
                effects=[OBSERVE],
                output="format_result",
                raw_output=True,
                examples=["notes show-format"],
            ),
        )
        return a

    h.build = build
    for args, want in (
        ((), "json"),
        (("--json",), "json"),
        (("--format", "text"), "format: text\n"),
        (("--format", "raw"), "raw:raw"),
    ):
        r = h.run("show-format", *args)
        got = r.data()["format"] if r.env is not None else r.stdout
        assert r.exit == 0 and got == want, (args, r)
    assert aicli.output_format() == "json", "outside a run"


# ---------------------------------------------------------------- Q15 durations and times


def test_q15_parse_duration_and_time() -> None:
    """parse_duration and parse_time_arg read CLI-028's forms."""
    td = _dt.timedelta
    for s, want in (
        ("500ms", td(milliseconds=500)),
        ("30s", td(seconds=30)),
        ("5m", td(minutes=5)),
        ("2h", td(hours=2)),
        ("7d", td(days=7)),
        ("0s", td(0)),
    ):
        assert aicli.parse_duration(s) == want, s
    for s in ("", "1m30s", "1.5h", "-5m", "5", "5w", "99999999999999999999d"):
        with pytest.raises(ValueError):
            aicli.parse_duration(s)
    with pytest.raises(ValueError) as info:
        aicli.parse_duration("5w")
    assert str(info.value) == (
        '"5w" is not a duration: use a whole number and one unit, ms, s, m, h or d (500ms, 30s, '
        "5m, 2h, 7d)"
    )
    with pytest.raises(ValueError) as info:
        aicli.parse_duration("99999999999999999999d")
    assert str(info.value) == '"99999999999999999999d" is too long a duration'
    now = _dt.datetime(2026, 10, 14, 12, 0, 0, tzinfo=_dt.timezone.utc)
    for s, want in (
        ("7d", "2026-10-07T12:00:00Z"),
        ("30m", "2026-10-14T11:30:00Z"),
        ("2026-06-01", "2026-06-01T00:00:00Z"),
        ("2026-06-01T08:00:00Z", "2026-06-01T08:00:00Z"),
        ("2026-06-01T10:00:00+02:00", "2026-06-01T08:00:00Z"),
    ):
        got = aicli.parse_time_arg(s, now)
        assert time_string(got) == want and got.utcoffset() == td(0), (s, got)
    with pytest.raises(ValueError) as info:
        aicli.parse_time_arg("last week", now)
    assert str(info.value) == (
        '"last week" is not a time: use an ISO 8601 date (2026-06-01), a date and time '
        "(2026-06-01T08:00:00Z) or a time ago (7d, 24h, 30m)"
    )


# ---------------------------------------------------------------- Q16 write results


def test_q16_write_result_uses_default_fields(h: THarness) -> None:
    """A confirmed write's result takes its schema's default fields without
    --fields."""

    def build(u: Upstream) -> App:
        a = notes_app(u)
        a.schema(
            "note_brief",
            SchemaDef(shape="object", fields=note_fields(), default_fields=["ref", "title"]),
        )
        node_meta(a, "note update").output = "note_brief"
        return a

    h.build = build
    for fields, want in (
        ("", {"ref", "title"}),
        ("+body", {"body", "ref", "title"}),
        ("all", set(note_fields())),
        ("id", {"id", "ref"}),
    ):
        args = ("note", "update", "1", "--title", "T" + fields)
        extra = ("--fields", fields) if fields else ()
        r = h.run(*args, "--confirm", h.must_token(*args), *extra)
        assert r.exit == 0 and set(r.data()["result"]) == want, (fields, r)
        assert r.data()["receipt"]["outcome"], r


# ---------------------------------------------------------------- Q17 wants_field


def test_q17_wants_field(h: THarness) -> None:
    """wants_field answers whether the response keeps a field."""

    def build(u: Upstream) -> App:
        a = notes_app(u)

        @note_group(a).command("peek")
        def peek() -> None:
            wants = {k: aicli.wants_field(None, k) for k in ("body", "title", "ref")}
            a.list(None, Page(items=[], scope={"wants": wants}))

        a.register(
            peek,
            Meta(
                summary="Peek at notes",
                effects=[OBSERVE],
                output="note_page",
                examples=["notes note peek"],
            ),
        )
        return a

    h.build = build
    for fields, want in (
        ("", {"body": False, "ref": True, "title": True}),
        ("body", {"body": True, "ref": True, "title": False}),
        ("+body", {"body": True, "ref": True, "title": True}),
        ("all", {"body": True, "ref": True, "title": True}),
    ):
        extra = ("--fields", fields) if fields else ()
        r = h.run("note", "peek", *extra)
        assert r.exit == 0 and r.data()["scope"]["wants"] == want, (fields, r)
    assert aicli.wants_field(None, "anything"), "outside a run every field is wanted"


# ---------------------------------------------------------------- Q18 schemas once


def test_q18_schema_registered_twice() -> None:
    """A schema name registered twice is a defect; the first definition stays."""
    a = notes_app(Upstream())
    a.schema("note", SchemaDef(shape="page"))
    assert a._reg.schemas["note"].shape == "object", "the second definition replaced the first"
    assert (
        'App.schema("note") was called twice; register each schema name once (the first '
        "definition is kept)"
    ) in "\n".join(a.check_tree())


# ---------------------------------------------------------------- Q19 batch action


@pytest.mark.parametrize(
    ("action", "want"), [("Tag the notes", "Tag the notes"), ("", "Tag notes (2 targets)")]
)
def test_q19_batch_action(h: THarness, action: str, want: str) -> None:
    """BatchSpec.action is the batch preview's action; empty means the first item's
    with the number of targets."""

    def build(u: Upstream) -> App:
        a = notes_app(u)
        bulk = note_group(a).commands["bulk-tag"]

        def cb(ids: Any, tag: str) -> None:
            items = [
                BatchItem(
                    Target("notes/" + nid),
                    lambda: Preview(action="Tag notes", can_apply=True),
                    lambda op: Result(),
                )
                for nid in ids
            ]
            a.write_batch(None, BatchSpec(items=items, action=action))

        bulk.callback = cb
        return a

    h.build = build
    r = h.run("note", "bulk-tag", "--ids", "1,2", "--tag", "x", "--dry-run")
    assert r.data()["preview"]["action"] == want, r


# ---------------------------------------------------------------- Q20 untrusted


def test_q20_untrusted_follows_the_projection(h: THarness) -> None:
    """meta.untrusted names only paths the projected response still has."""

    def untrusted(r: TOut) -> Any:
        return (r.env or {}).get("meta", {}).get("untrusted")

    assert untrusted(h.run("note", "list")) == ["data.items[].title"]
    assert untrusted(h.run("note", "list", "--fields", "all")) == [
        "data.items[].body",
        "data.items[].title",
    ]
    assert untrusted(h.run("note", "list", "--tag", "none")) is None, "an empty page"
    args = ("note", "create", "--title", "Q20")
    r = h.run(*args, "--confirm", h.must_token(*args), "--fields", "id")
    u = untrusted(r) or []
    assert r.exit == 0 and "data.result.title" not in u and "data.receipt.observed" in u, r


# ---------------------------------------------------------------- Q21 examples


def test_q21_examples_with_shortcuts_and_aliases() -> None:
    """An example written with a root shortcut or an alias passes the tree check and
    R01, which resolve it as the parser does; an example of another command fails
    both."""

    def build(u: Upstream) -> App:
        a = notes_app(u)
        node_meta(a, "note comment add").examples = [
            "notes comment add 1 --text Hi --dry-run",
            "notes comment create 1 --text Hi --confirm <token>",
        ]
        node_meta(a, "note delete").examples = [
            "notes note rm 1 --dangerous --dry-run",
            "notes notes delete 1 --dangerous --confirm <token>",
        ]
        return a

    assert build(Upstream()).check_tree() == []
    harness, _ = harness_for(build)
    rec = Recorder()
    aicli.run_conformance(harness, rec)
    bad = [e for e in rec.errors + rec.fatals if e.startswith(("R01", "R02"))]
    assert not bad, bad
    a = notes_app(Upstream())
    node_meta(a, "note get").examples = ["notes note list"]
    assert (
        '"note get": example "notes note list" does not run this command; start it with "notes" '
        "and the command's path, a shortcut or an alias"
    ) in "\n".join(a.check_tree())


# ---------------------------------------------------------------- Q22 debug lock


def test_q22_debug_from_several_threads(h: THarness) -> None:
    """debug, add_secret and notice are safe from several threads: every line whole."""

    def build(u: Upstream) -> App:
        a = notes_app(u)

        @a.root.command("fan")  # type: ignore[union-attr]
        def fan() -> None:
            def work(g: int) -> None:
                for i in range(25):
                    a.debug(f"fan {g}.{i}")
                    a.add_secret(f"secret-{g}")
                    a.notice(Notice(type="fan", severity="info", message=f"thread {g}"))

            threads = [threading.Thread(target=work, args=(g,)) for g in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            a.ok(None, {})

        a.schema("fan_result", SchemaDef(shape="object"))
        a.register(
            fan,
            Meta(
                summary="Fan out",
                effects=[OBSERVE],
                output="fan_result",
                examples=["notes fan"],
            ),
        )
        return a

    h.build = build
    h.env["NOTES_DEBUG"] = "1"
    r = h.run("fan")
    lines = [line for line in r.stderr.splitlines() if line.startswith("debug: fan ")]
    notices = (r.env or {}).get("meta", {}).get("notices", [])
    assert r.exit == 0 and len(lines) == 200 and len(notices) == 8, r


# ---------------------------------------------------------------- Q23 streams


def log_app(fail_after: int, raw_text: bool = True) -> Callable[[Upstream], App]:
    """`note log`: a stream of lines whose items have their own schema
    (Meta.stream_item), and raw output (the lines as text, or the objects when
    raw_text is false); it fails after fail_after lines when set."""

    def build(u: Upstream) -> App:
        a = notes_app(u)

        @note_group(a).command("log")
        def log_cmd() -> None:
            s = a.stream(None)
            for i, line in enumerate(["a", "b", "c"]):
                if fail_after > 0 and i == fail_after:
                    raise aicli.from_http(503, "The log stopped.")
                raw = raw_text and aicli.output_format() == "raw"
                s.item(line + "\n" if raw else {"line": line})

        a.schema("note_log", SchemaDef(shape="object", fields=["log", "lines"], untrusted=["log"]))
        a.schema("log_line", SchemaDef(shape="object", fields=["line"], untrusted=["line"]))
        a.register(
            log_cmd,
            Meta(
                summary="Stream the notes' log as NDJSON, one line per entry",
                effects=[OBSERVE],
                output="note_log",
                stream_item="log_line",
                raw_output=True,
                examples=["notes note log"],
            ),
        )
        return a

    return build


def test_q23_raw_streams_and_item_schemas(tmp_path: Any) -> None:
    """Stream items take their untrusted paths from the item schema; a raw stream
    writes the bytes only, never a JSON summary line."""
    h = new_harness(tmp_path, "ok")
    h.build = log_app(0)
    r = h.run("note", "log")
    assert r.exit == 0 and len(r.lines) == 4, r
    for line in r.lines[:3]:
        assert line["meta"]["untrusted"] == ["data.line"], line
    raw = h.run("note", "log", "--format", "raw")
    assert raw.exit == 0 and raw.stdout == "a\nb\nc\n", raw
    h2 = new_harness(tmp_path, "fail")
    h2.build = log_app(1)
    raw = h2.run("note", "log", "--format", "raw")
    assert raw.exit != 0 and raw.stdout == "a\n" and raw.stderr.startswith("error "), raw
    a = notes_app(Upstream())
    node_meta(a, "note tail").stream_item = "nope"
    assert '"note tail" names stream item schema "nope", which is not registered' in "\n".join(
        a.check_tree()
    )
    # a non-bytes item in raw mode is one compact JSON line
    h3 = new_harness(tmp_path, "json")
    h3.build = log_app(0, raw_text=False)
    app = h3.app()
    out, err = io.BytesIO(), io.BytesIO()
    app.stdout, app.stderr = out, err
    assert app.execute(["note", "log", "--format", "raw"]) == 0, err.getvalue()
    assert out.getvalue() == b'{"line":"a"}\n{"line":"b"}\n{"line":"c"}\n', out.getvalue()


# ---------------------------------------------------------------- Q24 budgets


def test_q24_the_success_budget_is_r04s() -> None:
    """validate_envelope no longer holds a success to the success budget; R04 does,
    for the suite's default invocations."""
    big = {
        "ok": True,
        "schema_version": aicli.SCHEMA_VERSION,
        "data": {"text": "x" * 9000},
        "meta": {"duration_ms": 1},
    }
    raw = (json.dumps(big, separators=(",", ":")) + "\n").encode("utf-8")
    aicli.validate_envelope(raw)
    size, over = over_success_budget(big)
    assert over and size > SUCCESS_BUDGET_CHARS
    assert not default_invocation(["note", "list", "--limit", "100"])
    assert not default_invocation(["note", "list", "--fields=all"])
    assert default_invocation(["note", "list", "--tag", "x"])
    harness, _ = harness_for()
    harness.samples["note create"] = ["--title", "Hello", "--body", "long body " * 1000]
    rec = Recorder()
    aicli.run_conformance(harness, rec)
    assert any("over the 8000 budget of a default invocation (CLI-042)" in e for e in rec.errors), (
        rec.errors
    )


# ---------------------------------------------------------------- Q25 E_CONFIG


def test_q25_config_error_in_error_codes(h: THarness, tmp_path: Any) -> None:
    """With settings declared, every tool command's error codes include E_CONFIG."""

    def codes(hh: THarness, path: str) -> list[str]:
        r = hh.run("reference", "--command", path)
        return r.data()["command"]["error_codes"]

    h.build = settings_app
    assert "E_CONFIG" in codes(h, "probe") and "E_CONFIG" in codes(h, "note get")
    assert "E_CONFIG" not in codes(h, "context"), "a built-in"
    plain = new_harness(tmp_path, "plain")
    assert "E_CONFIG" not in codes(plain, "note get"), "no settings, no E_CONFIG"


# ---------------------------------------------------------------- Q26 set_resolved


def test_q26_set_resolved_for_a_part_of_another_input(h: THarness) -> None:
    """set_resolved sets a flag the user did not give when its value came inside
    another input: inbox/1 and 1 --box inbox bind alike."""

    def build(u: Upstream) -> App:
        a = notes_app(u)

        @note_group(a).command("file")
        @click.option("--id", "nid", default="", help="Note id, or box/id")
        @click.option("--box", default="", help="Box")
        def file_cmd(nid: str, box: str) -> None:
            if "/" in nid:
                box, nid = nid.split("/", 1)
                aicli.set_resolved(None, "box", box)
                aicli.set_resolved(None, "id", nid)

            def apply(op: Op) -> Result:
                u.write(lambda: None)
                return Result()

            a.write(
                None,
                WriteSpec(
                    target=Target(f"boxes/{box}/notes/{nid}"),
                    preview=lambda: Preview(
                        action="File note " + nid,
                        can_apply=True,
                        changes=[FieldChange("box", after=box)],
                    ),
                    apply=apply,
                ),
            )

        a.register(
            file_cmd,
            Meta(
                summary="File a note in a box",
                effects=[CHANGE],
                output="note_write",
                positional="id",
                examples=[
                    "notes note file inbox/1 --dry-run",
                    "notes note file inbox/1 --confirm <token>",
                ],
            ),
        )
        return a

    h.build = build
    dry = h.run("note", "file", "inbox/1", "--dry-run")
    inputs = dry.data()["preview"]["inputs"]
    assert dry.exit == 0 and inputs["box"] == "inbox" and inputs["id"] == "1", dry
    r = h.run("note", "file", "1", "--box", "inbox", "--confirm", dry.token())
    assert r.exit == 0 and h.up.writes() == 1, r


def test_q26_set_resolved_doc() -> None:
    doc = App.set_resolved.__doc__ or ""
    assert "acme/web!5" in doc and "never a ref that embeds both" in doc
    assert time_string(_dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)) == "2026-01-01T00:00:00Z"
