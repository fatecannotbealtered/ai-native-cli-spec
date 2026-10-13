"""Regression tests for round 2 of the fixes before v2.0.0-rc.1 (FIXES-RC1.md R2),
found by migrating gitlab-cli to the library: one or more per item, named after it
(port of rc1_r2_test.go). P8 (G03 never adds --profile) is covered by the
conformance run of a tool with settings; P14's update refusal is tested in
test_update.py, next to its fake release server."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from typing import Any

import click
import pytest
from aicli_testapp import (
    THarness,
    TNote,
    Upstream,
    expect_conflict,
    expect_mismatch,
    notes_app,
)
from test_conformance import EXPORT_DIR, harness_for
from test_settings import settings_app, settings_file
from test_update import FakeGitHub

import aicli
from aicli import (
    CHANGE,
    CONFIGURE,
    OBSERVE,
    App,
    BatchItem,
    BatchSpec,
    Block,
    FieldChange,
    Meta,
    Op,
    ParamDoc,
    Preview,
    Recorder,
    Result,
    SchemaDef,
    Setting,
    Target,
    WriteSpec,
)
from aicli import validate as V
from aicli.builtins import CHANGELOG_TRUNCATED, dev_version
from aicli.conformance import mutate_value
from aicli.contract_gen import SUCCESS_BUDGET_CHARS

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


# ---------------------------------------------------------------- P1 aliases


def test_p1_aliases_are_per_parent(h: THarness) -> None:
    """label list and milestone list may both have the alias ls. An alias still may
    not take a sibling's name or alias, and a root alias may not take a root
    command's or a built-in's name."""

    def build(u: Upstream) -> App:
        a = notes_app(u)
        a.schema("noun_result", SchemaDef(shape="object", fields=["noun"]))
        for noun in ("label", "milestone"):
            group = click.Group(noun)
            a.group(group, f"Work with {noun}s")
            a.root.add_command(group)

            def run(noun: str = noun) -> None:
                a.ok(None, {"noun": noun})

            cmd = click.Command("list", callback=run)
            group.add_command(cmd)
            a.register(
                cmd,
                Meta(
                    summary=f"List {noun}s",
                    effects=[OBSERVE],
                    output="noun_result",
                    aliases=["ls"],
                    examples=[f"notes {noun} list"],
                ),
            )
        return a

    h.build = build
    ref = h.run("reference")
    assert ref.exit == 0, ref
    V.validate_reference_full(ref.data())  # two groups with the alias ls validate
    r = h.run("milestone", "ls")
    assert r.exit == 0 and r.data()["noun"] == "milestone", r
    cases: list[tuple[list[str], dict[str, list[str]], str]] = [
        (["label list", "milestone list"], {"label list": ["ls"], "milestone list": ["ls"]}, ""),
        (["label list", "label ls"], {"label list": ["ls"]}, "label ls"),
        (
            ["label list", "label search"],
            {"label list": ["find"], "label search": ["find"]},
            "label find",
        ),
        (["label list", "deploy"], {"deploy": ["label"]}, "label"),
        (["deploy"], {"deploy": ["context"]}, "context"),
    ]
    for paths, aliases, bad in cases:
        v = V._V()
        V._tree(v, "data.commands", paths, aliases)
        want = (
            [
                f"data.commands: alias '{bad}' collides with a command path or another alias "
                "(CLI-021)"
            ]
            if bad
            else []
        )
        assert v.problems == want, (paths, aliases, v.problems)


# ---------------------------------------------------------------- P2 ids


def test_p2_id_inputs_are_strings(h: THarness) -> None:
    """An input named id, iid or *_id shows as a string in preview.inputs and in the
    operation record whatever the flag's type (CLI-007); other numbers stay numbers."""

    def build(u: Upstream) -> App:
        a = notes_app(u)
        note = a.root.commands["note"]

        @note.command("pin")
        @click.option("--iid", type=int, help="Note iid")
        @click.option("--label-id", type=int, help="Label id")
        @click.option("--weight", type=int, help="Pin weight")
        def pin(iid: Any, label_id: Any, weight: Any) -> None:
            def preview() -> Preview:
                return Preview(
                    action="Pin a note", can_apply=True, changes=[FieldChange("pinned", after=True)]
                )

            def apply(op: Op) -> Result:
                u.write(lambda: None)
                return Result()

            a.write(
                None,
                WriteSpec(target=Target(f"notes/{iid}", "note"), preview=preview, apply=apply),
            )

        a.register(
            pin,
            Meta(
                summary="Pin a note",
                effects=[CHANGE],
                output="note_write",
                positional="iid",
                examples=["notes note pin 5 --dry-run", "notes note pin 5 --confirm <token>"],
            ),
        )
        return a

    h.build = build
    args = ("note", "pin", "5", "--label-id", "7", "--weight", "3")
    r = h.run(*args, "--dry-run")  # h.run validates the envelope: ids are strings
    inputs = r.data()["preview"]["inputs"]
    assert r.exit == 0 and inputs == {"iid": "5", "label_id": "7", "weight": 3}, r
    op = r.data()["operation_id"]
    with open(os.path.join(h.state, "operations", op + ".json"), encoding="utf-8") as fh:
        stored = json.load(fh)["preview"]["inputs"]
    assert stored == {"iid": "5", "label_id": "7", "weight": 3}, stored
    assert h.run("operation", "show", "--id", op).exit == 0
    assert h.run(*args, "--confirm", r.token()).exit == 0


# ---------------------------------------------------------------- P3 changelog


def test_p3_changelog_fits_the_budget(h: THarness) -> None:
    """changelog without --since lists the newest entries that fit the success
    budget and says what it left out; --since is unchanged."""
    releases = 60
    parts = ["# Changelog\n\n## [Unreleased]\n\n"]
    for minor in range(releases, 0, -1):
        parts.append(
            f"## [1.{minor}.0] - 2026-01-01\n### Added\n- "
            + "A long description of the change. " * 8
            + "\n\n"
        )

    def build(u: Upstream) -> App:
        a = notes_app(u)
        a.version = f"1.{releases}.0"
        a.changelog = "".join(parts)
        return a

    h.build = build
    r = h.run("changelog")
    d = r.data()
    entries = d["entries"]
    assert r.exit == 0 and d["truncated"] is True and d["omitted"] >= 1 and entries, r
    assert d["omitted"] + len(entries) == releases, d["omitted"]
    assert (
        d["truncated_reason"]
        == CHANGELOG_TRUNCATED
        == (
            "Only the newest entries fit the size budget; run changelog --since <version> for the "
            "changes after a version."
        )
    )
    assert d["count"] == len(entries) and entries[0]["version"] == f"1.{releases}.0", d
    assert len(r.stdout.strip()) <= SUCCESS_BUDGET_CHARS
    V.validate_changelog(d)
    since = h.run("changelog", "--since", f"1.{releases - 3}.0")
    assert since.exit == 0 and len(since.data()["entries"]) == 3, since
    assert "truncated" not in since.data(), since
    h.build = notes_app  # a changelog that fits is not marked
    small = h.run("changelog")
    assert small.exit == 0 and "truncated" not in small.data() and "omitted" not in small.data()
    for extra, want in (
        ({"truncated": False}, "data.truncated: is true when present"),
        (
            {"truncated": True, "omitted": 2},
            "data.truncated_reason: a truncated changelog says why (CLI-041)",
        ),
        (
            {"truncated": True, "truncated_reason": "Cut.", "omitted": 0},
            "data.omitted: counts the entries left out, a positive integer",
        ),
        ({"omitted": 2}, "data: truncated_reason and omitted come with truncated: true"),
    ):
        data = {"current_version": "1.0.0", "entries": [], "count": 0, **extra}
        with pytest.raises(aicli.ContractViolation) as info:
            V.validate_changelog(data)
        assert want in str(info.value), (extra, info.value)


# ---------------------------------------------------------------- P4 replay at dry-run


def create_refusing_duplicates(previews: list[int]) -> Any:
    """note create whose preview blocks a title that exists (as GitLab refuses a
    duplicate label name), counting previews."""

    def build(u: Upstream) -> App:
        a = notes_app(u)
        create = a.root.commands["note"].commands["create"]

        def callback(title: str, **_: Any) -> None:
            def preview() -> Preview:
                previews[0] += 1
                pv = Preview(
                    action="Create a note",
                    can_apply=True,
                    changes=[FieldChange("title", after=title)],
                )
                with u.mu:
                    if any(n.title == title for n in u.notes.values()):
                        pv.can_apply = False
                        pv.blocked_by = [Block("already_exists", "A note with this title exists.")]
                return pv

            def apply(op: Op) -> Result:
                def do() -> TNote:
                    nid = str(u.next)
                    u.next += 1
                    n = TNote(nid, "notes/" + nid, title, "", [], 1, "2026-10-12T00:00:00Z")
                    u.notes[nid] = n
                    return n

                n = u.write(do)
                return Result(
                    data={"id": n.id, "ref": n.ref, "title": n.title}, untrusted=["title"]
                )

            a.write(
                None,
                WriteSpec(
                    target=Target("notes", "collection", "notes"), preview=preview, apply=apply
                ),
            )

        create.callback = callback
        return a

    return build


def test_p4_replay_at_dry_run(h: THarness) -> None:
    """With --idempotency-key, a dry-run or a bare call looks the key up before
    Preview: an applied operation answers with its replay, even when Preview would
    block the duplicate, and neither Preview nor Apply runs."""
    previews = [0]
    h.build = create_refusing_duplicates(previews)
    args = ("note", "create", "--title", "Weekly", "--idempotency-key", "w-1")
    first = h.run(*args, "--confirm", h.must_token(*args))
    assert first.exit == 0, first
    ref = first.data()["result"]["ref"]
    previews[0] = 0
    for extra in (("--dry-run",), ()):  # a dry-run and a bare call
        r = h.run(*args, *extra)
        assert r.exit == 0 and r.env["ok"] is True, (extra, r)
        assert r.data()["receipt"]["replayed"] is True, r
        assert r.data()["operation_id"] == first.data()["operation_id"], r
        assert r.data()["result"]["ref"] == ref and "confirm_token" not in r.data(), r
    assert previews[0] == 0 and h.up.writes() == 1
    # without the key the duplicate meets the preview's block
    r = h.run("note", "create", "--title", "Weekly", "--dry-run")
    assert previews[0] == 1 and r.token() == "", r
    assert r.data()["preview"]["can_apply"] is False, r
    # another request under the key is the mismatch, before Preview too
    previews[0] = 0
    expect_conflict(
        h.run("note", "create", "--title", "Other", "--idempotency-key", "w-1", "--dry-run"),
        "idempotency_mismatch",
    )
    assert previews[0] == 0


def test_p4_batch_replay_at_dry_run(h: THarness) -> None:
    """A batch's key is looked up the same way, before any item's Preview."""
    previews = [0]

    def build(u: Upstream) -> App:
        a = notes_app(u)
        bulk = a.root.commands["note"].commands["bulk-tag"]
        bulk.params.append(
            click.Option(["--idempotency-key"], expose_value=False, help="Repeat-safe key")
        )

        def callback(ids: Any, tag: str) -> None:
            items = []
            for nid in list(ids):

                def preview() -> Preview:
                    previews[0] += 1
                    return Preview(action="Tag", can_apply=True)

                def apply(op: Op, nid: str = nid) -> Result:
                    u.write(lambda: None)
                    return Result(data={"id": nid})

                items.append(BatchItem(Target("notes/" + nid, "note"), preview, apply))
            a.write_batch(None, BatchSpec(items=items))

        bulk.callback = callback
        return a

    h.build = build
    args = ("note", "bulk-tag", "--ids", "1,2", "--tag", "x", "--idempotency-key", "b-1")
    later = h.must_token(*args)  # issued before the first confirm
    first = h.run(*args, "--confirm", h.must_token(*args))
    assert first.exit == 0 and h.up.writes() == 2, first
    previews[0] = 0
    again = h.run(*args, "--dry-run")
    assert again.exit == 0 and previews[0] == 0 and h.up.writes() == 2, again
    assert again.data()["operation_id"] == first.data()["operation_id"], again
    assert len(again.data()["items"]) == 2, again
    assert all(it["receipt"]["replayed"] is True for it in again.data()["items"]), again
    # a token issued before the first confirm meets it at its own confirm (A5)
    confirmed = h.run(*args, "--confirm", later)
    assert confirmed.exit == 0 and h.up.writes() == 2, confirmed
    assert confirmed.data()["operation_id"] == first.data()["operation_id"], confirmed
    assert all(it["receipt"]["replayed"] is True for it in confirmed.data()["items"]), confirmed


# ---------------------------------------------------------------- P5 settings writer


def test_p5_set_profile_settings(h: THarness) -> None:
    """set_profile_settings validates the profile name and the keys, keeps
    everything else in the file, writes it atomically with mode 0600, and never
    overwrites an invalid file."""
    h.build = settings_app
    path = os.path.join(h.state, "settings.json")

    def read_back() -> Any:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    # no file yet: the state directory and the file are created
    h.app().set_profile_settings("lab", {"host": "https://lab.example"})
    assert read_back() == {"profiles": {"lab": {"host": "https://lab.example"}}}
    if not WINDOWS:
        assert os.stat(path).st_mode & 0o777 == 0o600
        assert os.stat(h.state).st_mode & 0o777 == 0o700
    # every other key, profile, number and default_profile is kept
    settings_file(
        h,
        {
            "default_profile": "work",
            "profiles": {"work": {"host": "https://w.example", "page_size": 50}},
        },
        path,
    )
    a = h.app()
    a.set_profile_settings("lab", {"page_size": "10"})
    a.set_profile_settings("work", {"host": "https://w2.example"})
    assert read_back() == {
        "default_profile": "work",
        "profiles": {
            "lab": {"page_size": "10"},
            "work": {"host": "https://w2.example", "page_size": 50},
        },
    }
    r = h.run("probe", "--name", "page_size", "--profile", "lab")
    assert r.data()["value"] == "10" and r.data()["source"] == "profile", r
    # an invalid file is E_CONFIG and stays as it is
    bad = '{"profiles": {"Bad Name": {}}}'
    settings_file(h, bad, path)
    with pytest.raises(aicli.Error) as info:
        h.app().set_profile_settings("lab", {"host": "https://x.example"})
    assert info.value.code == "E_CONFIG", info.value
    with open(path, encoding="utf-8") as fh:
        assert fh.read() == bad
    os.remove(path)
    # the profile name and the keys are checked first
    with pytest.raises(aicli.Error) as info:
        a.set_profile_settings("Lab!", {"host": "https://x.example"})
    e = info.value
    assert e.code == "E_VALIDATION" and e.details["parameter"] == "--profile", e
    assert e.message == "The profile name is not allowed; nothing was written."
    assert e.details["expected"] == "a profile name: a-z, 0-9, _ and -, up to 64 characters"
    assert e.details["next_action"] == (
        "Give a profile name of a-z, 0-9, _ and -, up to 64 characters, starting with a letter "
        "or a digit."
    )
    with pytest.raises(aicli.Error) as info:
        a.set_profile_settings("lab", {"nope": "x"})
    assert info.value.code == "E_UNKNOWN" and info.value.message == (
        'App.set_profile_settings: "nope" is not declared in App.settings.'
    )
    with pytest.raises(aicli.Error) as info:
        a.set_profile_settings("lab", {"api_token": "x"})
    assert info.value.code == "E_UNKNOWN" and info.value.message == (
        'App.set_profile_settings: "api_token" is a secret setting; secrets go in the OS '
        "keyring, not the settings file (SEC-040)."
    )
    assert not os.path.exists(path), "a refused call wrote the file"
    # a file that cannot be written is E_IO
    d = os.path.join(h.dir, "no-such-dir")
    h.env["NOTES_SETTINGS_FILE"] = os.path.join(d, "settings.json")
    with pytest.raises(aicli.Error) as info:
        h.app().set_profile_settings("lab", {"host": "https://x.example"})
    e = info.value
    assert e.code == "E_IO" and e.message == (
        "The settings file cannot be written; nothing was changed."
    ), e
    assert e.details["next_action"] == f"Check that {d} is writable.", e
    # a relative <PREFIX>_SETTINGS_FILE is E_CONFIG (C4)
    h.env["NOTES_SETTINGS_FILE"] = "settings.json"
    with pytest.raises(aicli.Error) as info:
        h.app().set_profile_settings("lab", {"host": "https://x.example"})
    assert info.value.code == "E_CONFIG", info.value


# ---------------------------------------------------------------- P6 new profiles


def login_app(u: Upstream) -> App:
    """The settings tool with login (Meta.new_profile): it reads its settings and
    the profile, and its apply creates the profile with set_profile_settings."""
    a = settings_app(u)
    a.settings = [
        Setting("host", flag="host", default="https://notes.example"),
        Setting("page_size", env="PAGE_SIZE", default="20"),
    ]
    a.schema(
        "login_result",
        SchemaDef(
            shape="object",
            fields=["profile", "profile_source", "host", "host_source", "page_size"],
        ),
    )

    @a.root.command("login")
    @click.option("--host", help="Notes service URL")
    def login(host: Any) -> None:
        host_v, host_from = a.setting("host")
        size, size_from = a.setting("page_size")
        profile, profile_from = a.profile()

        def preview() -> Preview:
            return Preview(
                action="Sign in", can_apply=True, changes=[FieldChange("host", after=host_v)]
            )

        def apply(op: Op) -> Result:
            try:
                a.set_profile_settings(profile, {"host": host_v})
            except aicli.Error as e:
                raise aicli.not_dispatched(e) from None
            return Result(
                data={
                    "profile": profile,
                    "profile_source": profile_from,
                    "host": host_v,
                    "host_source": host_from,
                    "page_size": size,
                    "page_size_source": size_from,
                }
            )

        a.write(
            None,
            WriteSpec(
                target=Target("profiles/" + profile, "profile"), preview=preview, apply=apply
            ),
        )

    a.register(
        login,
        Meta(
            summary="Sign in and keep the host in a profile",
            effects=[CONFIGURE],
            output="login_result",
            new_profile=True,
            examples=["notes login --profile lab --host https://lab.example --dangerous --dry-run"],
        ),
    )
    return a


def test_p6_new_profile_commands(h: THarness) -> None:
    """A command declared with Meta.new_profile runs with a requested profile the
    settings file does not have: setting reads the flag, the environment and the
    default; profile() and the binding name the requested profile; its apply can
    create it with set_profile_settings. Other commands still refuse the profile."""
    h.build = login_app
    h.grant({"effects": ["change", "deliver", "configure"]})
    settings_file(h, {"profiles": {"work": {"host": "https://w.example"}}})
    h.env["NOTES_PAGE_SIZE"] = "30"
    args = ("login", "--profile", "lab", "--host", "https://lab.example", "--dangerous")
    dry = h.run(*args, "--dry-run")
    assert dry.exit == 0 and dry.data()["preview"]["inputs"]["profile"] == "lab", dry
    assert dry.token(), dry
    # the token binds the requested profile
    other = ("login", "--profile", "lab2", "--host", "https://lab.example", "--dangerous")
    expect_mismatch(h.run(*other, "--confirm", dry.token()), "profile")
    r = h.run(*args, "--confirm", dry.token())
    assert r.data()["result"] == {
        "profile": "lab",
        "profile_source": "flag",
        "host": "https://lab.example",
        "host_source": "flag",
        "page_size": "30",
        "page_size_source": "env",
    }, r
    # the profile now exists
    p = h.run("probe", "--name", "host", "--profile", "lab")
    assert p.exit == 0 and p.data()["value"] == "https://lab.example", p
    assert p.data()["source"] == "profile", p
    # other commands still refuse a profile the file does not have
    p = h.run("probe", "--name", "host", "--profile", "nowhere")
    assert p.code() == "E_CONFIG", p
    assert p.env["error"]["message"] == "The requested profile is not in the settings file."
    # the environment selects the new profile the same way
    h.env["NOTES_PROFILE"] = "home"
    env = h.run("login", "--host", "https://home.example", "--dangerous", "--dry-run")
    assert env.exit == 0 and env.data()["preview"]["inputs"]["profile"] == "home", env


# ---------------------------------------------------------------- P7, P8 conformance


def test_p7_local_configure_commands_in_conformance() -> None:
    """A command whose only effects are configure, persist or artifact may change
    only local things: G04 takes its receipt (change applied or persistence saved) as
    its one write. A confirm that applied nothing is still reported."""
    saved: list[str] = []

    def build_for(fail: bool) -> Any:
        def build(u: Upstream) -> App:
            a = notes_app(u)

            @a.root.command("login")
            @click.option("--host", required=True, help="Notes service URL")
            def login(host: str) -> None:
                def preview() -> Preview:
                    return Preview(
                        action="Keep the host",
                        can_apply=True,
                        changes=[FieldChange("host", after=host)],
                    )

                def apply(op: Op) -> Result:
                    if fail:
                        raise aicli.not_dispatched(RuntimeError("the keyring is locked"))
                    saved.append(host)  # a local change: the fake upstream counts no write
                    return Result(data={"host": host})

                a.write(
                    None,
                    WriteSpec(
                        target=Target("profiles/default", "profile"), preview=preview, apply=apply
                    ),
                )

            a.schema("login_result", SchemaDef(shape="object", fields=["host"]))
            a.register(
                login,
                Meta(
                    summary="Keep the notes host",
                    effects=[CONFIGURE],
                    output="login_result",
                    examples=[
                        "notes login --host https://lab.example --dangerous --dry-run",
                        "notes login --host https://lab.example --dangerous --confirm <token>",
                    ],
                ),
            )
            return a

        return build

    def harness(fail: bool) -> aicli.Harness:
        h, _ = harness_for(build_for(fail))
        h.samples["login"] = ["--host", "https://lab.example"]
        assert h.scope is not None
        h.scope["effects"] = ["change", "deliver", "configure", "artifact", "persist"]
        return h

    aicli.run_conformance(harness(False))
    assert saved, "the suite never confirmed login"
    rec = Recorder()
    aicli.run_conformance(harness(True), rec)
    assert rec.has(
        "G04: login: the exact confirm sent no write, and its receipt shows no applied change "
        "or saved file"
    ), "\n".join(rec.errors)


def test_p8_g03_never_adds_profile() -> None:
    """--profile is aicli's, like the other global flags: G03 never adds it (an
    unknown profile would stop the command before the gate)."""

    def build(u: Upstream) -> App:
        a = notes_app(u)
        a.settings = [Setting("host", default="https://notes.example")]
        return a

    h, _ = harness_for(build)
    aicli.run_conformance(h)


# ---------------------------------------------------------------- P9 typed mutations


def test_p9_mutations_stay_within_the_type() -> None:
    """G03 changes a typed value within its type, so the tool's own validation lets
    it through to the gate."""
    a = notes_app(Upstream())
    note = a.root.commands["note"]

    @note.command("mutate")
    @click.option("--count", type=int, help="Count")
    @click.option("--ratio", type=float, help="Ratio")
    @click.option("--draft", type=bool, help="Draft")
    @click.option("--state", help="State")
    @click.option("--due-date", help="Due date")
    @click.option("--at", help="When")
    @click.option("--color", help="Colour")
    @click.option("--title", help="Title")
    @click.option("--labels", multiple=True, help="Labels")
    @click.option("--ids", type=int, multiple=True, help="Ids")
    def mutate(**_: Any) -> None:
        pass

    m = Meta(
        summary="Mutate",
        effects=[OBSERVE],
        output="note",
        params={"state": ParamDoc(enum=["opened", "closed"])},
        examples=["notes note mutate"],
    )
    a.register(mutate, m)
    a.install_builtins()
    node = a.command_by_path("note mutate")
    assert node is not None
    for flag, value, want in (
        ("count", "5", "6"),
        ("count", "-1", "0"),
        ("ratio", "1.5", "2.5"),
        ("draft", "true", "false"),
        ("draft", "false", "true"),
        ("state", "opened", "closed"),
        ("state", "closed", "opened"),
        ("due-date", "2026-12-31", "2027-01-01"),
        ("at", "2026-10-14T10:00:00Z", "2026-10-15T10:00:00Z"),
        ("at", "2026-10-14T10:00:00.500+08:00", "2026-10-15T10:00:00.5+08:00"),
        ("at", "2026-10-14T10:00:00+00:00", "2026-10-15T10:00:00Z"),
        ("color", "#ff0000", "#00ffff"),
        ("title", "Hello", "Hello-mutated"),
        ("labels", "a,b", "a,b,aicli-mutated"),
        ("ids", "1,2", "1,2,1002"),
    ):
        got = mutate_value(node.flag(flag), m, flag, value)
        assert got == want, (flag, value, got)
    # a positional argument has no flag: its shape decides
    for value, want in (
        ("41", "42"),
        ("2026-02-28", "2026-03-01"),
        ("acme/web", "acme/web-mutated"),
    ):
        assert mutate_value(None, m, "", value) == want, value


# ---------------------------------------------------------------- P10 transport


def test_p10_ssl_context_and_user_agent(h: THarness, tmp_path: Any) -> None:
    """The CLI-110 TLS settings and User-Agent are exported: TLS 1.2 or newer,
    verified, the system roots plus <PREFIX>_CA_FILE; an unusable CA file is
    E_CONFIG."""
    a = h.app()
    ctx = a.ssl_context()
    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2 and ctx.check_hostname
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert a.user_agent() == "notes/1.2.3 aicli/" + aicli.SPEC_VERSION
    gh = FakeGitHub()
    try:
        gh.json("/hello", {"ok": True})

        def get(app: App) -> Any:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(context=app.ssl_context()),
            )
            req = urllib.request.Request(
                gh.base + "/hello", headers={"User-Agent": app.user_agent()}
            )
            with opener.open(req, timeout=10) as resp:
                return json.loads(resp.read())

        with pytest.raises(urllib.error.URLError):
            get(a)  # a server whose CA is not trusted is refused
        h.env["NOTES_CA_FILE"] = gh.ca_file
        a = h.app()
        assert get(a) == {"ok": True}
        assert gh.requests[-1][1]["user-agent"] == a.user_agent()
    finally:
        gh.close()
    bad = tmp_path / "bad.pem"
    bad.write_text("not a certificate", encoding="utf-8")
    h.env["NOTES_CA_FILE"] = str(bad)
    with pytest.raises(aicli.Error) as info:
        h.app().ssl_context()
    e = info.value
    assert e.code == "E_CONFIG" and e.details["dispatch_state"] == "not_started", e
    assert e.message == f"NOTES_CA_FILE names {bad}, which is not a readable PEM certificate file."
    assert e.details["next_action"] == (
        "Point NOTES_CA_FILE at a PEM file of CA certificates, or unset it."
    )


# ---------------------------------------------------------------- P11 unknown verbs


def test_p11_unknown_verb_keeps_flag_values(h: THarness) -> None:
    """After an unknown verb the corrected command line reads the remaining
    arguments as the suggested command does: a value of its flag is not a
    positional."""
    r = h.run("note", "edit", "1", "--title", "Renamed")
    assert r.code() == "E_USAGE", r
    assert r.details()["next_action"] == (
        "Did you mean `notes note update`? Run `notes note update 1 --title Renamed`."
    ), r
    r = h.run("note", "edit", "1", "2", "--title", "Renamed")
    assert r.details()["next_action"] == (
        "Did you mean `notes note update`? It does not take those positional arguments; run "
        "`notes note update --help` for its flags."
    ), r


# ---------------------------------------------------------------- P12 --fields on writes


def test_p12_fields_project_the_write_result(h: THarness) -> None:
    """--fields on a confirmed write projects data.result with the command's output
    schema, as for a read; the receipt is never projected. Replays and batch items
    too."""
    args = ("note", "update", "1", "--title", "Renamed")
    r = h.run(*args, "--confirm", h.must_token(*args), "--fields", "title")
    receipt = r.data()["receipt"]
    assert r.exit == 0 and r.data()["result"] == {"title": "Renamed", "ref": "notes/1"}, r
    assert receipt["outcome"] and receipt["target"], r
    # without --fields the whole result comes back
    args = ("note", "update", "2", "--title", "Whole")
    r = h.run(*args, "--confirm", h.must_token(*args))
    assert len(r.data()["result"]) == 7, r
    # a replay's result is projected by the repeat's --fields
    create = ("note", "create", "--title", "Kept", "--idempotency-key", "p12")
    assert h.run(*create, "--confirm", h.must_token(*create)).exit == 0
    again = h.run(*create, "--dry-run", "--fields", "title")
    res = again.data()["result"]
    assert set(res) == {"title", "ref"} and res["title"] == "Kept", again
    assert again.data()["receipt"]["replayed"] is True, again
    # batch items
    bulk = ("note", "bulk-tag", "--ids", "1,2", "--tag", "work")
    b = h.run(*bulk, "--confirm", h.must_token(*bulk), "--fields", "tags")
    items = b.data()["items"]
    assert b.exit == 0 and len(items) == 2, b
    for it in items:
        assert set(it["result"]) == {"tags"} and it["receipt"], b


# ---------------------------------------------------------------- P13 context --fields


def test_p13_context_fields(h: THarness) -> None:
    """context accepts as --fields every top-level key it returns, profile and the
    context_extra keys included."""

    def build(u: Upstream) -> App:
        a = settings_app(u)
        a.context_extra = lambda: {"git_remote": {"host": "gitlab.example", "path": "acme/web"}}
        return a

    h.build = build
    r = h.run("context", "--fields", "profile,git_remote")
    assert r.exit == 0 and set(r.data()) == {"profile", "git_remote"}, r
    r = h.run("context", "--fields", "nope")
    expected = str(r.details().get("expected"))
    assert r.code() == "E_USAGE" and "profile" in expected and "git_remote" in expected, r


# ---------------------------------------------------------------- P14 development builds


@pytest.mark.parametrize("version", ["dev", ""])
def test_p14_development_builds(h: THarness, version: str) -> None:
    """A development build (App.version "dev" or empty) runs as the next patch after
    the newest changelog release with the prerelease dev, everywhere."""

    def build(u: Upstream) -> App:
        a = notes_app(u)
        a.version = version
        return a

    h.build = build
    want = "1.2.4-dev"  # the test changelog's newest release is 1.2.3
    r = h.run("--version")
    assert r.exit == 0 and r.data()["version"] == want, r
    ref = h.run("reference")
    assert ref.data()["version"] == want, ref
    V.validate_reference_full(ref.data())
    cl = h.run("changelog")
    assert cl.data()["current_version"] == want, cl
    V.validate_changelog(cl.data())
    assert h.run("context").data()["version"] == want
    args = ("note", "update", "1", "--title", "Dev")
    assert h.run(*args, "--confirm", h.must_token(*args)).exit == 0
    assert h.app().user_agent() == f"notes/{want} aicli/{aicli.SPEC_VERSION}"


def test_p14_dev_version() -> None:
    for changelog, want in (
        ("# Changelog\n\n## [Unreleased]\n### Added\n- Soon.\n", "0.0.0-dev"),
        ("## [1.2.17] - 2026-01-01\n### Fixed\n- A.\n", "1.2.18-dev"),
        ("## [1.2.17]\n- A.\n## [1.10.0]\n- B.\n", "1.10.1-dev"),
    ):
        assert dev_version(changelog) == want, changelog


def test_p14_development_build_passes_conformance() -> None:
    def build(u: Upstream) -> App:
        a = notes_app(u)
        a.version = "dev"
        return a

    h, _ = harness_for(build)
    aicli.run_conformance(h)


# ---------------------------------------------------------------- P15 lock_held


def test_p15_lock_held_without_a_known_owner(h: THarness, monkeypatch: pytest.MonkeyPatch) -> None:
    """lock_held names the running operation only when the lock file names one; the
    envelope validates either way (h.run validates it)."""
    import aicli.gate as gate

    monkeypatch.setattr(gate, "TARGET_LOCK_WAIT", 0.3)
    tok = h.must_token("note", "update", "1", "--title", "Locked")
    d = os.path.join(h.state, "locks")
    os.makedirs(d, exist_ok=True)
    lock = os.path.join(d, hashlib.sha256(b"notes/1").hexdigest()[:32] + ".lock")
    with open(lock, "w", encoding="utf-8") as fh:
        fh.write("not an operation id")
    r = h.run("note", "update", "1", "--title", "Locked", "--confirm", tok)
    expect_conflict(r, "lock_held")
    assert r.details()["target"] == "notes/1" and "active_operation" not in r.details(), r
    assert h.up.writes() == 0


# ---------------------------------------------------------------- P16 validators and F10


def test_p16_validators_enforce_f10() -> None:
    """skill_sync_command comes only when the Skill sync could not finish, and then
    always; binary_replaced alone no longer asks for it."""
    data = {
        "status": "up_to_date",
        "current_version": "1.3.1",
        "target_version": "1.3.1",
        "update_available": False,
        "install_method": "binary",
        "signature_status": "not_checked",
        "binary_replaced": False,
    }
    for status in ("skipped", "not_needed", "synced"):
        v = V._V()
        V._update(v, "data", {**data, "skill_sync_status": status, "skill_sync_command": "x"})
        assert (
            "data.skill_sync_command: comes only when the Skill sync could not finish, not with "
            f"skill_sync_status {status} (F10)"
        ) in v.problems, v.problems
        v = V._V()
        V._update(v, "data", {**data, "skill_sync_status": status})
        assert v.problems == [], v.problems
    partial = {
        "dispatch_state": "dispatched",
        "next_action": "Run `notes update` again to finish the Skill sync.",
        "status": "partial",
        "stage": "skill_sync",
        "binary_replaced": True,
        "skill_sync_status": "failed",
        "previous_version": "1.3.0",
        "target_version": "1.3.1",
        "current_version": "1.3.1",
        "update_available": False,
        "install_method": "binary",
        "signature_status": "verified",
    }
    v = V._V()
    V._update_failure(v, "error.details", partial, "E_PARTIAL")
    assert v.problems == [
        'error.details: missing key "skill_sync_command": the Skill sync could not finish (F10)'
    ], v.problems
    v = V._V()
    V._update_failure(v, "error.details", {**partial, "skill_sync_command": "notes update"}, "")
    assert v.problems == [], v.problems
    # an error after the swap whose sync did not run carries no command
    replaced = {**partial, "stage": "replace", "skill_sync_status": "skipped"}
    del replaced["status"]
    v = V._V()
    V._update_failure(v, "error.details", replaced, "E_IO")
    assert v.problems == [], v.problems


# ---------------------------------------------------------------- P17 redaction parity


def test_p17_redaction_forms(h: THarness) -> None:
    """Errors and debug lines redact the raw value, the query-escaped form, the
    path-escaped form and the path-escaped form that keeps "/" (Go's forms, byte
    for byte)."""
    secret = "kr/SENTINEL+9 x"
    forms = [
        secret,
        urllib.parse.quote_plus(secret, safe=""),  # url.QueryEscape
        urllib.parse.quote(secret, safe="$&+:=@"),  # url.PathEscape
        urllib.parse.quote(secret, safe="$&+:=@/"),  # PathEscape with %2F back to /
    ]
    assert forms[1:] == ["kr%2FSENTINEL%2B9+x", "kr%2FSENTINEL+9%20x", "kr/SENTINEL+9%20x"]

    def build(u: Upstream) -> App:
        a = notes_app(u)
        a.schema("ping_result", SchemaDef(shape="object"))

        @a.root.command("ping")
        def ping() -> None:
            a.add_secret(secret)
            a.debug("forms " + " | ".join(forms))
            raise RuntimeError("forms " + " | ".join(forms))

        a.register(
            ping,
            Meta(summary="Ping", effects=[OBSERVE], output="ping_result", examples=["notes ping"]),
        )
        return a

    h.build = build
    h.env["NOTES_DEBUG"] = "1"
    r = h.run("ping")
    assert r.code() == "E_UNKNOWN", r
    for f in forms:
        assert f not in r.stdout and f not in r.stderr, (f, r)
    assert "debug: forms *** | *** | *** | ***" in r.stderr, r.stderr
    assert r.details()["cause"].endswith("forms *** | *** | *** | ***"), r
