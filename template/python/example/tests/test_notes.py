"""The notes example: the conformance suite and focused tests (port of the Go
example's notes_test.go). The last test checks R06 coverage over this module."""

from __future__ import annotations

import io
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from notes.cli import new_app
from notes.store import APIError, MemStore
from notes_coverage import COVERAGE_FILE, ENVELOPES, FAKE_API

import aicli

SAMPLES = {
    "note create": ["--title", "Groceries", "--body", "Milk", "--tag", "home"],
    "note update": ["1", "--title", "Weekly groceries"],
    "note delete": ["3"],
    "note share": ["1", "--to", "bob@example.com", "--token", "share-token-123"],
    "note bulk-tag": ["--ids", "1,2", "--tag", "urgent"],
}


def offline(app: aicli.App) -> aicli.App:
    """The tool with its update built-in pointed at the local API stand-in (doctor
    refreshes the update notice): the tests never use the network."""
    assert app.update is not None
    app.update.api_base = FAKE_API
    return app


def harness(store: MemStore) -> aicli.Harness:
    """Connects the conformance suite to the notes tool and its fake service."""
    return aicli.Harness(
        new_app=lambda env: offline(new_app(store, env)),
        writes=store.writes,
        reset=store.reset,
        samples=SAMPLES,
        scope={
            "scope_id": "scp_notes_tests",
            "tool": "notes",
            "granted_by": "notes tests",
            "granted_at": "2026-10-12T00:00:00Z",
            "expires_at": None,
            "effects": ["change", "deliver"],
            "dangerous": True,
            "targets": [{"key": "notes"}],
            "destinations": ["bob@example.com"],
        },
    )


def test_conformance() -> None:
    aicli.run_conformance(harness(MemStore()))


# ---------------------------------------------------------------- focused tests


def full_scope() -> dict[str, Any]:
    return {
        "scope_id": "scp_test",
        "tool": "notes",
        "granted_by": "tester",
        "granted_at": "2026-10-12T00:00:00Z",
        "expires_at": None,
        "effects": ["change", "deliver"],
        "dangerous": True,
        "destinations": ["bob@example.com"],
    }


@dataclass
class Out:
    exit: int
    out: str
    err: str
    doc: dict[str, Any] = field(default_factory=dict)

    def data(self) -> dict[str, Any]:
        d = self.doc.get("data")
        return d if isinstance(d, dict) else {}

    def details(self) -> dict[str, Any]:
        e = self.doc.get("error")
        d = e.get("details") if isinstance(e, dict) else None
        return d if isinstance(d, dict) else {}

    def code(self) -> str:
        e = self.doc.get("error")
        return e.get("code", "") if isinstance(e, dict) else ""

    def token(self) -> str:
        t = self.data().get("confirm_token") or self.details().get("confirm_token")
        return t if isinstance(t, str) else ""


class Env:
    """A test's own service, state directory and scope."""

    def __init__(self, d: Path, scope: dict[str, Any] | None) -> None:
        self.store = MemStore()
        self.dir = d
        self.stdin = ""
        self.vars = {
            "NOTES_STATE_DIR": str(d / "state"),
            "NOTES_ACCOUNT": "tester",
            "NOTES_SCOPE_FILE": "",
        }
        if scope is not None:
            path = d / "scope.json"
            path.write_text(json.dumps(scope), encoding="utf-8")
            self.vars["NOTES_SCOPE_FILE"] = str(path)

    def run(self, *args: str) -> Out:
        app = offline(new_app(self.store, self.vars))
        out, err = io.BytesIO(), io.BytesIO()
        app.stdout, app.stderr = out, err
        app.stdin = io.BytesIO(self.stdin.encode("utf-8"))
        r = Out(
            app.execute(list(args)), out.getvalue().decode("utf-8"), err.getvalue().decode("utf-8")
        )
        if r.out.strip().startswith("{"):
            r.doc = json.loads(r.out)
            aicli.validate_envelope(out.getvalue())
            ENVELOPES.append(out.getvalue())
        return r

    def dry_run(self, *args: str) -> str:
        r = self.run(*args, "--dry-run")
        assert r.exit == 0 and r.token(), f"dry-run {args}: {r.out}"
        return r.token()


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path, None)


@pytest.fixture
def scoped(tmp_path: Path) -> Env:
    return Env(tmp_path, full_scope())


def test_list_pages_and_default_fields(env: Env) -> None:
    r = env.run("note", "list", "--limit", "2")
    assert (
        r.exit == 0 and r.data()["has_more"] is True and r.data().get("next_offset") is not None
    ), r.out
    first = r.data()["items"][0]
    assert "body" not in first and first["ref"] == "notes/1", first
    assert r.data()["total"] == 3, r.data()
    r = env.run("note", "list", "--offset", "2", "--fields", "+body")
    assert r.data()["has_more"] is False and "body" in r.data()["items"][0], r.out
    r = env.run("note", "list", "--tag", "home")
    assert r.data()["scope"]["tag"] == "home" and len(r.data()["items"]) == 2, r.out


def test_get_accepts_what_the_agent_has(env: Env) -> None:
    for args in (
        ["note", "get", "2"],
        ["note", "get", "notes/2"],
        ["note", "get", "https://notes.example/notes/2"],
        ["note", "get", "--id", "2"],
        ["note", "view", "2"],
        ["notes", "show", "2"],
    ):
        r = env.run(*args)
        assert r.exit == 0 and r.data()["title"] == "Release plan", (args, r.out)
    r = env.run("note", "get", "99")
    assert r.exit == 3 and r.code() == "E_NOT_FOUND", r.out
    r = env.run("note", "get", "abc")
    assert r.exit == 2 and r.code() == "E_VALIDATION", r.out
    r = env.run("note", "get")
    assert (
        r.exit == 2
        and r.details()["problem"] == "missing_parameter"
        and r.details()["parameter"] == "--id"
    ), r.out


def test_create_binds_the_body_content(scoped: Env) -> None:
    f = scoped.dir / "body.txt"
    f.write_text("Milk and eggs", encoding="utf-8")
    tok = scoped.dry_run("note", "create", "--title", "Shopping", "--body", "Milk and eggs")
    r = scoped.run("note", "create", "--title", "Shopping", "--body-file", str(f), "--confirm", tok)
    assert r.exit == 0 and scoped.store.writes() == 1, r.out
    tok = scoped.dry_run("note", "create", "--title", "Shopping 2", "--body-file", str(f))
    f.write_text("Milk, eggs and a surprise", encoding="utf-8")
    r = scoped.run(
        "note", "create", "--title", "Shopping 2", "--body-file", str(f), "--confirm", tok
    )
    assert (
        r.code() == "E_CONFLICT"
        and r.details()["parameter"] == "--body"
        and scoped.store.writes() == 1
    ), r.out


def test_body_from_stdin(scoped: Env) -> None:
    scoped.stdin = "Piped body"
    tok = scoped.dry_run("note", "create", "--title", "Piped", "--body-file", "-")
    r = scoped.run("note", "create", "--title", "Piped", "--body-file", "-", "--confirm", tok)
    assert r.exit == 0 and r.data()["result"]["body"] == "Piped body", r.out


def test_update_refuses_a_drifted_note(scoped: Env) -> None:
    tok = scoped.dry_run("note", "update", "1", "--title", "Mine")
    scoped.store.update("1", None, None, ["edited-elsewhere"], [])
    r = scoped.run("note", "update", "1", "--title", "Mine", "--confirm", tok)
    assert r.code() == "E_CONFLICT" and r.details()["conflict"] == "state_drift", r.out
    assert scoped.store.get("1").title == "Groceries"


def test_update_changes_tags(scoped: Env) -> None:
    args = ["note", "update", "notes/2", "--add-tag", "urgent", "--remove-tag", "work"]
    r = scoped.run(*args, "--confirm", scoped.dry_run(*args))
    assert r.exit == 0 and r.data()["result"]["tags"] == ["urgent"], r.out
    assert r.data()["receipt"]["outcome"]["verification"] == "passed", r.out


def test_delete_needs_dangerous_and_is_verified(scoped: Env) -> None:
    r = scoped.run("note", "delete", "3", "--dry-run")
    assert r.exit == 5 and r.details()["problem"] == "dangerous_flag_required", r.out
    tok = scoped.dry_run("note", "delete", "3", "--dangerous")
    r = scoped.run("note", "rm", "notes/3", "--dangerous", "--confirm", tok)
    assert r.exit == 0, r.out
    assert r.data()["receipt"]["outcome"]["verification"] == "passed", r.out
    with pytest.raises(APIError):
        scoped.store.get("3")


def test_share_checks_the_destination_and_hides_the_token(scoped: Env) -> None:
    secret = "s3cr3t-share-token"
    r = scoped.run(
        "note",
        "share",
        "1",
        "--to",
        "mallory@example.com",
        "--token",
        secret,
        "--dangerous",
        "--dry-run",
    )
    assert r.code() == "E_FORBIDDEN" and r.details()["scope"]["status"] == "insufficient", r.out
    tok = scoped.dry_run(
        "note", "share", "1", "--to", "bob@example.com", "--token", secret, "--dangerous"
    )
    r = scoped.run(
        "note",
        "share",
        "1",
        "--to",
        "bob@example.com",
        "--token",
        secret,
        "--dangerous",
        "--confirm",
        tok,
    )
    assert r.exit == 0 and r.data()["receipt"]["outcome"]["delivery"] == "delivered", r.out
    assert secret not in r.out + r.err
    for root, _dirs, files in os.walk(scoped.vars["NOTES_STATE_DIR"]):
        for name in files:
            with open(os.path.join(root, name), "rb") as fh:
                assert secret.encode() not in fh.read(), name


def test_refused_writes_are_not_unknown(scoped: Env) -> None:
    tok = scoped.dry_run(
        "note", "share", "1", "--to", "bob@example.com", "--token", "revoked-token", "--dangerous"
    )
    r = scoped.run(
        "note",
        "share",
        "1",
        "--to",
        "bob@example.com",
        "--token",
        "revoked-token",
        "--dangerous",
        "--confirm",
        tok,
    )
    assert (
        r.exit == 4 and r.code() == "E_FORBIDDEN" and r.details()["dispatch_state"] == "dispatched"
    ), r.out
    assert "error.details.upstream_message" in r.doc["meta"]["untrusted"], r.out


def test_server_error_during_write_is_unknown_until_resolved(scoped: Env) -> None:
    tok = scoped.dry_run("note", "update", "2", "--title", "Maybe")
    scoped.store.fail = lambda op, nid: APIError(502, "bad gateway")
    r = scoped.run("note", "update", "2", "--title", "Maybe", "--confirm", tok)
    assert (
        r.exit == 6
        and r.code() == "E_OUTCOME_UNKNOWN"
        and "operation show" in r.details()["lookup"]
    ), r.out
    scoped.store.fail = None
    op_id = r.details()["operation_id"]
    blocked = scoped.run("note", "update", "2", "--title", "Again", "--dry-run")
    assert blocked.details().get("conflict") == "unresolved_operation", blocked.out
    show = scoped.run("operation", "show", op_id)
    assert show.data()["status"] == "unknown", show.out
    res = scoped.run(
        "operation",
        "resolve",
        op_id,
        "--outcome",
        "not_applied",
        "--evidence",
        "note 2 still has its old title",
    )
    assert res.exit == 0 and res.data()["status"] == "resolved", res.out
    assert scoped.run("note", "update", "2", "--title", "Again", "--dry-run").exit == 0


def test_network_failure_before_send_is_not_dispatched(scoped: Env) -> None:
    tok = scoped.dry_run("note", "create", "--title", "Offline")
    scoped.store.fail = lambda op, nid: APIError(0, "connection refused")
    r = scoped.run("note", "create", "--title", "Offline", "--confirm", tok)
    assert (
        r.exit == 7 and r.code() == "E_NETWORK" and r.details()["dispatch_state"] == "not_started"
    ), r.out
    assert scoped.store.writes() == 0


def test_bulk_tag_reports_each_item(scoped: Env) -> None:
    tok = scoped.dry_run("note", "bulk-tag", "--ids", "1,notes/2,3", "--tag", "urgent")
    scoped.store.fail = lambda op, nid: APIError(409, "note is locked") if nid == "2" else None
    r = scoped.run("note", "bulk-tag", "--ids", "1,notes/2,3", "--tag", "urgent", "--confirm", tok)
    assert r.exit == 1 and r.code() == "E_PARTIAL", r.out
    s = r.details()["summary"]
    assert s["succeeded"] == 2 and s["failed"] == 1, s


def test_idempotent_create_returns_the_first_result(scoped: Env) -> None:
    args = ["note", "create", "--title", "Once", "--idempotency-key", "create-once-1"]
    first = scoped.run(*args, "--confirm", scoped.dry_run(*args))
    # a repeat with the same key is answered with the replay, even at dry-run (P4)
    second = scoped.run(*args, "--dry-run")
    assert first.exit == 0 and second.exit == 0 and scoped.store.writes() == 1, (
        first.out,
        second.out,
    )
    assert second.data()["receipt"]["replayed"] is True, second.out
    assert second.data()["operation_id"] == first.data()["operation_id"], second.out


def test_agent_guesses_are_accepted_or_answered(scoped: Env) -> None:
    """The example's guess list (CLI-022): the forms agents type first are accepted,
    and the rest get an exact did_you_mean and the corrected command."""
    accepted = [
        ["note", "ls"],
        ["note", "list", "--label", "home"],
        ["note", "list", "--labels", "home"],
        ["note", "list", "--tags", "home"],
        ["notes", "list"],
        ["note", "view", "1"],
        ["note", "show", "1"],
        ["note", "add", "--title", "x", "--dry-run"],
        ["note", "new", "--title", "x", "--text", "body", "--dry-run"],
        ["note", "create", "--title", "x", "--message", "body", "--dry-run"],
        ["note", "edit", "1", "--title", "y", "--dry-run"],
        ["note", "rm", "3", "--dangerous", "--dry-run"],
    ]
    for args in accepted:
        r = scoped.run(*args)
        assert r.exit == 0, (args, r.out)
    answered = [
        (["note", "lst"], "unknown_command", "notes note list", "`notes note list`"),
        (["note", "edti", "1"], "unknown_command", "notes note update", "`notes note update 1`"),
        (
            # list takes no positional argument, so the rewrite points to its flags
            # rather than to a command line that would be refused.
            ["note", "search", "groceries"],
            "unknown_command",
            "notes note list",
            "`notes note list --help`",
        ),
        (["note", "find"], "unknown_command", "notes note list", "`notes note list`"),
        (["bulk-tag"], "unknown_command", "notes note bulk-tag", "`notes note bulk-tag`"),
        (["get", "1"], "unknown_command", "notes note get", "`notes note get 1`"),
        (["note", "list", "--all"], "unknown_flag", "--limit", "--limit <n>"),
        (
            ["note", "list", "--tagg", "home"],
            "unknown_flag",
            "--tag",
            "`notes note list --tag home`",
        ),
    ]
    for args, problem, suggest, nxt in answered:
        r = scoped.run(*args)
        assert r.exit == 2 and r.details()["problem"] == problem, (args, r.out)
        assert (r.details().get("did_you_mean") or [None])[0] == suggest, (args, r.out)
        assert nxt in r.details()["next_action"], (args, r.details()["next_action"])
    r = scoped.run("note", "list", "extra")
    assert r.details()["problem"] == "unexpected_argument", r.out
    r = scoped.run("note", "create", "Groceries")
    assert "`notes note create --title Groceries`" in r.details()["next_action"], r.out
    r = scoped.run("note", "update", "2")
    assert r.exit == 2 and r.details()["problem"] == "missing_parameter", r.out


def test_command_map_is_the_skill_block() -> None:
    """The Skill command map (decision 4): the block SKILL-SPEC and the Skill template show."""
    want = "- `note`: list, get, create, update, delete (dangerous), share (dangerous), bulk-tag\n"
    assert new_app(MemStore()).command_map() == want


def test_share_token_comes_from_the_environment(scoped: Env) -> None:
    """NOTES_SHARE_TOKEN is the documented path (SEC-040), and an environment value
    is bound like a flag (CLI-054)."""
    scoped.vars["NOTES_SHARE_TOKEN"] = "revoked-token"
    tok = scoped.dry_run("note", "share", "1", "--to", "bob@example.com", "--dangerous")
    scoped.vars["NOTES_SHARE_TOKEN"] = "another-token"
    r = scoped.run("note", "share", "1", "--to", "bob@example.com", "--dangerous", "--confirm", tok)
    assert (
        r.code() == "E_CONFLICT"
        and r.details()["parameter"] == "--token"
        and scoped.store.writes() == 0
    ), r.out
    scoped.vars["NOTES_SHARE_TOKEN"] = "revoked-token"
    tok = scoped.dry_run("note", "share", "1", "--to", "bob@example.com", "--dangerous")
    r = scoped.run("note", "share", "1", "--to", "bob@example.com", "--dangerous", "--confirm", tok)
    assert r.code() == "E_FORBIDDEN" and r.details().get("required"), r.out
    assert "NOTES_SHARE_TOKEN" in r.details()["next_action"], r.out
    assert "revoked-token" not in r.out + r.err


def test_built_ins_describe_the_tool(scoped: Env) -> None:
    idx = scoped.run("reference", "--index")
    assert idx.exit == 0 and len(idx.out) <= 8000, len(idx.out)
    ctx = scoped.run("context")
    assert (
        ctx.data()["account"] == "tester" and ctx.data()["authorization"]["status"] == "granted"
    ), ctx.out
    doc = scoped.run("doctor")
    assert doc.exit == 0 and len(doc.data()["checks"]) >= 5, doc.out
    cl = scoped.run("changelog", "--since", "0.0.1")
    assert cl.exit == 0 and len(cl.data()["entries"]) == 1, cl.out
    hlp = scoped.run("--help")
    assert "note bulk-tag" in hlp.out and "--confirm <token>" in hlp.out, hlp.out
    lst = scoped.run("operation", "list")
    assert lst.exit == 0, lst.out


def test_update_and_the_host_setting(scoped: Env) -> None:
    """The update built-in and the host setting (CLI-070, CLI-100): this checkout runs
    from source, so it cannot update itself; NOTES_HOST, else the active profile of
    the settings file, else the default sets the host context reports."""
    r = scoped.run("update", "--check")
    assert r.exit == 2 and r.code() == "E_UNSUPPORTED", r.out
    ctx = scoped.run("context").data()
    assert ctx["host"] == "memory://notes" and ctx["profile"]["source"] == "none", ctx
    settings = scoped.dir / "settings.json"
    settings.write_text(
        json.dumps({"profiles": {"work": {"host": "https://notes.work.example"}}}), encoding="utf-8"
    )
    scoped.vars["NOTES_SETTINGS_FILE"] = str(settings)
    ctx = scoped.run("context").data()
    assert ctx["host"] == "https://notes.work.example" and ctx["profile"]["name"] == "work", ctx
    scoped.vars["NOTES_HOST"] = "https://notes.env.example"
    assert scoped.run("context").data()["host"] == "https://notes.env.example"


def test_zz_every_command_is_covered(request: pytest.FixtureRequest) -> None:
    """R06 (VER-003): every registry command ran in this module's tests. Like the Go
    example's TestMain, it runs only when the whole module ran."""
    module = request.node.path
    selected = [i for i in request.session.items if i.path == module]
    defined = [name for name in globals() if name.startswith("test_")]
    if len(selected) != len(defined):
        pytest.skip("checks coverage only when the whole module runs")
    h = harness(MemStore())
    h.coverage_file = COVERAGE_FILE
    aicli.check_coverage(h)
