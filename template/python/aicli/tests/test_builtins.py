"""The built-in commands (port of builtins_test.go, plus decisions 4, 22 and 26)."""

from __future__ import annotations

import datetime as _dt
import os
from typing import Any

import click
import pytest
from aicli_testapp import THarness, Upstream, list_has, notes_app

import aicli
from aicli import OBSERVE, Check, Meta
from aicli.builtins import parse_changelog, redact_host
from aicli.contract_gen import COMMAND_BUDGET_CHARS, INDEX_BUDGET_CHARS, SPEC_VERSION
from aicli.validate import (
    validate_context,
    validate_doctor,
    validate_reference_command,
    validate_reference_full,
    validate_reference_index,
)


def test_reference_layers(h: THarness) -> None:
    full = h.run("reference")
    assert full.exit == 0, full
    validate_reference_full(full.data())
    assert full.data()["spec_version"] == SPEC_VERSION and full.data()["risk_tier"] == "T2", (
        full.data()
    )
    idx = h.run("reference", "--index")
    assert idx.exit == 0 and len(idx.stdout.strip()) <= INDEX_BUDGET_CHARS, idx
    validate_reference_index(idx.data())
    dele = next((c for c in idx.data()["commands"] if c["path"] == "note delete"), None)
    assert dele is not None and dele["type"] == "write" and dele["dangerous"] is True, dele
    one = h.run("reference", "--command", "note update")
    assert one.exit == 0 and len(one.stdout.strip()) <= COMMAND_BUDGET_CHARS, one
    validate_reference_command(one.data())
    params = one.data()["command"]["params"]
    assert params[0]["name"] == "id" and params[0]["kind"] == "positional", params
    body_file = next(p for p in params if p["name"] == "body-file")
    assert body_file["type"] == "path" and body_file["file_input"] is True, body_file
    codes = one.data()["error_codes"]
    for c in (
        "E_CONFIRMATION_REQUIRED",
        "E_CONFLICT",
        "E_FORBIDDEN",
        "E_OUTCOME_UNKNOWN",
        "E_NOT_FOUND",
    ):
        assert c in codes, codes
    assert "note_write" in one.data()["schemas"], one.data()["schemas"]
    for args in (["reference", "--command", "note", "update"], ["reference", "note", "update"]):
        r = h.run(*args)
        assert r.exit == 0 and r.data()["command"]["path"] == "note update", (args, r)
    r = h.run("reference", "--command", "note updte")
    assert (
        r.exit == 2
        and r.details()["problem"] == "unknown_command"
        and list_has(r.details().get("did_you_mean"), "note update")
        and list_has(r.details().get("available"), "note list")
    ), r
    r = h.run("reference", "--index", "--command", "note get")
    assert r.details().get("problem") == "conflicting_flags", r


def test_reference_index_groups_when_large(h: THarness) -> None:
    def build(u: Upstream) -> aicli.App:
        a = notes_app(u)
        for g in range(6):
            grp = click.Group(f"area{g}")
            a.group(grp, f"Area {g}")
            a.root.add_command(grp)
            for i in range(20):
                c = click.Command(f"action{i:02d}", callback=lambda: a.ok(None, {}))
                grp.add_command(c)
                a.register(
                    c,
                    Meta(
                        summary="Does something useful with the area " * 2,
                        effects=[OBSERVE],
                        output="note",
                        examples=["notes area action"],
                    ),
                )
        return a

    h.build = build
    r = h.run("reference", "--index")
    groups = r.data().get("groups")
    assert r.exit == 0 and isinstance(groups, list) and len(groups) >= 7, r
    assert len(r.stdout.strip()) <= INDEX_BUDGET_CHARS, "the grouped index is over budget"
    g = h.run("reference", "--index", "area3")
    assert g.exit == 0 and len(g.data()["commands"]) == 20, g
    bad = h.run("reference", "--index", "nowhere")
    assert bad.exit == 2, bad


def _skill(root: str, tool: str, version: str, min_version: str) -> str:
    d = os.path.join(root, tool)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(
            f"---\nname: {tool}\ndescription: Notes.\nmetadata:\n  version: {version}\n  "
            f"min_version: {min_version}\n---\n\n# {tool}\n"
        )
    return root


def _statuses(r: Any) -> dict[str, str]:
    return {c["check"]: c["status"] for c in r.data()["checks"]}


def test_context_and_doctor(h: THarness) -> None:
    r = h.run("context")
    assert r.exit == 0, r
    validate_context(r.data())
    d = r.data()
    assert (
        d["account"] == "tester"
        and d["host"] == "https://notes.example"
        and d["credentials"]["source"] == "env"
    ), d
    assert not os.path.exists(h.state), "context must not create the state directory"
    skills = _skill(os.path.join(h.dir, "skills"), "notes", "1.2.3", "1.0.0")

    def with_skills(u: Upstream) -> aicli.App:
        a = notes_app(u)
        a.skill_dirs = [skills]
        return a

    h.build = with_skills
    doc = h.run("doctor")
    assert doc.exit == 0, doc
    validate_doctor(doc.data())
    st = _statuses(doc)
    assert (
        st["authorization"] == "pass" and st["version"] == "pass" and st["credentials"] == "pass"
    ), st

    def with_checks(u: Upstream) -> aicli.App:
        a = notes_app(u)
        a.skill_dirs = [_skill(os.path.join(h.dir, "skills9"), "notes", "9.0.0", "9.0.0")]
        a.doctor_checks = lambda: [
            Check("connectivity", "pass", "Reached the upstream."),
            Check("quota", "warn", "Low."),
        ]
        return a

    h.build = with_checks
    doc = h.run("doctor")
    st = _statuses(doc)
    assert st["connectivity"] == "pass" and st["quota"] == "warn" and st["version"] == "fail", st
    validate_doctor(doc.data())
    fail = next(c for c in doc.data()["checks"] if c["check"] == "version")
    assert fail["fix"], "a failing check carries a fix (decision 15)"


def test_doctor_version_check_reads_the_installed_skill(h: THarness) -> None:
    """Decision 22: the version check compares the tool with the installed Skill."""
    cases = [
        (None, "warn", "not installed"),  # no Skill installed
        (("1.2.3", "1.0.0"), "pass", "matches"),
        (("1.2.0", "1.0.0"), "warn", "older"),  # Skill older than the tool: run update
        (("1.3.0", "1.3.0"), "fail", "needs notes 1.3.0"),  # Skill needs a newer tool
        (("one", "1.0.0"), "warn", "not a semantic version"),
    ]
    for i, (versions, want, text) in enumerate(cases):
        root = os.path.join(h.dir, f"skills{i}")
        os.makedirs(root, exist_ok=True)
        if versions is not None:
            _skill(root, "notes", *versions)

        def build(u: Upstream, root: str = root) -> aicli.App:
            a = notes_app(u)
            a.skill_dirs = [root]
            a.min_skill_version = ""
            return a

        h.build = build
        doc = h.run("doctor")
        check = next(c for c in doc.data()["checks"] if c["check"] == "version")
        assert check["status"] == want and text in check["message"], (versions, check)
        assert (check["fix"] is None) == (want == "pass"), check


def test_context_state_is_honest(h: THarness) -> None:
    """An unknown state directory is null, protection is null when the store is
    unavailable, and on Windows a directory outside the user profile is
    windows-unverified (decisions 15 and 30)."""
    from aicli.keystore import protection

    state = h.run("context").data()["state"]
    assert state["available"] is True and state["dir"] == os.path.abspath(h.state), state
    assert state["protection"] == protection(h.state), state
    if os.name == "nt":
        home = os.path.expanduser("~")
        assert protection(os.path.join(home, ".notes-aicli-test")) == "windows-profile-acl"
        for outside in (r"Z:\aicli-state", r"\\server\share\state"):
            assert protection(outside) == "windows-unverified", outside
    else:
        assert protection(h.state) == "posix-0700"
    os.makedirs(h.state, exist_ok=True)
    with open(os.path.join(h.state, "confirm.key"), "wb") as fh:
        fh.write(b"broken")
    state = h.run("context").data()["state"]
    assert state["available"] is False and state["protection"] is None and state.get("reason"), (
        state
    )
    # no state directory can be resolved: dir is null, never ""
    a = notes_app(Upstream())
    a.env = lambda k: ""
    a.state_dir = ""
    import io as _io

    out = _io.BytesIO()
    a.stdout = a.stderr = out
    old_home = os.environ.get("HOME"), os.environ.get("USERPROFILE")
    try:
        os.environ["HOME"] = os.environ["USERPROFILE"] = ""
        if os.path.expanduser("~") not in ("~", ""):
            pytest.skip("the home directory is still known here")
        assert a.execute(["context"]) == 0, out.getvalue()
    finally:
        for k, v in zip(("HOME", "USERPROFILE"), old_home, strict=True):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    aicli.validate_envelope(out.getvalue())
    assert b'"dir":null' in out.getvalue() and b'"protection":null' in out.getvalue(), (
        out.getvalue()
    )


def test_context_redacts_host_credentials() -> None:
    assert (
        redact_host("https://user:pw@host.example/path?token=abc#x") == "https://host.example/path"
    )


def test_changelog(h: THarness) -> None:
    r = h.run("changelog")
    entries = r.data()["entries"]
    assert r.exit == 0 and len(entries) == 3 and r.data()["current_version"] == "1.2.3", r
    assert entries[0]["version"] == "1.2.3" and entries[0]["date"] == "2026-10-01", entries[0]
    assert (
        entries[1]["changes"]["changed"][0] == "Compact JSON by default. It is also documented."
    ), entries[1]
    r = h.run("changelog", "--since", "1.0.0")
    assert len(r.data()["entries"]) == 2 and r.data()["since"] == "1.0.0", r
    r = h.run("changelog", "--since", "1.0")
    assert r.exit == 2 and r.details().get("parameter") == "--since", r


def test_changelog_parser_skips_comments_and_link_references() -> None:
    """Decision 26: HTML comments are skipped; a continuation line ends at a blank
    line or a link-reference definition."""
    text = """# Changelog
<!-- maintainers: keep the newest release first -->

## [Unreleased]

## [1.1.0] - 2026-02-01
### Added
- Search.
  <!-- internal: ticket 42 -->
  Works offline.
<!--
## [9.9.9] - 2099-01-01
### Added
- Not a release.
-->
- Export.
[1.1.0]: https://example.com/compare/v1.0.0...v1.1.0
  not a continuation

## [1.0.0] - 2026-01-01
### Fixed
- Crash.

[1.0.0]: https://example.com/releases/v1.0.0
"""
    entries = parse_changelog(text)
    assert [e.version for e in entries] == ["1.1.0", "1.0.0"], entries
    assert entries[0].changes == {"added": ["Search. Works offline.", "Export."]}, entries[0]
    assert entries[1].changes == {"fixed": ["Crash."]}, entries[1]


def test_operation_builtins(h: THarness) -> None:
    r = h.run("operation", "list")
    assert r.exit == 0 and r.data()["items"] == [], r
    assert not os.path.exists(h.state), "operation list must not create the state directory"
    for i in range(3):
        tok = h.must_token("note", "update", "1", "--title", f"T{i}")
        h.run("note", "update", "1", "--title", f"T{i}", "--confirm", tok)
    r = h.run("operation", "list", "--status", "applied", "--limit", "2")
    assert (
        r.exit == 0
        and len(r.data()["items"]) == 2
        and r.data()["has_more"] is True
        and r.data().get("next_offset")
        and r.data()["total"] == 3
    ), r
    r = h.run("operation", "show", "--id", "op_zzzzzzzzzzzzzzzzzzzzzzzzzz")
    assert r.exit == 3 and r.code() == "E_NOT_FOUND", r
    r = h.run("operation", "show", "--id", "../../etc/passwd")
    assert r.exit == 2 and r.details().get("parameter") == "--id", r
    r = h.run(
        "operation",
        "resolve",
        "--id",
        "op_zzzzzzzzzzzzzzzzzzzzzzzzzz",
        "--outcome",
        "maybe",
        "--evidence",
        "x",
    )
    assert r.exit == 2 and r.details().get("parameter") == "--outcome", r


def test_update_notice_from_cache_only(h: THarness) -> None:
    clock = [_dt.datetime.now(_dt.timezone.utc)]
    h.now = lambda: clock[0]
    h.app().record_update_check("1.3.0", "warning")
    r = h.run("note", "get", "1")
    notices = r.env["meta"].get("notices") or []
    assert (
        len(notices) == 1
        and notices[0]["type"] == "update_available"
        and notices[0]["latest_version"] == "1.3.0"
    ), r
    r = h.run("note", "get", "1")
    assert "notices" not in r.env["meta"], "the notice must appear at most once a day"
    clock[0] += _dt.timedelta(hours=25)
    r = h.run("note", "get", "1")
    assert "notices" in r.env["meta"], "the notice should reappear after a day"
    b = h.app()
    b.record_update_check("1.2.3", "info")
    clock[0] += _dt.timedelta(hours=48)
    r = h.run("note", "get", "1")
    assert r.env["meta"].get("notices") is None, r
    with pytest.raises(aicli.Error):
        b.record_update_check("1.3.0;rm -rf", "info")


def test_command_map(h: THarness) -> None:
    """Decision 4: one bullet per top-level noun; dangerous verbs marked; built-ins omitted."""
    assert h.app().command_map() == (
        "- `note`: list, get, create, update, delete (dangerous), share (dangerous), bulk-tag, "
        "tail, export; `note comment` add\n"
    )
