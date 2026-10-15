"""The scope file: who allowed what (port of scope_test.go, decision 29)."""

from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path
from typing import Any

import pytest
from aicli_testapp import THarness

from aicli._util import time_string
from aicli.scope import (
    TargetSelector,
    destination_allowed,
    parse_scope,
    path_within,
    subtree_match,
    validate_scope_file,
)

WRITES = [
    ["note", "update", "1", "--title", "X", "--dry-run"],
    ["note", "update", "1", "--title", "X"],
    ["note", "share", "1", "--to", "bob@example.com", "--dangerous", "--dry-run"],
    ["note", "bulk-tag", "--ids", "1,2", "--tag", "x", "--dry-run"],
]


def test_no_scope_refuses_every_write_before_sending(h: THarness) -> None:
    h.scope_on = False
    for args in WRITES:
        r = h.run(*args)
        scope = r.details().get("scope") or {}
        assert (
            r.exit == 4
            and r.code() == "E_FORBIDDEN"
            and scope.get("status") == "missing"
            and r.token() == ""
        ), (args, r)
        assert "do not create or edit it yourself" in r.details()["next_action"], r.details()[
            "next_action"
        ]
        ex = scope["grant_example"]
        assert ex["tool"] == "notes" and ex["granted_by"] == "<your name>", ex
    r = h.run("note", "update", "1", "--title", "X")
    p = r.details().get("preview")
    assert isinstance(p, dict) and p["command"] == "note update", r
    assert h.up.writes() == 0
    assert h.run("note", "get", "1").exit == 0


NOW = _dt.datetime.now(_dt.timezone.utc)
SCOPE_CASES: list[tuple[str, dict[str, Any] | None, bytes, str, list[str] | None]] = [
    ("expired", {"expires_at": time_string(NOW - _dt.timedelta(minutes=1))}, b"", "expired", None),
    ("revoked", {"revoked": True}, b"", "revoked", None),
    ("other tool", {"tool": "other-cli"}, b"", "invalid", None),
    ("unknown key", {"approved": True}, b"", "invalid", None),
    ("missing key", {"granted_by": None}, b"", "invalid", None),
    ("empty targets", {"targets": []}, b"", "insufficient", None),
    (
        "null targets",
        None,
        b'{"scope_id":"a","tool":"notes","granted_by":"x","granted_at":"2026-01-01T00:00:00Z","expires_at":null,'
        b'"effects":["change"],"dangerous":false,"targets":null}',
        "invalid",
        None,
    ),
    ("bad effect", {"effects": ["change", "launch"]}, b"", "invalid", None),
    ("bad time", {"expires_at": "tomorrow"}, b"", "invalid", None),
    ("relative output", {"outputs": ["out"]}, b"", "invalid", None),
    (
        "duplicate key",
        None,
        b'{"scope_id":"a","scope_id":"b","tool":"notes","granted_by":"x","granted_at":"2026-01-01T00:00:00Z",'
        b'"expires_at":null,"effects":["change"],"dangerous":false}',
        "invalid",
        None,
    ),
    (
        "nested duplicate",
        None,
        b'{"scope_id":"a","tool":"notes","granted_by":"x","granted_at":"2026-01-01T00:00:00Z","expires_at":null,'
        b'"effects":["change"],"dangerous":false,"targets":[{"key":"notes/","key":"x"}]}',
        "invalid",
        None,
    ),
    ("bom", None, b"\xef\xbb\xbf{}", "invalid", None),
    (
        "two documents",
        None,
        b'{"scope_id":"a","tool":"notes","granted_by":"x","granted_at":"2026-01-01T00:00:00Z","expires_at":null,'
        b'"effects":["change"],"dangerous":false} {}',
        "invalid",
        None,
    ),
    ("not an object", None, b'["change"]', "invalid", None),
    ("invalid utf-8", None, b'{"scope_id":"\xff"}', "invalid", None),
    ("missing effect", {"effects": ["deliver"]}, b"", "insufficient", None),
    (
        "not dangerous",
        {"dangerous": False},
        b"",
        "insufficient",
        ["note", "delete", "1", "--dangerous", "--dry-run"],
    ),
    ("target outside", {"targets": [{"key": "notes/2"}]}, b"", "insufficient", None),
    ("exact target", {"targets": [{"key": "notes", "match": "exact"}]}, b"", "insufficient", None),
    (
        "destination not granted",
        {"destinations": ["alice@example.com"]},
        b"",
        "insufficient",
        ["note", "share", "1", "--to", "bob@example.com", "--dangerous", "--dry-run"],
    ),
    # the contract's scope schema: max_operations is at least 1, a scope expires
    # after it was granted, and times are UTC with the Z suffix
    ("zero budget", {"limits": {"max_operations": 0}}, b"", "invalid", None),
    (
        "expires before granted",
        {"granted_at": time_string(NOW), "expires_at": time_string(NOW - _dt.timedelta(minutes=5))},
        b"",
        "invalid",
        None,
    ),
    ("local time", {"granted_at": "2026-01-01T08:00:00+08:00"}, b"", "invalid", None),
]


@pytest.mark.parametrize(
    ("name", "change", "raw", "status", "args"), SCOPE_CASES, ids=[c[0] for c in SCOPE_CASES]
)
def test_scope_statuses(
    h: THarness,
    name: str,
    change: dict[str, Any] | None,
    raw: bytes,
    status: str,
    args: list[str] | None,
) -> None:
    if raw:
        h.write_scope_raw(raw)
    else:
        h.grant(change)
    r = h.run(*(args or ["note", "update", "1", "--title", "X", "--dry-run"]))
    scope = r.details().get("scope") or {}
    assert r.exit == 4 and r.code() == "E_FORBIDDEN" and scope.get("status") == status, r
    assert h.up.writes() == 0


def test_budget_spent_after_the_last_operation(h: THarness) -> None:
    h.grant({"limits": {"max_operations": 1}})
    tok = h.must_token("note", "update", "1", "--title", "A")
    assert h.run("note", "update", "1", "--title", "A", "--confirm", tok).exit == 0
    r = h.run("note", "update", "1", "--title", "B", "--dry-run")
    assert (
        r.exit == 4
        and r.code() == "E_FORBIDDEN"
        and r.details()["scope"]["status"] == "budget_spent"
    ), r
    assert h.run("context").data()["authorization"]["status"] == "budget_spent"


def test_non_canonical_target_keys_are_refused(h: THarness) -> None:
    h.grant({"targets": [{"key": "notes/"}]})
    for nid in ("../admin", "1/../../admin", "..", "1/./2", "1\\..\\x", "1\x00"):
        r = h.run("note", "update", nid, "--title", "X", "--dry-run")
        assert (
            r.exit == 2 and r.code() == "E_VALIDATION" and r.details().get("parameter") == "target"
        ), (nid, r)
    assert h.up.writes() == 0


def test_scope_checked_again_at_confirm(h: THarness) -> None:
    tok = h.must_token("note", "update", "1", "--title", "X")
    h.grant({"revoked": True})
    r = h.run("note", "update", "1", "--title", "X", "--confirm", tok)
    assert r.exit == 4 and r.details()["scope"]["status"] == "revoked" and h.up.writes() == 0, r


def test_scope_budget_counts_dispatched_operations(h: THarness, tmp_path: Path) -> None:
    h.grant({"limits": {"max_operations": 2}})
    for title in ("A", "B"):
        tok = h.must_token("note", "update", "1", "--title", title)
        r = h.run("note", "update", "1", "--title", title, "--confirm", tok)
        assert r.exit == 0, r
    r = h.run("note", "update", "1", "--title", "C", "--dry-run")
    assert r.exit == 4 and r.details()["scope"]["status"] == "budget_spent", r
    os.makedirs(tmp_path / "second")
    h2 = THarness(str(tmp_path / "second"))
    h2.grant({"limits": {"max_operations": 1}})
    r = h2.run("note", "bulk-tag", "--ids", "1,2", "--tag", "x", "--dry-run")
    assert r.exit == 4 and r.details()["scope"]["status"] == "budget_spent", r


def test_subtree_match() -> None:
    cases = [
        (TargetSelector("acme"), "acme", True),
        (TargetSelector("acme"), "acme/web", True),
        (TargetSelector("acme"), "acme/web!5", True),
        (TargetSelector("acme"), "acme!5", True),
        (TargetSelector("acme"), "acme#3", True),
        (TargetSelector("acme"), "acme:x", True),
        (TargetSelector("acme"), "acme@x", True),
        (TargetSelector("acme"), "acmecorp", False),
        (TargetSelector("acme"), "acme-evil", False),
        (TargetSelector("acme"), "acm", False),
        (TargetSelector("acme/"), "acme/web", True),
        (TargetSelector("acme/"), "acme", False),
        (TargetSelector("acme/web"), "acme/web!5", True),
        (TargetSelector("acme/web"), "acme/webhooks", False),
        (TargetSelector("legacy/payments", "exact"), "legacy/payments", True),
        (TargetSelector("legacy/payments", "exact"), "legacy/payments!1", False),
        (TargetSelector(""), "anything", False),
    ]
    for sel, key, want in cases:
        assert subtree_match(sel, key) == want, (sel, key)


GRANTED = [
    "bob@example.com",
    "https://hooks.example.com/notes",
    "#general",
    "mailto:alice@acme.com",
    "team/",
]
DESTINATIONS = {
    "bob@example.com": True,
    "alice@example.com": False,
    # '@', ':', '#', '!' and '/' are ordinary characters of a non-URL destination,
    # never append points (decision 29, GO-GATE-01).
    "bob@example.com@evil.com": False,
    "bob@example.com/evil": False,
    "bob@example.com:evil": False,
    "bob@example.com#evil": False,
    "bob@example.com!evil": False,
    "bob@example.com:x@evil.org": False,
    "bob@example.com/x@evil.org": False,
    "bob@example.com.evil.com": False,
    "mailto:alice@acme.com": True,
    "mailto:alice@acme.com@evil.com": False,
    "#general:secret": False,
    # a granted entry ending in "/" is a prefix at "/" boundaries only
    "team/": True,
    "team/alice": True,
    "team/a/b": True,
    "team": False,
    "teams/alice": False,
    "team/../admin": False,
    "team/./x": False,
    "team//x": False,
    "team/x/": False,
    "team/x\\..\\y": False,
    "team/x\x00": False,
    "https://hooks.example.com/notes": True,
    "https://hooks.example.com/notes/team": True,
    "https://HOOKS.example.com/notes/team": True,
    "https://hooks.example.com/notesx": False,
    "https://hooks.example.com/other": False,
    "https://hooks.example.com.evil.org/notes": False,
    "https://hooks.example.com@evil.org/notes": False,
    "https://evil.org/hooks.example.com/notes": False,
    "https://hooks.example.com/notes/../admin": False,
    "https://hooks.example.com/notes/%2e%2e/x": False,
    "https://hooks.example.com/notes?x=1": False,
    "https://hooks.example.com/notes#frag": False,
    "http://hooks.example.com/notes": False,
    "https://hooks.example.com/notes%2fadmin": False,
    "https://hooks.example.com\\notes\\x": False,
    "#general": True,
    "#general-secret": False,
    "https://hooks.example.com:8443/notes/team": False,
}


@pytest.mark.parametrize(("dest", "want"), sorted(DESTINATIONS.items()))
def test_destination_matching(dest: str, want: bool) -> None:
    assert destination_allowed(dest, GRANTED) == want


def test_destination_escape_is_refused_end_to_end(h: THarness) -> None:
    for to in (
        "bob@example.com@evil.com",
        "bob@example.com:x@evil.org",
        "bob@example.com/x@evil.org",
    ):
        r = h.run("note", "share", "1", "--to", to, "--dangerous", "--dry-run")
        scope = r.details().get("scope") or {}
        assert (
            r.exit == 4
            and r.code() == "E_FORBIDDEN"
            and scope.get("status") == "insufficient"
            and r.token() == ""
        ), (to, r)
    assert h.up.writes() == 0


def test_output_paths_resolve_links(tmp_path: Path) -> None:
    allowed, other = tmp_path / "allowed", tmp_path / "other"
    allowed.mkdir()
    other.mkdir()
    assert path_within(str(allowed / "a.txt"), [str(allowed)])
    assert path_within(str(allowed / "new" / "deep" / "a.txt"), [str(allowed)])
    assert not path_within(os.path.join(str(allowed), "..", "other", "a.txt"), [str(allowed)])
    assert not path_within(str(other) + "x", [str(other)])
    link = allowed / "escape"
    try:
        os.symlink(other, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pass
    else:
        assert not path_within(str(link / "a.txt"), [str(allowed)]), (
            "a symlink inside the root escaped it"
        )
    if os.name == "nt":
        assert path_within(str(allowed / "a.txt").upper(), [str(allowed)]), (
            "Windows paths compare case-insensitively"
        )


def test_context_reports_the_scope(h: THarness) -> None:
    auth = h.run("context").data()["authorization"]
    assert auth["status"] == "granted" and auth["scope_id"] == "scp_test", auth
    h.scope_on = False
    assert h.run("context").data()["authorization"]["status"] == "missing"


def test_parse_scope_reasons() -> None:
    sc, reason = parse_scope(b'{"scope_id":"a"}')
    assert sc is None and reason, reason
    good = (
        b'{"scope_id":"a","tool":"notes","granted_by":"x","granted_at":"2026-01-01T00:00:00Z","expires_at":null,'
        b'"effects":["change"],"dangerous":false}'
    )
    sc, reason = parse_scope(good)
    assert sc is not None and reason == "", reason
    assert validate_scope_file(good) == ""
    assert validate_scope_file(good[:-1] + b',"x":1}') == "unknown key `x`"
    assert (
        validate_scope_file(good[:-1] + b',"note":"x"}')
        == "unknown key `note` (did you mean `notes`?)"
    )
    assert validate_scope_file(good.replace(b'"dangerous":false', b'"dangerous":"no"')) == (
        "dangerous must be true or false, not string"
    )
    assert validate_scope_file(good[:-1] + b',"limits":{"max_operations":1.5}}') == (
        "limits.max_operations must be a number, not number 1.5"
    )
