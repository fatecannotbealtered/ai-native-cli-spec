"""Settings and profiles (D39, CLI-100, CLI-101): precedence and sources, profile
selection, invalid settings files, context.profile, doctor's settings check and the
--profile flag."""

from __future__ import annotations

import json
import os
from typing import Any

import click
from aicli_testapp import THarness, Upstream, expect_mismatch, notes_app

from aicli import OBSERVE, App, Meta, SchemaDef, Setting
from aicli.settings import parse_settings_file
from aicli.validate import validate_context, validate_doctor

SETTINGS = [
    Setting("host", default="https://notes.example", description="The notes service"),
    Setting("region", flag="region", default="eu"),
    Setting("page_size", env="PAGE_SIZE", default="20"),
    Setting("api_token", secret=True),
]


def settings_app(u: Upstream) -> App:
    """The test tool with settings and a command that reads one (probe)."""
    a = notes_app(u)
    a.settings = list(SETTINGS)
    a.schema("setting_value", SchemaDef(shape="object", fields=["name", "value", "source"]))

    @a.root.command("probe")
    @click.option("--name", required=True, help="The setting to read")
    @click.option("--region", help="A region for this run")
    def probe(name: str, region: Any) -> None:
        value, source = a.setting(name)
        a.ok(None, {"name": name, "value": value, "source": source})

    a.register(
        probe,
        Meta(
            summary="Read a setting",
            effects=[OBSERVE],
            output="setting_value",
            examples=["notes probe --name host"],
        ),
    )
    return a


def settings_file(h: THarness, content: Any, path: str = "") -> str:
    """Write the settings file (NOTES_SETTINGS_FILE unless path is given) and return
    its path."""
    if not path:
        path = os.path.join(h.dir, "my-settings.json")
        h.env["NOTES_SETTINGS_FILE"] = path
    data = content if isinstance(content, (str, bytes)) else json.dumps(content)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data.encode("utf-8") if isinstance(data, str) else data)
    return path


def probe(h: THarness, name: str, *extra: str) -> tuple[str, str]:
    r = h.run("probe", "--name", name, *extra)
    assert r.exit == 0, r
    return r.data()["value"], r.data()["source"]


def profile(h: THarness, *extra: str) -> dict[str, Any]:
    r = h.run("context", *extra)
    assert r.exit == 0, r
    validate_context(r.data())
    return r.data()["profile"]


def test_setting_precedence(h: THarness) -> None:
    """The running command's flag > env > profile > default, each named as the
    source; the file is <state>/settings.json by default."""
    h.build = settings_app
    assert probe(h, "host") == ("https://notes.example", "default")
    path = os.path.join(h.state, "settings.json")
    settings_file(
        h, {"profiles": {"work": {"host": "https://work.example", "page_size": 50}}}, path
    )
    assert probe(h, "host") == ("https://work.example", "profile")
    assert probe(h, "page_size") == ("50", "profile"), "a number keeps its text"
    assert probe(h, "region") == ("eu", "default")
    h.env["NOTES_HOST"] = "https://env.example"
    assert probe(h, "host") == ("https://env.example", "env")
    h.env["NOTES_PAGE_SIZE"] = "5"
    assert probe(h, "page_size") == ("5", "env"), "env names the variable without the prefix"
    h.env["NOTES_REGION"] = "ap"
    assert probe(h, "region") == ("ap", "env")
    assert probe(h, "region", "--region", "sa") == ("sa", "flag")


def test_profile_selection(h: THarness) -> None:
    """--profile > NOTES_PROFILE > default_profile > the only profile > none."""
    h.build = settings_app
    path = settings_file(
        h,
        {
            "default_profile": "work",
            "profiles": {
                "work": {"host": "https://work.example"},
                "home": {"host": "https://home"},
            },
        },
    )
    want = {"name": "work", "source": "config", "profiles": ["home", "work"], "file": path}
    assert profile(h) == want
    h.env["NOTES_PROFILE"] = "home"
    assert profile(h) == {**want, "name": "home", "source": "env"}
    assert probe(h, "host") == ("https://home", "profile")
    assert profile(h, "--profile", "work") == {**want, "source": "flag"}
    assert probe(h, "host", "--profile", "work") == ("https://work.example", "profile")
    del h.env["NOTES_PROFILE"]
    settings_file(h, {"profiles": {"solo": {}}})
    assert profile(h) == {"name": "solo", "source": "single", "profiles": ["solo"], "file": path}
    settings_file(h, {"default_profile": None, "profiles": {"a": {}, "b": {}}})
    assert profile(h) == {"name": None, "source": "none", "profiles": ["a", "b"], "file": path}
    assert probe(h, "host") == ("https://notes.example", "default")
    os.remove(path)
    assert profile(h) == {"name": None, "source": "none", "profiles": [], "file": None}
    assert probe(h, "host") == ("https://notes.example", "default"), "no settings file is fine"


def test_unknown_profile(h: THarness) -> None:
    """D4/D5: an unknown profile stops every tool command before it runs, and the
    requested name is only in details.profile (untrusted), never in the message."""
    h.build = settings_app
    path = settings_file(h, {"profiles": {"work": {}, "home": {}}})
    h.env["NOTES_PROFILE"] = "wrok"
    r = h.run("probe", "--name", "host")
    assert r.exit == 4 and r.code() == "E_CONFIG", r
    assert r.env["error"]["message"] == "The requested profile is not in the settings file."
    assert r.details()["did_you_mean"] == ["work"], r.details()
    assert r.details()["profile"] == "wrok" and r.details()["source"] == "env", r.details()
    assert "error.details.profile" in r.env["meta"]["untrusted"], r.env["meta"]
    assert r.details()["next_action"] == (
        f"Use one of the profiles in details.did_you_mean, or add the profile to {path}."
    )
    assert "wrok" not in r.env["error"]["message"] + r.details()["next_action"]
    assert r.details()["dispatch_state"] == "not_started"
    # context never fails on it: it lists the profiles to choose from
    want = {"name": None, "source": "none", "profiles": ["home", "work"], "file": path}
    assert profile(h) == want
    doc = h.run("doctor")
    check = next(c for c in doc.data()["checks"] if c["check"] == "settings")
    assert check["status"] == "fail" and check["message"] == r.env["error"]["message"], check


def test_invalid_settings_file(h: THarness) -> None:
    h.build = settings_app
    path = settings_file(h, '{"profiles": {"work": {}}, "profiles": {}}')
    r = h.run("probe", "--name", "host")
    assert r.exit == 4 and r.code() == "E_CONFIG", r
    assert r.env["error"]["message"] == (
        f'The settings file {path} is invalid: the key "profiles" appears twice.'
    )
    assert r.details()["next_action"] == (
        f"Ask the user to fix {path}; `notes context` shows which file is read."
    )
    assert profile(h) == {"name": None, "source": "none", "profiles": [], "file": path}
    h.env["NOTES_HOST"] = "https://env.example"
    r = h.run("probe", "--name", "host")
    assert r.code() == "E_CONFIG", "an invalid settings file fails whatever the value's source"
    settings_file(h, {"profiles": {"work": {"api_token": "abc"}}})
    r = h.run("probe", "--name", "region")
    assert r.env["error"]["message"] == (
        f"The settings file {path} is invalid: `api_token` is a secret; secrets go in the OS "
        "keyring, not the settings file (SEC-040)."
    ), r


def test_settings_file_reasons() -> None:
    cases = {
        b"\xef\xbb\xbf{}": "it starts with a byte order mark",
        b"": "it is not JSON",
        b"{": "it is not valid JSON",
        b"[]": "it is not a JSON object",
        b"{} {}": "it holds more than one JSON value",
        b'{"profile": {}}': "unknown key `profile` (did you mean `profiles`?)",
        b'{"default_profile": 1}': "default_profile must be a string or null",
        b'{"profiles": []}': "profiles must be an object",
        b'{"profiles": {"Work": {}}}': "the profile name `Work` is not allowed (use a-z, 0-9, _ "
        "and -, up to 64 characters)",
        b'{"profiles": {"work": 1}}': "profile `work` must be an object",
        b'{"profiles": {"work": {"hots": "x"}}}': "profile `work` sets `hots`, which is not a "
        "setting of notes",
        b'{"profiles": {"work": {"host": ["x"]}}}': "`host` in profile `work` must be a string, "
        "number or true/false",
        b'{"profiles": {"work": {"host": null}}}': "`host` in profile `work` must be a string, "
        "number or true/false",
        b'{"default_profile": "home", "profiles": {"work": {}}}': "default_profile `home` is not "
        "one of its profiles",
    }
    for raw, want in cases.items():
        assert parse_settings_file(raw, SETTINGS, "notes") == (None, want), raw
    raw = b'{"profiles": {"w": {"region": true, "page_size": 1.50}}}'
    f, reason = parse_settings_file(raw, SETTINGS, "notes")
    assert (
        reason == ""
        and f is not None
        and f.profiles == {"w": {"region": "true", "page_size": "1.50"}}
    )


def test_doctor_settings_check(h: THarness) -> None:
    h.build = settings_app

    def check() -> dict[str, Any]:
        r = h.run("doctor")
        validate_doctor(r.data())
        return next(c for c in r.data()["checks"] if c["check"] == "settings")

    assert check() == {
        "check": "settings",
        "status": "pass",
        "message": "No settings file; defaults and environment variables apply.",
        "fix": None,
    }
    names = [c["check"] for c in h.run("doctor").data()["checks"]]
    assert names.index("authorization") + 1 == names.index("settings") == names.index("version") - 1
    path = settings_file(h, {"profiles": {"work": {}}})
    assert check()["message"] == f"{path} is valid; profile `work` is active (single)."
    settings_file(h, {"profiles": {"a": {}, "b": {}}})
    assert check()["message"] == f"{path} is valid; no profile is active."
    settings_file(h, "{")
    assert check() == {
        "check": "settings",
        "status": "fail",
        "message": f"The settings file {path} is invalid: it is not valid JSON.",
        "fix": f"Fix {path}, or move it aside to use defaults and environment variables.",
    }


def test_profile_flag_is_bound_into_the_token(h: THarness) -> None:
    h.build = settings_app
    settings_file(h, {"profiles": {"work": {}, "home": {}}})
    tok = h.must_token("note", "update", "1", "--title", "X", "--profile", "work")
    expect_mismatch(h.run("note", "update", "1", "--title", "X", "--confirm", tok), "profile")
    tok = h.must_token("note", "update", "1", "--title", "X", "--profile", "work")
    r = h.run("note", "update", "1", "--title", "X", "--profile", "work", "--confirm", tok)
    assert r.exit == 0, r


def test_profile_flag_exists_only_with_settings(h: THarness) -> None:
    r = h.run("note", "list", "--profile", "work")
    assert r.exit == 2 and r.details()["problem"] == "unknown_flag", r
    assert "profile" not in h.run("context").data(), "no settings, no context.profile"
    a = settings_app(Upstream())
    a.root.commands["note"].commands["list"].params.append(click.Option(["--profile"]))
    a.settings = [*SETTINGS, Setting("Host"), Setting("host")]
    a.install_builtins()
    problems = a.check_tree()
    assert (
        '"note list" defines --profile, which aicli reserves for the active profile when '
        "App.settings is set (CLI-101)"
    ) in problems, problems
    assert 'App.settings[4]: "Host" is not a snake_case name' in problems, problems
    assert 'App.settings: "host" is declared twice' in problems, problems


def test_profile_is_a_global_flag_in_reference(h: THarness) -> None:
    """--profile is listed once with the global flags, not in each command's params."""
    h.build = settings_app
    full = h.run("reference").data()
    assert "profile" in [f["name"] for f in full["global_flags"]], full["global_flags"]
    for c in full["commands"]:
        assert "profile" not in [p["name"] for p in c["params"]], c["path"]
    assert "--profile <name>" in h.run("note", "get", "--help").stdout
