"""The conformance suite against the test tool (port of conformance_test.go)."""

from __future__ import annotations

import os
import secrets
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click
import pytest
from aicli_testapp import THarness, Upstream, notes_app

import aicli
from aicli import (
    App,
    FieldChange,
    Harness,
    Op,
    Outcome,
    Preview,
    Recorder,
    Result,
    Target,
    WriteSpec,
)

TEST_SAMPLES = {
    "note create": ["--title", "Hello", "--body", "Body text", "--tag", "a,b"],
    "note update": ["1", "--title", "New title"],
    "note delete": ["2"],
    "note share": ["1", "--to", "bob@example.com", "--token", "sample-secret-token"],
    "note bulk-tag": ["--ids", "1,2", "--tag", "work"],
    "note comment add": ["1", "--text", "Looks good"],
}


# holds the conformance sample's --output file: inside the scope's outputs (G04
# creates it) and roots (the later stages plan to overwrite it, C1). Only its name
# is chosen on import: the module fixtures that use it create and remove it, so a
# run that selects none of their tests leaves nothing behind.
EXPORT_DIR = os.path.join(tempfile.gettempdir(), "aicli-conformance-export-" + secrets.token_hex(8))


@pytest.fixture(scope="module", autouse=True)
def _remove_export_dir():  # noqa: ANN202 - pytest fixture
    os.makedirs(EXPORT_DIR, exist_ok=True)
    yield
    shutil.rmtree(EXPORT_DIR, ignore_errors=True)


def harness_for(build: Callable[[Upstream], App] = notes_app) -> tuple[Harness, Upstream]:
    u = Upstream()
    samples = {"note export": ["--output", os.path.join(EXPORT_DIR, "notes.tsv")], **TEST_SAMPLES}
    return (
        Harness(
            new_app=lambda env: build(u),
            writes=u.writes,
            reset=u.reset,
            samples=samples,
            scope={
                "scope_id": "scp_conformance",
                "tool": "notes",
                "granted_by": "test",
                "granted_at": "2026-01-01T00:00:00Z",
                "expires_at": None,
                "effects": ["change", "deliver", "artifact", "persist"],
                "dangerous": True,
                "destinations": ["bob@example.com"],
                "outputs": [EXPORT_DIR],
                "roots": [EXPORT_DIR],
            },
        ),
        u,
    )


def test_conformance_suite_passes_for_the_test_tool() -> None:
    h, _ = harness_for()
    aicli.run_conformance(h)


def test_conformance_raises_listing_every_failure() -> None:
    def build(u: Upstream) -> App:
        a = notes_app(u)
        a.risk_tier = "T0"
        return a

    h, _ = harness_for(build)
    with pytest.raises(AssertionError) as info:
        aicli.run_conformance(h)
    assert "R02" in str(info.value), info.value


def _cmd(a: App, path: str) -> Any:
    c = a.root
    for w in path.split():
        c = c.commands[w]
    return c


def test_conformance_catches_a_write_that_skips_the_gate() -> None:
    def build(u: Upstream) -> App:
        a = notes_app(u)
        c = _cmd(a, "note comment add")
        orig = c.callback

        def callback(**kw: Any) -> None:
            u.write(lambda: None)  # writes before asking the gate
            orig(**kw)

        c.callback = callback
        return a

    h, _ = harness_for(build)
    rec = Recorder()
    aicli.run_conformance(h, rec)
    assert rec.has("G01"), "\n".join(rec.errors)


def test_conformance_catches_a_missing_sample() -> None:
    h, _ = harness_for()
    samples = {k: v for k, v in h.samples.items() if k != "note share"}
    samples["note nothing"] = ["x"]
    h.samples = samples
    rec = Recorder()
    aicli.run_conformance(h, rec)
    assert rec.has('G00: write command "note share" has no sample'), "\n".join(rec.errors)
    assert rec.has('G00: Harness.samples names "note nothing"'), "\n".join(rec.errors)


def test_conformance_binding_covers_an_ignored_input() -> None:
    """The tool ignores --title: the binding is generic, so --title is still bound
    and G03 passes; a tool cannot opt an input out of the token."""

    def build(u: Upstream) -> App:
        a = notes_app(u)

        def callback(nid: Any, **kw: Any) -> None:
            def apply(op: Op) -> Result:
                u.write(lambda: None)
                return Result()

            a.write(
                None,
                WriteSpec(
                    target=Target("notes/" + (nid or "")),
                    preview=lambda: Preview(
                        can_apply=True, changes=[FieldChange("title", after="fixed")]
                    ),
                    apply=apply,
                ),
            )

        _cmd(a, "note update").callback = callback
        return a

    h, _ = harness_for(build)
    rec = Recorder()
    aicli.run_conformance(h, rec)
    assert not [e for e in rec.errors if e.startswith("G03")], rec.errors


def test_conformance_catches_an_unverified_pass() -> None:
    """A receipt the gate had to correct (verification passed with nothing observed)
    is still a tool bug: the suite reports it under G04."""

    def build(u: Upstream) -> App:
        a = notes_app(u)

        def callback(nid: Any, **kw: Any) -> None:
            def apply(op: Op) -> Result:
                u.write(lambda: None)
                return Result(outcome=Outcome(verification="passed"))

            a.write(
                None,
                WriteSpec(
                    target=Target("notes/" + (nid or "") + "#comments", "comments"),
                    preview=lambda: Preview(
                        action="Comment",
                        can_apply=True,
                        changes=[FieldChange("comments", add=["x"])],
                    ),
                    apply=apply,
                ),
            )

        _cmd(a, "note comment add").callback = callback
        return a

    h, _ = harness_for(build)
    rec = Recorder()
    aicli.run_conformance(h, rec)
    assert rec.has(
        "G04: note comment add: Apply reported verification passed with an empty Result.observed"
    ), "\n".join(rec.errors)


def test_conformance_catches_bad_self_description() -> None:
    def build(u: Upstream) -> App:
        a = notes_app(u)
        a.risk_tier = "T0"
        a.root.add_command(click.Command("orphan", callback=lambda: a.ok(None, None)))
        return a

    h, _ = harness_for(build)
    rec = Recorder()
    aicli.run_conformance(h, rec)
    assert rec.has("R02"), "\n".join(rec.errors)


def test_coverage_recorder_and_r06(tmp_path: Path) -> None:
    cov = str(tmp_path / "coverage.jsonl")
    os.makedirs(tmp_path / "h")
    th = THarness(str(tmp_path / "h"))
    th.env["NOTES_COVERAGE_FILE"] = cov
    th.run("note", "get", "1")
    th.run("note", "list")
    with open(cov, encoding="utf-8") as fh:
        text = fh.read()
    assert '"command":"note get"' in text, text
    h, _ = harness_for()
    h.coverage_file = cov
    rec = Recorder()
    aicli.run_conformance(h, rec)
    assert rec.has('R06: command "note create" was never executed'), "\n".join(rec.errors)
    assert not rec.has('R06: command "note get"'), "\n".join(rec.errors)
    rec2 = Recorder()
    aicli.check_coverage(h, rec2)
    assert rec2.has('R06: command "note create" was never executed') and not rec2.has(
        'R06: command "note list"'
    )
