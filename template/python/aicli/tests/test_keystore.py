"""The confirm key, the state directory and the journal (port of keystore_test.go)."""

from __future__ import annotations

import datetime as _dt
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from aicli_testapp import THarness, expect_conflict

import aicli
from aicli import App
from aicli._util import time_string
from aicli.journal import Journal, Record, TargetRef, effective_status
from aicli.keystore import KEY_SIZE

WINDOWS = os.name == "nt"


def must_write(p: str, b: bytes) -> None:
    with open(p, "wb") as fh:
        fh.write(b)


def test_key_is_created_once_with_private_mode(tmp_path: Path) -> None:
    a = App(name="notes", state_dir=str(tmp_path / "state"))
    state = a.ensure_state_dir()
    k1 = a.load_key(state, True)
    assert k1 is not None and len(k1) == KEY_SIZE
    assert a.load_key(state, False) == k1, "the key changed between loads"
    if not WINDOWS:
        assert stat.S_IMODE(os.stat(os.path.join(state, "confirm.key")).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(state).st_mode) == 0o700
    assert not [n for n in os.listdir(state) if ".tmp" in n], os.listdir(state)


def test_concurrent_key_creation_agrees_on_one_key(tmp_path: Path) -> None:
    a = App(name="notes", state_dir=str(tmp_path / "state"))
    state = a.ensure_state_dir()
    n = 24
    keys: list[bytes | None] = [None] * n
    errs: list[BaseException | None] = [None] * n
    start = threading.Event()

    def worker(i: int) -> None:
        start.wait()
        try:
            keys[i] = a.load_key(state, True)
        except BaseException as exc:  # noqa: BLE001 - reported below
            errs[i] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    start.set()
    for t in threads:
        t.join()
    assert errs == [None] * n, errs
    assert all(k == keys[0] and k is not None for k in keys), "two threads got different keys"


def _broken_cases() -> dict[str, object]:
    def symlink(p: str) -> None:
        target = os.path.join(os.path.dirname(p), "real.key")
        must_write(target, bytes(KEY_SIZE))
        try:
            os.symlink(target, p)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlinks unavailable: {exc}")

    cases: dict[str, object] = {
        "empty": lambda p: must_write(p, b""),
        "31 bytes": lambda p: must_write(p, bytes(31)),
        "33 bytes": lambda p: must_write(p, bytes(33)),
        "directory": lambda p: os.mkdir(p, 0o700),
        "symlink": symlink,
    }
    if not WINDOWS:

        def group_readable(p: str) -> None:
            must_write(p, bytes(KEY_SIZE))
            os.chmod(p, 0o640)

        cases["group readable"] = group_readable
        if os.geteuid() != 0:

            def unreadable(p: str) -> None:
                must_write(p, bytes(KEY_SIZE))
                os.chmod(p, 0)

            cases["unreadable"] = unreadable
    return cases


@pytest.mark.parametrize("name", sorted(_broken_cases()))
def test_broken_key_fails_closed(tmp_path: Path, name: str) -> None:
    a = App(name="notes", state_dir=str(tmp_path / "state"))
    state = a.ensure_state_dir()
    _broken_cases()[name](os.path.join(state, "confirm.key"))  # type: ignore[operator]
    for create in (True, False):
        with pytest.raises(aicli.Error) as info:
            a.load_key(state, create)
        assert info.value.code == "E_IO", info.value
        assert info.value.details.get("next_action"), "E_IO without next_action"


def test_state_dir_must_be_a_private_directory(tmp_path: Path) -> None:
    f = tmp_path / "afile"
    must_write(str(f), b"x")
    with pytest.raises(aicli.Error) as info:
        App(name="notes", state_dir=str(f)).ensure_state_dir()
    assert info.value.code == "E_IO"
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    try:
        os.symlink(real, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pass  # symlinks unavailable (Windows without the privilege): the junction test covers links
    else:
        with pytest.raises(aicli.Error) as info:
            App(name="notes", state_dir=str(link)).ensure_state_dir()
        assert info.value.code == "E_IO"
    if not WINDOWS:
        opened = tmp_path / "open"
        opened.mkdir()
        os.chmod(opened, 0o755)
        with pytest.raises(aicli.Error) as info:
            App(name="notes", state_dir=str(opened)).ensure_state_dir()
        assert info.value.code == "E_IO"


def test_state_dir_resolution() -> None:
    a = App(name="notes", env=lambda k: "/tmp/from-env" if k == "NOTES_STATE_DIR" else "")
    assert os.path.basename(a.state_dir_path()) == "from-env"
    a.state_dir = "/tmp/explicit"
    assert os.path.basename(a.state_dir_path()) == "explicit"
    b = App(name="notes", env=lambda k: "")
    assert os.path.basename(b.state_dir_path()) == ".notes"


def test_journal_path_blocked_fails_closed(h: THarness, tmp_path: Path) -> None:
    os.makedirs(h.state, mode=0o700)
    must_write(os.path.join(h.state, "operations"), b"not a dir")
    for extra in (["--dry-run"], []):
        r = h.run("note", "update", "1", "--title", "X", *extra)
        assert r.exit == 1 and r.code() == "E_IO" and "ct2_" not in r.stdout, (extra, r)
    os.makedirs(tmp_path / "second")
    h2 = THarness(str(tmp_path / "second"))
    tok = h2.must_token("note", "update", "1", "--title", "X")
    ops = os.path.join(h2.state, "operations")
    for name in os.listdir(ops):
        os.remove(os.path.join(ops, name))
    os.rmdir(ops)
    must_write(ops, b"not a dir")
    r = h2.run("note", "update", "1", "--title", "X", "--confirm", tok)
    assert r.exit == 1 and r.code() == "E_IO" and h2.up.writes() == 0, r


def _record(op_id: str, **kw: object) -> Record:
    base: dict[str, object] = {
        "operation_id": op_id,
        "kind": "single",
        "command": "x",
        "status": "planned",
        "target": TargetRef("k"),
        "created_at": "2026-01-01T00:00:00Z",
        "expires_at": "2026-01-01T00:10:00Z",
    }
    base.update(kw)
    return Record(**base)  # type: ignore[arg-type]


def test_atomic_record_writes_never_expose_partial_content(tmp_path: Path) -> None:
    j = Journal(str(tmp_path))
    op_id = "op_aaaaaaaaaaaaaaaaaaaaaaaaaa"
    small = _record(op_id)
    big = _record(op_id, preview={"pad": "x" * (64 << 10)})
    j.write(small)
    done = threading.Event()
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            for i in range(150):
                j.write(big if i % 2 == 0 else small)
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)
        finally:
            done.set()

    t = threading.Thread(target=writer)
    t.start()
    reads = 0
    while not done.is_set():
        r, exists = j.read(op_id)
        assert exists and r is not None, "read saw a missing or partial record"
        reads += 1
    t.join()
    assert not errors, errors
    assert reads > 0, "no reads happened"
    assert not [n for n in os.listdir(tmp_path) if ".tmp" in n or n.startswith("tmp")], os.listdir(
        tmp_path
    )


def test_consume_is_exclusive_across_threads(tmp_path: Path) -> None:
    j = Journal(str(tmp_path))
    n = 64
    wins: list[bool] = []
    lock = threading.Lock()
    start = threading.Event()

    def worker() -> None:
        start.wait()
        ok = j.consume("op_aaaaaaaaaaaaaaaaaaaaaaaaaa", _dt.datetime.now(_dt.timezone.utc))
        with lock:
            wins.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    start.set()
    for t in threads:
        t.join()
    assert len(wins) == n and wins.count(True) == 1, wins.count(True)
    marker = tmp_path / "op_aaaaaaaaaaaaaaaaaaaaaaaaaa.consumed"
    assert marker.read_bytes().startswith(
        b'{"operation_id":"op_aaaaaaaaaaaaaaaaaaaaaaaaaa","consumed_at":"'
    )


HELPER = r"""
import io, os, sys, time
sys.path[:0] = [os.environ["AICLI_HELPER_PATH"], os.environ["AICLI_HELPER_TESTS"]]
from aicli_testapp import Upstream, notes_app
go = os.environ["AICLI_HELPER_GO"]
while not os.path.exists(go):
    time.sleep(0.001)
u = Upstream()
counter = os.environ["AICLI_HELPER_COUNTER"]
def hook(kind, nid):
    with open(counter, "a", encoding="utf-8") as fh:
        fh.write("write\n")
    return None
u.hook = hook
a = notes_app(u)
a.state_dir = os.environ["AICLI_HELPER_STATE"]
scope = os.environ["AICLI_HELPER_SCOPE"]
a.env = lambda k: scope if k == "NOTES_SCOPE_FILE" else ""
out = io.BytesIO()
a.stdout = a.stderr = out
token = os.environ["AICLI_HELPER_TOKEN"]
code = a.execute(["note", "update", "1", "--title", "Proc", "--confirm", token])
with open(os.environ["AICLI_HELPER_RESULT"], "wb") as fh:
    fh.write(str(code).encode() + b"\n" + out.getvalue())
"""


@pytest.mark.slow
def test_concurrent_confirms_from_processes(h: THarness) -> None:
    tok = h.must_token("note", "update", "1", "--title", "Proc")
    go = os.path.join(h.dir, "go")
    counter = os.path.join(h.dir, "counter")
    here = os.path.dirname(os.path.abspath(__file__))
    n = 4
    procs = []
    for i in range(n):
        env = dict(os.environ)
        env.update(
            PYTHONDONTWRITEBYTECODE="1",
            AICLI_HELPER_PATH=os.path.dirname(os.path.dirname(here)),
            AICLI_HELPER_TESTS=here,
            AICLI_HELPER_GO=go,
            AICLI_HELPER_COUNTER=counter,
            AICLI_HELPER_STATE=h.state,
            AICLI_HELPER_SCOPE=h.scope,
            AICLI_HELPER_TOKEN=tok,
            AICLI_HELPER_RESULT=os.path.join(h.dir, f"result{i}"),
        )
        procs.append(subprocess.Popen([sys.executable, "-c", HELPER], env=env))
    time.sleep(1.5)  # let every child import and wait at the start line
    must_write(go, b"go")
    for p in procs:
        assert p.wait(timeout=120) == 0
    success = 0
    for i in range(n):
        with open(os.path.join(h.dir, f"result{i}"), "rb") as fh:
            first, _, rest = fh.read().decode().partition("\n")
        if first == "0":
            success += 1
        else:
            assert first == "6" and "token_used" in rest, (first, rest)
    with open(counter, encoding="utf-8") as fh:
        writes = fh.read().count("write")
    assert success == 1 and writes == 1, f"{success} successes, {writes} writes across processes"


def test_corrupt_record_blocks_every_target_until_resolved(h: THarness) -> None:
    r = h.run("note", "update", "1", "--title", "X", "--dry-run")
    op_id = r.data()["operation_id"]
    must_write(os.path.join(h.state, "operations", op_id + ".json"), b"{not json")
    for target in ("1", "2"):
        blocked = h.run("note", "update", target, "--title", "Y", "--dry-run")
        expect_conflict(blocked, "unresolved_operation")
    show = h.run("operation", "show", op_id)
    assert (
        show.exit == 0
        and show.data()["status"] == "unknown"
        and show.data()["command"] == "unknown"
    ), show
    # An unreadable record could be any of the tool's writes, so resolving it needs
    # a scope covering every write effect the tool has.
    res = h.run(
        "operation",
        "resolve",
        op_id,
        "--outcome",
        "not_applied",
        "--evidence",
        "inspected the upstream",
    )
    assert res.code() == "E_FORBIDDEN", res
    h.grant({"effects": ["change", "deliver", "artifact", "persist"]})
    res = h.run(
        "operation",
        "resolve",
        op_id,
        "--outcome",
        "not_applied",
        "--evidence",
        "inspected the upstream",
    )
    assert res.exit == 0 and res.data()["status"] == "resolved", res
    ok = h.run("note", "update", "2", "--title", "Y", "--dry-run")
    assert ok.exit == 0, ok
    conf = h.run("note", "update", "1", "--title", "X", "--confirm", r.token())
    assert conf.exit != 0, conf


def test_prune_keeps_unresolved_records(tmp_path: Path) -> None:
    j = Journal(str(tmp_path))
    now = _dt.datetime(2026, 10, 12, tzinfo=_dt.timezone.utc)
    old = time_string(now - _dt.timedelta(days=40))

    def mk(op_id: str, status: str, lease: str | None) -> None:
        j.write(
            _record(
                op_id,
                status=status,
                created_at=old,
                expires_at=old,
                finished_at=old,
                lease_until=lease,
            )
        )

    mk("op_aaaaaaaaaaaaaaaaaaaaaaaaaa", "applied", None)
    mk("op_bbbbbbbbbbbbbbbbbbbbbbbbbb", "unknown", None)
    mk("op_cccccccccccccccccccccccccc", "dispatching", old)
    mk("op_dddddddddddddddddddddddddd", "planned", None)
    j.prune(now)
    want = {
        "op_aaaaaaaaaaaaaaaaaaaaaaaaaa": False,
        "op_bbbbbbbbbbbbbbbbbbbbbbbbbb": True,
        "op_cccccccccccccccccccccccccc": True,
        "op_dddddddddddddddddddddddddd": False,
    }
    for op_id, exists in want.items():
        assert j.read(op_id)[1] == exists, op_id


def test_effective_status() -> None:
    now = _dt.datetime(2026, 10, 12, 12, tzinfo=_dt.timezone.utc)
    past, future = (
        time_string(now - _dt.timedelta(minutes=1)),
        time_string(now + _dt.timedelta(minutes=1)),
    )
    cases = [
        ("planned", future, None, "planned"),
        ("planned", past, None, "expired"),
        ("dispatching", future, future, "dispatching"),
        ("dispatching", future, past, "unknown"),
        ("dispatching", future, None, "unknown"),
        ("applied", past, None, "applied"),
        ("resolved", past, None, "resolved"),
    ]
    for status, exp, lease, want in cases:
        r = _record(
            "op_aaaaaaaaaaaaaaaaaaaaaaaaaa", status=status, expires_at=exp, lease_until=lease
        )
        assert effective_status(r, now) == want, (status, exp, lease)


def _junction(link: str, target: str) -> None:
    out = subprocess.run(
        ["cmd", "/c", "mklink", "/J", link, target], capture_output=True, check=False
    )
    if out.returncode != 0:
        pytest.skip(f"mklink /J failed: {out.stdout!r} {out.stderr!r}")


@pytest.mark.skipif(not WINDOWS, reason="directory junctions are Windows-only")
def test_junctions_are_refused_on_windows(h: THarness, tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    _junction(str(link), str(real))
    with pytest.raises(aicli.Error) as info:
        App(name="notes", state_dir=str(link)).ensure_state_dir()
    assert info.value.code == "E_IO"
    os.makedirs(h.state, mode=0o700)
    elsewhere = os.path.join(h.dir, "elsewhere")
    os.mkdir(elsewhere)
    _junction(os.path.join(h.state, "operations"), elsewhere)
    r = h.run("note", "update", "1", "--title", "X", "--dry-run")
    assert r.exit == 1 and r.code() == "E_IO", r


def test_protection_values(tmp_path: Path) -> None:
    """Decision 30: the protection value comes from the resolved state dir."""
    from aicli.keystore import protection

    if WINDOWS:
        profile = os.path.join(os.path.expanduser("~"), ".notes-protection-probe")
        assert protection(profile) == "windows-profile-acl"
        outside = os.path.join(
            os.path.splitdrive(os.path.expanduser("~"))[0] or "C:", os.sep, "aicli-elsewhere"
        )
        if os.path.normcase(outside).startswith(os.path.normcase(os.path.expanduser("~"))):
            pytest.skip("cannot find a path outside the profile")
        assert protection(outside) == "windows-unverified"
    else:
        assert protection(str(tmp_path)) == "posix-0700"
    assert protection(None) in ("posix-0700", "windows-profile-acl", "windows-unverified")
