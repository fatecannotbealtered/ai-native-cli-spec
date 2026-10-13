"""Deprecation (D40, CLI-081), diagnostics (D41, CLI-102) and the default flag
synonyms (D42, CLI-028)."""

from __future__ import annotations

import dataclasses
import os
from typing import Any

from aicli_testapp import THarness, Upstream, notes_app

from aicli import App, Setting


def deprecated_app(u: Upstream) -> App:
    """The test tool with `note get` deprecated and `note list --tag` deprecated."""
    a = notes_app(u)
    note = a.root.commands["note"]
    get, lst = note.commands["get"], note.commands["list"]
    a.register(get, dataclasses.replace(a._reg.meta[get], deprecated="use `note view`"))
    a.register(lst, dataclasses.replace(a._reg.meta[lst], deprecated_flags={"tag": "use --label"}))
    return a


def test_deprecated_command_and_flag(h: THarness) -> None:
    h.build = deprecated_app
    r = h.run("note", "get", "1")
    assert r.exit == 0 and r.env["meta"]["notices"] == [
        {
            "type": "deprecated",
            "severity": "warning",
            "message": "`note get` is deprecated: use `note view`. It will be removed in notes "
            "2.0.0.",
        }
    ], r
    r = h.run("note", "list", "--tag", "seed")
    assert r.env["meta"]["notices"] == [
        {
            "type": "deprecated",
            "severity": "warning",
            "message": "`--tag` is deprecated: use --label.",
        }
    ], r
    assert "notices" not in h.run("note", "list").env["meta"], "only a use of the flag"
    # help and reference say so
    assert h.run("note", "get", "--help").stdout.startswith(
        "Deprecated: use `note view`. Show one note\n"
    )
    assert "Deprecated: use `note view`. Show one note" in h.run("note", "--help").stdout
    one = h.run("reference", "--command", "note get").data()["command"]
    assert one["summary"] == "Deprecated: use `note view`. Show one note", one
    idx = h.run("reference", "--index").data()["commands"]
    assert next(c for c in idx if c["path"] == "note get")["summary"] == one["summary"]
    params = h.run("reference", "--command", "note list").data()["command"]["params"]
    tag = next(p for p in params if p["name"] == "tag")
    assert tag["description"] == "Deprecated: use --label. Only notes with this tag", tag
    # the Skill teaches only current commands
    assert h.app().command_map() == (
        "- `note`: list, create, update, delete (dangerous), share (dangerous), bulk-tag, tail, "
        "export; `note comment` add\n"
    )
    a = deprecated_app(Upstream())
    lst = a.root.commands["note"].commands["list"]
    a.register(lst, dataclasses.replace(a._reg.meta[lst], deprecated_flags={"nope": "x"}))
    assert '"note list": Meta.deprecated_flags names --nope, which the command does not define' in (
        a.check_tree()
    )


def test_deprecation_text_is_trimmed_and_needs_no_semver(h: THarness) -> None:
    def build(u: Upstream) -> App:
        a = deprecated_app(u)
        a.version = "dev"
        get = a.root.commands["note"].commands["get"]
        a.register(get, dataclasses.replace(a._reg.meta[get], deprecated="use `note view`."))
        return a

    h.build = build
    r = h.run("note", "get", "1")
    assert r.env["meta"]["notices"][0]["message"] == (
        "`note get` is deprecated: use `note view`. It will be removed in the next major version "
        "of notes."
    ), r
    assert h.run("note", "get", "--help").stdout.startswith(
        "Deprecated: use `note view`. Show one note\n"
    )


def debug_app(u: Upstream) -> App:
    """The test tool with a secret setting; note share writes a debug line holding
    the secret flag's value and the secret setting's."""
    a = notes_app(u)
    a.settings = [Setting("api_token", secret=True)]
    share = a.root.commands["note"].commands["share"]
    orig = share.callback

    def callback(**kw: Any) -> None:
        a.debug(f"sharing with token {kw.get('token')} and {a.setting('api_token')[0]}")
        orig(**kw)

    share.callback = callback
    return a


def test_debug_lines_go_to_stderr_with_secrets_redacted(h: THarness) -> None:
    h.build = debug_app
    args = ("note", "share", "1", "--to", "bob@example.com", "--token", "s3cr3t-token")
    args += ("--dangerous", "--dry-run")
    h.env["NOTES_API_TOKEN"] = "tok-from-env"
    quiet = h.run(*args)
    assert quiet.exit == 0 and "debug:" not in quiet.stderr, quiet
    h.env["NOTES_DEBUG"] = "1"
    r = h.run(*args)
    assert r.exit == quiet.exit and "debug:" not in r.stdout, r
    assert "debug: sharing with token *** and ***\n" in r.stderr, r.stderr
    assert "s3cr3t-token" not in r.stderr and "tok-from-env" not in r.stderr, r.stderr
    assert f"debug: state directory: {os.path.abspath(h.state)}\n" in r.stderr, r.stderr
    assert f"debug: scope file: {os.path.abspath(h.scope)}\n" in r.stderr, r.stderr
    settings = os.path.join(os.path.abspath(h.state), "settings.json")
    assert f"debug: settings file: {settings} (not found)\n" in r.stderr, r.stderr
    assert "debug: active profile: none\n" in r.stderr, r.stderr
    assert r.stderr.count("debug: state directory") == 1, "library lines are written once a run"
    h.env["NOTES_DEBUG"] = "0"
    assert "debug:" not in h.run(*args).stderr


def test_default_flag_synonyms(h: THarness) -> None:
    r = h.run("note", "list", "--per-page", "5")
    assert r.exit == 2 and r.details()["problem"] == "unknown_flag", r
    assert r.details()["did_you_mean"][0] == "--limit", r.details()
    assert r.details()["next_action"] == "Run `notes note list --limit 5`.", r.details()
    for guess in ("page-size", "max-results", "max"):
        r = h.run("note", "list", f"--{guess}", "5")
        assert r.details()["did_you_mean"][0] == "--limit", (guess, r.details())
    r = h.run("note", "get", "1", "--per-page", "5")
    assert "--limit" not in (r.details().get("did_you_mean") or []), "only where --limit exists"

    def own(u: Upstream) -> App:
        a = notes_app(u)
        a.flag_synonyms = {"per-page": ["tag"]}
        return a

    h.build = own
    r = h.run("note", "list", "--per-page", "5")
    assert r.details()["did_you_mean"][0] == "--tag", "the tool's own entry wins"
