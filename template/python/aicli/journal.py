# Vendored from ai-native-cli-spec template/python/aicli.
# Do not edit in tool repos; run scripts/sync-spec.js.
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Sean Guo

"""The operation journal (CLI-058, CLI-060): one record per operation at
<state>/operations/<id>.json, replaced atomically (temp file + fsync + rename);
single use is the exclusive creation of <id>.consumed. A record is written before
anything is sent; if it cannot be written, nothing is sent.

The record layout is the Go library's byte for byte (json.Marshal of its record
struct: field order, HTML escaping, a trailing newline)."""

from __future__ import annotations

import datetime as _dt
import json
import os
import stat
import tempfile
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ._util import Ordered, RawNumber, decode_generic, go_json, parse_time, time_string
from .contract_gen import (
    MAX_RECORDS,
    OPERATION_STATUS_VALUES,
    RETENTION_DAYS,
    UNRESOLVED_STATUS_VALUES,
)
from .errors import Error, internal_error, io_error
from .keystore import IS_WINDOWS, exclusive_create, is_link_or_reparse, sync_dir
from .token import Claims, valid_operation_id

if TYPE_CHECKING:
    from .app import App


class CorruptRecord(Exception):
    """A record file that exists but cannot be trusted."""


@dataclass
class TargetRef:
    key: str
    kind: str = ""
    label: str = ""

    def to_json(self) -> Ordered:
        o = Ordered(key=self.key)
        if self.kind:
            o["kind"] = self.kind
        if self.label:
            o["label"] = self.label
        return o


@dataclass
class Resolution:
    outcome: str
    evidence: str
    resolved_at: str

    def to_json(self) -> Ordered:
        return Ordered(outcome=self.outcome, evidence=self.evidence, resolved_at=self.resolved_at)


@dataclass
class RecordError:
    code: str
    message: str


@dataclass
class Record:
    """An operation record as stored."""

    operation_id: str
    kind: str  # single, batch or item
    command: str
    status: str
    v: int = 2
    batch_id: str | None = None
    target: TargetRef | None = None
    effects: list[str] | None = None
    dangerous: bool = False
    scope_id: str | None = None
    created_at: str = ""
    expires_at: str = ""
    dispatched_at: str | None = None
    lease_until: str | None = None
    finished_at: str | None = None
    claims: Claims | None = None
    preview: Any = None  # generic JSON (numbers keep their text) or None
    receipt: Any = None
    error: RecordError | None = None
    resolution: Resolution | None = None
    items: list[str] | None = None
    replay_of: str | None = None
    late_receipt: Any = None
    # the confirmed write's data.result (at most MAX_STORED_RESULT bytes), so an
    # idempotent replay can return the original ref and url (CLI-059, CLI-062)
    result: Any = None

    def to_json(self) -> Ordered:
        o = Ordered()
        o["v"] = self.v
        o["operation_id"] = self.operation_id
        o["kind"] = self.kind
        o["batch_id"] = self.batch_id
        o["command"] = self.command
        o["status"] = self.status
        o["target"] = self.target.to_json() if self.target is not None else None
        o["effects"] = list(self.effects) if self.effects is not None else None
        o["dangerous"] = self.dangerous
        o["scope_id"] = self.scope_id
        o["created_at"] = self.created_at
        o["expires_at"] = self.expires_at
        o["dispatched_at"] = self.dispatched_at
        o["lease_until"] = self.lease_until
        o["finished_at"] = self.finished_at
        o["claims"] = self.claims.to_json() if self.claims is not None else None
        o["preview"] = self.preview
        o["receipt"] = self.receipt
        o["error"] = (
            Ordered(code=self.error.code, message=self.error.message)
            if self.error is not None
            else None
        )
        o["resolution"] = self.resolution.to_json() if self.resolution is not None else None
        o["items"] = list(self.items) if self.items is not None else None
        if self.replay_of:
            o["replay_of"] = self.replay_of
        if self.late_receipt is not None:
            o["late_receipt"] = self.late_receipt
        if self.result is not None:
            o["result"] = self.result
        return o

    def encode(self) -> bytes:
        return (go_json(self.to_json(), escape_html=True) + "\n").encode("utf-8")


def _is_text(v: Any) -> bool:
    """A JSON string (a number keeps its text as RawNumber, which is not one)."""
    return isinstance(v, str) and not isinstance(v, RawNumber)


def _opt_str(v: Any, name: str) -> str | None:
    if v is None:
        return None
    if not _is_text(v):
        raise ValueError(f"{name} is not a string")
    return v


def _str(v: Any, name: str) -> str:
    if v is None:
        return ""
    if not _is_text(v):
        raise ValueError(f"{name} is not a string")
    return v


def _str_list(v: Any, name: str) -> list[str] | None:
    if v is None:
        return None
    if not isinstance(v, list) or not all(_is_text(x) for x in v):
        raise ValueError(f"{name} is not a list of strings")
    return list(v)


# The keys every record carries (decision 46, A8): a record without one of them, or
# with one of the wrong type, is unreadable like a corrupt file.
REQUIRED_RECORD_TEXT = ("operation_id", "status", "command", "created_at")


def decode_record(raw: bytes) -> Record:
    """A record from its bytes; raises ValueError when the bytes do not decode
    into the record layout (Go's json.Unmarshal would fail), or when a required key
    (operation_id, status, command, created_at, effects) is missing or has the
    wrong type."""
    obj = decode_generic(raw)
    if not isinstance(obj, dict):
        raise ValueError("not an object")
    for k in REQUIRED_RECORD_TEXT:
        if not _is_text(obj.get(k)) or not obj[k]:
            raise ValueError(f"{k} is missing or not a string")
    if not isinstance(obj.get("effects"), list):
        raise ValueError("effects is missing or not a list")
    for k in ("preview", "receipt", "late_receipt"):
        if obj.get(k) is not None and not isinstance(obj[k], dict):
            raise ValueError(f"{k} is not an object")
    v = obj.get("v", 0)
    v = int(v) if isinstance(v, str) and v.isdigit() else v  # RawNumber
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("v is not an integer")
    target = obj.get("target")
    t = None
    if target is not None:
        if not isinstance(target, dict):
            raise ValueError("target is not an object")
        t = TargetRef(
            _str(target.get("key"), "target.key"),
            _str(target.get("kind"), "target.kind"),
            _str(target.get("label"), "target.label"),
        )
    claims = obj.get("claims")
    c = None
    if claims is not None:
        c = Claims.from_json(_numbers_plain(claims))
    err = obj.get("error")
    re_ = None
    if err is not None:
        if not isinstance(err, dict):
            raise ValueError("error is not an object")
        re_ = RecordError(
            _str(err.get("code"), "error.code"), _str(err.get("message"), "error.message")
        )
    res = obj.get("resolution")
    rs = None
    if res is not None:
        if not isinstance(res, dict):
            raise ValueError("resolution is not an object")
        rs = Resolution(
            _str(res.get("outcome"), "resolution.outcome"),
            _str(res.get("evidence"), "resolution.evidence"),
            _str(res.get("resolved_at"), "resolution.resolved_at"),
        )
    dangerous = obj.get("dangerous", False)
    if dangerous is None:
        dangerous = False
    if not isinstance(dangerous, bool):
        raise ValueError("dangerous is not a boolean")
    return Record(
        v=v,
        operation_id=_str(obj.get("operation_id"), "operation_id"),
        kind=_str(obj.get("kind"), "kind"),
        batch_id=_opt_str(obj.get("batch_id"), "batch_id"),
        command=_str(obj.get("command"), "command"),
        status=_str(obj.get("status"), "status"),
        target=t,
        effects=_str_list(obj.get("effects"), "effects"),
        dangerous=dangerous,
        scope_id=_opt_str(obj.get("scope_id"), "scope_id"),
        created_at=_str(obj.get("created_at"), "created_at"),
        expires_at=_str(obj.get("expires_at"), "expires_at"),
        dispatched_at=_opt_str(obj.get("dispatched_at"), "dispatched_at"),
        lease_until=_opt_str(obj.get("lease_until"), "lease_until"),
        finished_at=_opt_str(obj.get("finished_at"), "finished_at"),
        claims=c,
        preview=obj.get("preview"),
        receipt=obj.get("receipt"),
        error=re_,
        resolution=rs,
        items=_str_list(obj.get("items"), "items"),
        replay_of=_opt_str(obj.get("replay_of"), "replay_of") or None,
        late_receipt=obj.get("late_receipt"),
        result=obj.get("result"),
    )


def _numbers_plain(v: Any) -> Any:
    """Claims hold one integer (v); turn its kept number text back into an int."""
    if isinstance(v, dict) and isinstance(v.get("v"), str):
        out = dict(v)
        try:
            out["v"] = int(str.__str__(v["v"]))
        except ValueError:
            out["v"] = 0.5  # not an integer: rejected by Claims.from_json
        return out
    return v


def effective_status(r: Record, now: _dt.datetime) -> str:
    """The stored status with expiry and lease applied (BLUEPRINT 5.3)."""
    if r.status == "planned":
        exp = parse_time(r.expires_at)
        if exp is None or now > exp:
            return "expired"
    elif r.status == "dispatching":
        if r.lease_until is None:
            return "unknown"
        lease = parse_time(r.lease_until)
        if lease is None or now > lease:
            return "unknown"
    return r.status


def unresolved(status: str) -> bool:
    return status in UNRESOLVED_STATUS_VALUES


def batch_target_keys(r: Record) -> list[str]:
    """The target keys a batch record's preview lists."""
    pv = r.preview if isinstance(r.preview, dict) else {}
    targets = pv.get("targets")
    if not isinstance(targets, list):
        return []
    return [t["key"] for t in targets if isinstance(t, dict) and isinstance(t.get("key"), str)]


def rename_retry(src: str, dst: str) -> None:
    """os.replace, retried briefly on Windows where a concurrent reader can hold
    the target open for a moment."""
    for attempt in range(50):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if not IS_WINDOWS or attempt == 49:
                raise
            time.sleep((2 + attempt) / 1000)


def read_file_retry(path: str) -> bytes:
    """A file's bytes, retried briefly on Windows while a writer replaces it."""
    for attempt in range(50):
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except FileNotFoundError:
            raise
        except PermissionError:
            if not IS_WINDOWS or attempt == 49:
                raise
            time.sleep((2 + attempt) / 1000)
    raise OSError("unreachable")


def write_file_atomic(d: str, name: str, data: bytes) -> None:
    """Replace d/name with data via a synced temporary file."""
    fd, tmp = tempfile.mkstemp(prefix=name + ".tmp-", dir=d)
    done = False
    try:
        try:
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                view = view[n:]
            os.fsync(fd)
        finally:
            os.close(fd)
        if not IS_WINDOWS:
            os.chmod(tmp, 0o600)
        rename_retry(tmp, os.path.join(d, name))
        done = True
        sync_dir(d)
    finally:
        if not done:
            try:
                os.remove(tmp)
            except OSError:
                pass


@dataclass
class Listed:
    """One journal entry: a record, or a corrupt file."""

    id: str
    rec: Record | None
    corrupt: bool
    mod_time: float


class Journal:
    """The operations directory of one state dir."""

    def __init__(self, d: str) -> None:
        self.dir = d

    def mod_time(self, op_id: str) -> _dt.datetime:
        """The last write of a record file (the epoch when unknown)."""
        try:
            return _dt.datetime.fromtimestamp(
                os.stat(self.record_path(op_id)).st_mtime, _dt.timezone.utc
            )
        except OSError:
            return _dt.datetime.fromtimestamp(0, _dt.timezone.utc)

    def record_path(self, op_id: str) -> str:
        return os.path.join(self.dir, op_id + ".json")

    def write(self, r: Record) -> None:
        """Store a record atomically: temp file, fsync, rename over the old one."""
        if not valid_operation_id(r.operation_id):
            raise internal_error("Refusing to write a record with an invalid operation id.")
        try:
            data = r.encode()
        except (TypeError, ValueError):
            raise internal_error("The operation record cannot be encoded.") from None
        try:
            write_file_atomic(self.dir, r.operation_id + ".json", data)
        except OSError as exc:
            raise io_error(
                "The operation record cannot be written; nothing was sent.",
                f"Check that {self.dir} is writable.",
                exc,
            ) from None

    def read(self, op_id: str) -> tuple[Record | None, bool]:
        """A record. exists is False when there is none; raises CorruptRecord when
        the file exists but cannot be trusted and OSError when it cannot be read."""
        if not valid_operation_id(op_id):
            return None, False
        try:
            b = read_file_retry(self.record_path(op_id))
        except FileNotFoundError:
            return None, False
        try:
            r = decode_record(b)
        except (ValueError, UnicodeDecodeError, RecursionError) as exc:
            raise CorruptRecord(op_id) from exc
        if (
            r.v != 2
            or r.operation_id != op_id
            or r.kind not in ("single", "batch", "item")
            or r.status not in OPERATION_STATUS_VALUES
            or r.status == "expired"
        ):
            raise CorruptRecord(op_id)
        if r.kind != "batch" and r.target is None:
            raise CorruptRecord(op_id)
        return r, True

    def consume(self, op_id: str, at: _dt.datetime) -> bool:
        """Create the single-use marker for op_id. False when it already exists (the
        token was used); the exclusive create is atomic across processes."""
        path = os.path.join(self.dir, op_id + ".consumed")
        body = json.dumps(
            {"operation_id": op_id, "consumed_at": time_string(at)}, separators=(",", ":")
        )
        try:
            created = exclusive_create(path, (body + "\n").encode("utf-8"))
        except OSError as exc:
            raise io_error(
                "The single-use marker cannot be written; nothing was sent.",
                f"Check that {self.dir} is writable.",
                exc,
            ) from None
        if created:
            sync_dir(self.dir)
        return created

    def is_consumed(self, op_id: str) -> bool:
        """Whether the single-use marker exists."""
        try:
            os.lstat(os.path.join(self.dir, op_id + ".consumed"))
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise io_error(
                "The single-use marker cannot be read; nothing was sent.", f"Check {self.dir}.", exc
            ) from None

    def lock(self, op_id: str):  # noqa: ANN201 - returns the unlock function
        """Serialise read-modify-write of one record between the dispatching process
        and operation resolve. Stale locks (older than 30 s) are broken."""
        path = os.path.join(self.dir, op_id + ".lock")
        try:
            got = acquire_lock(path, JOURNAL_LOCK_WAIT)
        except OSError as exc:
            raise io_error(
                "The operation record cannot be locked.",
                f"Check that {self.dir} is writable.",
                exc,
            ) from None
        if not got:
            raise io_error(
                "The operation record is locked by another process.",
                "Run the command again in a moment.",
            )
        return lambda: _remove_quiet(path)

    @property
    def state_dir(self) -> str:
        """The state directory the journal lives in."""
        return os.path.dirname(self.dir)

    def list(self) -> list[Listed]:
        """Every record in the journal. A missing journal is empty."""
        try:
            names = os.listdir(self.dir)
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise io_error(
                "The operation journal cannot be listed; nothing was sent.",
                f"Check {self.dir}.",
                exc,
            ) from None
        out = []
        for name in sorted(names):
            if not name.endswith(".json"):
                continue
            op_id = name[: -len(".json")]
            if not valid_operation_id(op_id):
                continue
            try:
                mod = os.stat(os.path.join(self.dir, name)).st_mtime
            except OSError:
                mod = 0.0
            try:
                r, exists = self.read(op_id)
            except CorruptRecord:
                out.append(Listed(op_id, None, True, mod))
                continue
            except OSError:
                out.append(Listed(op_id, None, True, mod))
                continue
            if not exists:
                continue
            out.append(Listed(op_id, r, False, mod))
        return out

    def blocking(self, targets: list[str], exclude: set[str], now: _dt.datetime) -> str:
        """An operation whose outcome is unresolved on any of targets, or a corrupt
        record (whose target is unknown, so it blocks every target). A batch that is
        still dispatching blocks every target of its preview: its items wait their
        turn as planned records, and the batch's record is what blocks them
        (CLI-057 step 14)."""
        want = set(targets)
        for entry in sorted(self.list(), key=lambda e: e.id):
            if entry.id in exclude:
                continue
            if entry.corrupt:
                return entry.id
            r = entry.rec
            assert r is not None
            if r.resolution is not None:
                continue
            if r.kind == "batch":
                if effective_status(r, now) == "dispatching" and any(
                    k in want for k in batch_target_keys(r)
                ):
                    return entry.id
                continue
            if r.target is None:
                continue
            if unresolved(effective_status(r, now)) and r.target.key in want:
                return entry.id
        return ""

    def find_idempotent(self, idem: str, exclude: set[str]) -> list[Record]:
        """Every operation, of any command, that was confirmed with the idempotency
        key idem (CLI-062): single writes and batches, not those still planned; the
        latest first."""
        found = []
        for entry in self.list():
            r = entry.rec
            if r is None or r.operation_id in exclude or r.claims is None or r.claims.idem != idem:
                continue
            if r.kind == "item" or r.status == "planned":
                continue
            found.append(r)
        found.sort(key=lambda r: (r.created_at, r.operation_id), reverse=True)
        return found

    def prune(self, now: _dt.datetime) -> None:
        """Remove settled records older than the retention window and keep at most
        MAX_RECORDS, oldest settled first. Unresolved and corrupt records stay. A
        batch's items go with their batch record, by the batch's finish time, and
        stay while the batch or any item is unresolved. The scope budget
        (scope-usage.json) is never touched."""
        try:
            entries = self.list()
        except Error:
            return
        horizon = now - _dt.timedelta(days=RETENTION_DAYS)
        batches = {e.id for e in entries if e.rec is not None and e.rec.kind == "batch"}
        groups: dict[str, list[Record]] = {}
        for e in entries:
            r = e.rec
            if r is not None and r.kind == "item" and r.batch_id in batches:
                groups.setdefault(r.batch_id or "", []).append(r)

        def settled(r: Record) -> bool:
            st = effective_status(r, now)
            return not unresolved(st) and st != "planned"

        candidates: list[tuple[str, list[str]]] = []  # (created_at, ids), oldest first later
        kept = 0
        for e in entries:
            r = e.rec
            if e.corrupt or r is None:
                kept += 1
                continue
            if r.kind == "item" and r.batch_id in batches:
                continue  # counted and removed with its batch
            members = [r, *groups.get(r.operation_id, [])] if r.kind == "batch" else [r]
            ids = [m.operation_id for m in members]
            if not all(settled(m) for m in members):
                kept += len(ids)
                continue
            ref = r.finished_at if r.finished_at is not None else r.created_at
            t = parse_time(ref)
            if t is not None and t < horizon:
                for op_id in ids:
                    self.remove(op_id)
                continue
            candidates.append((r.created_at, ids))
        total = kept + sum(len(ids) for _, ids in candidates)
        if total <= MAX_RECORDS:
            return
        candidates.sort(key=lambda x: x[0])
        for _, ids in candidates:
            if total <= MAX_RECORDS:
                break
            for op_id in ids:
                self.remove(op_id)
            total -= len(ids)

    def remove(self, op_id: str) -> None:
        _remove_quiet(self.record_path(op_id))
        _remove_quiet(os.path.join(self.dir, op_id + ".consumed"))


def _remove_quiet(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------- locks

LOCK_STALE_SECONDS = 30.0  # a lock file older than this is broken
JOURNAL_LOCK_WAIT = 3.0  # a record's read-modify-write lock
TARGET_LOCK_WAIT = 10.0  # the confirm checks' per-target lock (CLI-057 step 9)


def acquire_lock(path: str, wait: float, payload: bytes = b"") -> bool:
    """The journal's lock primitive: create path exclusively, holding payload.
    While another holder has it, retry for up to wait seconds; a lock file older
    than 30 s is stale and is broken. False when the wait ran out; OSError when the
    lock cannot be created at all (on Windows a file being deleted refuses access
    for a moment, so that is retried until the wait runs out)."""
    deadline = time.monotonic() + wait
    while True:
        denied: OSError | None = None
        try:
            if exclusive_create(path, payload):
                return True
        except PermissionError as exc:
            if not IS_WINDOWS:
                raise
            denied = exc
        else:
            try:
                if time.time() - os.stat(path).st_mtime > LOCK_STALE_SECONDS:
                    _remove_quiet(path)
                    continue
            except OSError:
                pass
        if time.monotonic() > deadline:
            if denied is not None:
                raise denied
            return False
        time.sleep(0.01)


def release_lock(path: str, owner: bytes = b"") -> None:
    """Remove a lock file, unless it now names another owner (ours was broken as
    stale and taken over)."""
    if owner:
        try:
            with open(path, "rb") as fh:
                if fh.read(len(owner) + 1) != owner:
                    return
        except OSError:
            return
    _remove_quiet(path)


def lock_holder(path: str) -> str:
    """The operation id a lock file names, or "" when it cannot be read."""
    try:
        with open(path, "rb") as fh:
            op_id = fh.read(256).decode("utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""
    return op_id if valid_operation_id(op_id) else ""


def ensure_private_dir(d: str) -> None:
    """d (0700) inside the state directory, created when missing; a link, a
    reparse point or a non-directory is refused (SEC-030). Raises E_IO."""
    try:
        st = os.lstat(d)
    except FileNotFoundError:
        try:
            os.mkdir(d, 0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise io_error(
                "A directory of the state store cannot be created; nothing was sent.",
                f"Check that {os.path.dirname(d)} is writable.",
                exc,
            ) from None
        st = None
    try:
        st = st or os.lstat(d)
    except OSError as exc:
        raise io_error(
            "A directory of the state store cannot be read; nothing was sent.", f"Check {d}.", exc
        ) from None
    if is_link_or_reparse(st) or not stat.S_ISDIR(st.st_mode):
        raise io_error(
            "A path in the state store is not a directory; the tool refuses writes.",
            f"Ask the user to inspect {d} and move it aside; the tool recreates it.",
        )


# ---------------------------------------------------------------- scope budget

SCOPE_USAGE = "scope-usage.json"


def read_scope_usage(state_dir: str) -> dict[str, int]:
    """The operations dispatched per scope_id (<state>/scope-usage.json,
    {"v":1,"scopes":{id: count}}); a missing file counts nothing. A file that
    cannot be read or trusted refuses the write (E_IO, fail closed)."""
    path = os.path.join(state_dir, SCOPE_USAGE)

    def broken(exc: BaseException | None = None) -> Error:
        return io_error(
            "The scope's operation count cannot be read; nothing was sent.",
            f"Ask the user to check {path}; deleting it resets the operation count of every scope.",
            exc,
        )

    try:
        raw = read_file_retry(path)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise broken(exc) from None
    if len(raw) > 1 << 20:
        raise broken()
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise broken(exc) from None
    ok = isinstance(obj, dict) and type(obj.get("v")) is int and obj["v"] == 1
    scopes = obj.get("scopes") if ok else None
    if not isinstance(scopes, dict) or set(obj) - {"v", "scopes"}:
        raise broken()
    out: dict[str, int] = {}
    for k, n in scopes.items():
        if type(n) is not int or n < 0:
            raise broken()
        out[k] = n
    return out


def write_scope_usage(state_dir: str, usage: dict[str, int]) -> None:
    """Replace scope-usage.json atomically. Raises E_IO."""
    body = Ordered(v=1, scopes={k: usage[k] for k in sorted(usage)})
    try:
        write_file_atomic(state_dir, SCOPE_USAGE, (go_json(body) + "\n").encode("utf-8"))
    except OSError as exc:
        raise io_error(
            "The scope's operation count cannot be written; nothing was sent.",
            f"Check that {state_dir} is writable.",
            exc,
        ) from None


def lock_scope_usage(state_dir: str, owner: str):  # noqa: ANN201 - returns the unlock function
    """The budget lock, <state>/locks/scope-usage.lock: every change of
    scope-usage.json happens under it (A4). Raises E_IO."""
    d = os.path.join(state_dir, "locks")
    ensure_private_dir(d)
    path = os.path.join(d, "scope-usage.lock")
    try:
        got = acquire_lock(path, TARGET_LOCK_WAIT, owner.encode("utf-8"))
    except OSError as exc:
        raise io_error(
            "The scope's operation count cannot be locked; nothing was sent.",
            f"Check that {d} is writable.",
            exc,
        ) from None
    if not got:
        raise io_error(
            "The scope's operation count is locked by another process; nothing was sent.",
            "Run the same command again in a few seconds; the confirm token stays valid until "
            "it expires.",
        )
    return lambda: release_lock(path, owner.encode("utf-8"))


class JournalMixin:
    def open_journal(self: App, state_dir: str, create: bool) -> Journal:
        """The journal of state_dir. With create, the operations directory is made
        (0700). A path that is a link or not a directory is E_IO (fail closed)."""
        d = os.path.join(state_dir, "operations")
        try:
            st = os.lstat(d)
        except FileNotFoundError:
            if not create:
                return Journal(d)
            try:
                os.mkdir(d, 0o700)
            except FileExistsError:
                pass
            except OSError as exc:
                raise io_error(
                    "The operation journal cannot be created; nothing was sent.",
                    f"Check that {state_dir} is writable.",
                    exc,
                ) from None
            try:
                st = os.lstat(d)
            except OSError as exc:
                raise io_error(
                    "The operation journal cannot be read; nothing was sent.", f"Check {d}.", exc
                ) from None
        except OSError as exc:
            raise io_error(
                "The operation journal cannot be read; nothing was sent.", f"Check {d}.", exc
            ) from None
        if is_link_or_reparse(st) or not stat.S_ISDIR(st.st_mode):
            raise io_error(
                "The operation journal path is not a directory; the tool refuses writes.",
                f"Ask the user to inspect {d} and move it aside; the tool recreates the journal.",
            )
        return Journal(d)
