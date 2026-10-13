# Vendored from ai-native-cli-spec template/python/aicli.
# Do not edit in tool repos; run scripts/sync-spec.js.
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sean Guo

"""The shared write gate (CLI-050 .. CLI-063, SEC-020). Every command with an
effect other than observe reports through App.write or App.write_batch:

- without --confirm the gate plans: it checks --dangerous, the scope and earlier
  unknown outcomes, calls preview, records the operation and returns the preview
  with a confirm token (as data with --dry-run, as E_CONFIRMATION_REQUIRED
  without it). Nothing is sent.
- with --confirm it re-checks everything in a fixed order, consumes the token
  exactly once, records the operation as dispatching, calls apply and derives ok
  and the error code from the receipt.

No output mode, flag or environment variable turns the gate off.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ._tree import Node
from ._util import (
    canonical_json,
    claim_string,
    claim_strings,
    go_json,
    json_equal,
    pick,
    raw_bytes,
    sha256_hex,
    snake,
    time_string,
    to_generic,
    truncate_runes,
)
from .binding import Binding
from .contract_gen import DEFAULT_LEASE_SECONDS, EXCLUDED_FROM_BINDING
from .envelope import Notice, Result_, prefix_paths
from .errors import (
    Error,
    as_error,
    conflict_error,
    find_error,
    internal_error,
    io_error,
    new_error,
    usage,
)
from .journal import (
    TARGET_LOCK_WAIT,
    CorruptRecord,
    Journal,
    Record,
    RecordError,
    TargetRef,
    acquire_lock,
    effective_status,
    ensure_private_dir,
    lock_holder,
    lock_scope_usage,
    read_scope_usage,
    release_lock,
    unresolved,
    write_scope_usage,
)
from .keystore import KEY_SIZE
from .receipt import (
    Outcome,
    Result,
    auto_verify,
    derive_receipt,
    normalize_outcome,
    record_status,
    sanitize_outcome,
)
from .scope import ScopeNeed, ScopeState
from .token import TOKEN_LIFETIME, Claims, mint_token, new_operation_id, parse_token, verify_mac

if TYPE_CHECKING:
    from .app import App, Identity
    from .binding import Binding
    from .envelope import RunState
    from .registry import Meta


@dataclass
class Target:
    """What a write acts on. key is the canonical key scopes match (for example
    "acme/web!5"); kind (snake_case) and label are for people."""

    key: str
    kind: str = ""
    label: str = ""


@dataclass
class FieldChange:
    """One planned change in a preview (preview.changes[]). None values are left
    out."""

    field: str
    before: Any = None
    after: Any = None
    add: Any = None
    remove: Any = None
    note: str = ""


@dataclass
class Block:
    """A reason a previewed write cannot be applied (preview.blocked_by[])."""

    kind: str
    message: str


@dataclass
class Preview:
    """What preview returns: the planned change, read-only. base_version is the
    target's current version (updated_at, etag, revision) when the upstream has
    one; the gate re-reads it at confirm and refuses on drift. can_apply must be
    true for a token to be issued; blocked_by says why not. destinations
    (deliver) and outputs (artifact, persist) are checked against the scope.
    untrusted lists paths relative to the preview object that carry external
    content (for example "changes[].before")."""

    action: str = ""
    changes: list[FieldChange] = field(default_factory=list)
    base_version: str = ""
    base_source: str = ""
    can_apply: bool = False
    blocked_by: list[Block] = field(default_factory=list)
    destinations: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    untrusted: list[str] = field(default_factory=list)


@dataclass
class Op:
    """Handed to apply: the operation id, the idempotency key to send upstream
    and the timeout in seconds. apply MUST honour timeout (pass it to every
    request it makes): the gate cannot cancel apply, and the operation's lease
    ends 5 s after the timeout, when `operation resolve` may record an outcome. A
    write resolved while apply was still running keeps that resolution and is
    reported as E_OUTCOME_UNKNOWN (CLI-058)."""

    id: str
    idempotency_key: str = ""
    timeout: float = 0.0


@dataclass
class WriteSpec:
    """One write. preview is read-only and is called at dry-run, at the refusal
    and again at confirm. apply performs the change; what it raises decides the
    outcome: not_dispatched(...) (nothing was sent), rejected(...) (the upstream
    definitively refused; nothing changed), anything else an unknown outcome
    (E_OUTCOME_UNKNOWN). timeout is the lease in seconds (default 120). read_back
    is the read command, without the tool name, that shows whether the change
    landed (for example "note get 1"); an unknown outcome's next_action names it.
    Build it from the registry path and validated inputs only, never from upstream
    text (SEC-002)."""

    target: Target
    preview: Callable[[], Preview]
    apply: Callable[[Op], Result | None]
    timeout: float = 0.0
    read_back: str = ""


@dataclass
class BatchItem:
    """One target of a batch write."""

    target: Target
    preview: Callable[[], Preview]
    apply: Callable[[Op], Result | None]


@dataclass
class BatchSpec:
    """A batch write (CLI-063). continue_on_error overrides the
    --continue-on-error flag (default true, false for dangerous commands)."""

    items: list[BatchItem] = field(default_factory=list)
    continue_on_error: bool | None = None
    timeout: float = 0.0


class NotDispatchedError(Exception):
    """An apply error raised before anything was sent."""

    def __init__(self, err: BaseException) -> None:
        super().__init__("not dispatched: " + str(err))
        self.err = err


class RejectedError(Exception):
    """An apply error where the upstream definitively refused and nothing changed."""

    def __init__(self, err: BaseException) -> None:
        super().__init__("rejected: " + str(err))
        self.err = err


def not_dispatched(err: BaseException | None = None) -> NotDispatchedError:
    """Mark an apply error raised before anything was sent: the operation is
    not_started, nothing changed, and the error's code is reported."""
    return NotDispatchedError(err if err is not None else Exception("not dispatched"))


def rejected(err: BaseException | None = None) -> RejectedError:
    """Mark an apply error where the upstream definitively refused the request and
    nothing changed (for example HTTP 400, 403, 404, 409, 422). The error's code
    is reported with dispatch_state dispatched."""
    return RejectedError(err if err is not None else Exception("rejected"))


DEFAULT_TIMEOUT = float(DEFAULT_LEASE_SECONDS)
# The lease outlasts apply's timeout by this much, so operation resolve cannot start
# while apply may still be running (CLI-058, A6).
LEASE_GRACE = 5.0


def untrusted_paths(v: Any) -> list[str]:
    """Result.untrusted as a list of strings (anything else is dropped)."""
    if isinstance(v, (list, tuple)):
        return [p for p in v if isinstance(p, str)]
    return []


def origin_of(j: Journal, r: Record) -> tuple[str, Record]:
    """Follow replay_of from an applied record to the original operation (a replay
    of a replay answers with the original, CLI-062). Returns the original's id and
    the deepest record that can still be read: the original, or the replay whose
    copies stand in for it once the original was pruned."""
    op_id = r.operation_id
    for _ in range(8):
        if not r.replay_of:
            break
        op_id = r.replay_of
        try:
            o, exists = j.read(op_id)
        except (CorruptRecord, OSError, Error):
            break
        if not exists or o is None:
            break
        r = o
    return op_id, r


GATE_CODES = (
    "E_OUTCOME_UNKNOWN",
    "E_PARTIAL",
    "E_VERIFY_FAILED",
    "E_VERIFY_INCOMPLETE",
    "E_CONFIRMATION_REQUIRED",
)


def check_target_key(key: str) -> Error | None:
    """Refuse target keys that could slip past scope prefix matching: dot-dot path
    segments and control characters (SEC-012)."""
    bad = any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in key)
    for seg in key.replace("\\", "/").split("/"):
        if seg in ("..", "."):
            bad = True
    if not bad:
        return None
    from .errors import validation

    e = validation(
        "target",
        "a canonical identifier without . or .. segments",
        "The write target is not a canonical identifier; nothing was sent.",
    )
    e.details["next_action"] = "Give the resource by its id or ref, without relative path segments."
    return e


def target_object(t: Target | TargetRef) -> dict[str, Any]:
    o: dict[str, Any] = {"key": t.key}
    if t.kind:
        o["kind"] = t.kind
    if t.label:
        o["label"] = t.label
    return o


def change_objects(changes: list[FieldChange]) -> list[Any]:
    out = []
    for c in changes:
        o: dict[str, Any] = {"field": c.field}
        for k, v in (
            ("before", c.before),
            ("after", c.after),
            ("add", c.add),
            ("remove", c.remove),
        ):
            try:
                gv = to_generic(v)
            except (TypeError, ValueError):
                continue
            if gv is not None:
                o[k] = gv
        if c.note:
            o["note"] = c.note
        out.append(o)
    return out


def block_objects(blocks: list[Block]) -> list[Any]:
    return [{"kind": b.kind, "message": b.message} for b in blocks]


def can_apply_of(pv: Preview) -> tuple[bool, list[Block]]:
    blocks = list(pv.blocked_by)
    can = pv.can_apply and not blocks
    if not can and not blocks:
        blocks = [Block("preview", "The preview does not allow this change.")]
    return can, blocks


def digest_list(values: list[str]) -> str:
    """An ordered list of strings bound as one claim value."""
    return "sha256:" + sha256_hex(canonical_json(claim_strings(values)))


def strings_at(m: Any, key: str) -> list[str]:
    if not isinstance(m, dict):
        return []
    v = m.get(key)
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def journal_notice() -> Notice:
    return Notice(
        type="journal_write_failed",
        severity="warning",
        message="The operation record could not be updated; `operation show` may report this "
        "operation as unknown.",
    )


def cause_of(err: BaseException | None) -> Error | None:
    e = find_error(err)
    if e is not None and e.code not in GATE_CODES:
        return e.copy()
    return None


@dataclass
class WriteOutcome:
    """A classified apply result."""

    receipt: dict[str, Any] = field(default_factory=dict)
    ok: bool = False
    code: str = ""
    cause: Error | None = None
    dispatch: str = "dispatched"
    status: str = ""
    outcome: Outcome = field(default_factory=Outcome)
    observed: dict[str, Any] = field(default_factory=dict)
    result_data: Any = field(default_factory=dict)
    # Result.data could not be encoded as JSON: data.result is {} (A2)
    unencodable: bool = False
    # the record was resolved by hand while apply ran: the outcome is unknown (A6)
    resolved_meanwhile: bool = False


@dataclass
class ItemPlan:
    item: BatchItem
    id: str = ""
    pv: Preview = field(default_factory=Preview)
    can: bool = False
    blocks: list[Block] = field(default_factory=list)
    pobj: dict[str, Any] = field(default_factory=dict)


# The confirmed write's data.result kept in its record (at most this many bytes of
# JSON), so an idempotent replay can return the original ref and url (CLI-059, CLI-062).
MAX_STORED_RESULT = 4096


@dataclass
class Issued:
    """A planned write: its preview, operation id and token (None, with a None
    expiry, when the write cannot apply); or, when replay is set, the answer to a
    repeated idempotent request found before Preview (P4)."""

    pobj: dict[str, Any]
    id: str
    token: str | None = None
    expires: str | None = None
    can_apply: bool = False
    untrusted: list[str] = field(default_factory=list)
    replay: dict[str, Any] | None = None


def mint(
    pobj: dict[str, Any],
    op_id: str,
    key: bytes,
    cl: Claims,
    can_apply: bool,
    exp: _dt.datetime,
    untrusted: list[str],
) -> Issued:
    """Issue the token for a plan that can apply."""
    out = Issued(pobj=pobj, id=op_id, can_apply=can_apply, untrusted=list(untrusted))
    if can_apply:
        try:
            out.token = mint_token(key, cl)
        except (TypeError, ValueError):
            raise internal_error(
                "The confirm token could not be issued; nothing was changed."
            ) from None
        out.expires = time_string(exp)
    return out


def _file_shown(v: Any) -> bool:
    """Whether a preview input shows file content ({sha256, bytes})."""
    return isinstance(v, dict) and "sha256" in v and "bytes" in v


class GateRun:
    """One pass through the gate."""

    def __init__(self, a: App, st: RunState, node: Node) -> None:
        self.a = a
        self.st = st
        self.node = node
        self.m: Meta
        self.path = node.path
        self.dry_run = False
        self.confirm = ""
        self.confirm_set = False
        self.dangerous_flag = False
        self.idem = ""
        # the operation whose dispatching record is written: from then on nothing
        # reports dispatch_state not_started (CLI-057, A2)
        self.dispatched_id = ""

    # ---------------------------------------------------------------- reporting

    def fail(self, e: Error) -> Error:
        """Record the gate's error as the run's outcome (the first one wins)."""
        if not self.st.gate.set:
            self.st.gate = Result_(set=True, err=e)
        return e

    def succeed(self, data: dict[str, Any], untrusted: list[str]) -> None:
        try:
            gen = to_generic(data)
        except (TypeError, ValueError, RecursionError):
            if self.dispatched_id:
                raise self.fail(self.after_dispatch_error(self.dispatched_id)) from None
            raise self.fail(internal_error("The write result cannot be encoded as JSON.")) from None
        if not self.st.gate.set:
            self.st.gate = Result_(set=True, data=gen, raw=data, untrusted=untrusted, library=True)

    def lookup(self, op_id: str) -> str:
        return f"{self.tool()} operation show --id {op_id}"

    def after_dispatch_error(self, op_id: str, exc: BaseException | None = None) -> Error:
        """A failure the library cannot classify once the dispatching record is
        written (CLI-057, A2): the outcome is unknown, never "nothing was sent". A
        crash adds its text as details.cause."""
        lookup = self.lookup(op_id)
        e = new_error(
            "E_OUTCOME_UNKNOWN",
            "The write may have been applied, but its result could not be reported; nothing "
            "will be retried.",
            {
                "operation_id": op_id,
                "lookup": lookup,
                "dispatch_state": "unknown",
                "next_action": (
                    f"Run `{lookup}` and re-read the target before anything else; do not resend."
                ),
            },
        )
        if exc is not None:
            e.details["cause"] = truncate_runes(f"{type(exc).__name__}: {exc}", 300)
            e.untrusted = ["cause"]
        return e

    def resolved_meanwhile_error(self, op_id: str) -> Error:
        """The record was resolved by hand while apply ran (A6): that resolution
        stands, and the outcome of this run is unknown."""
        lookup = self.lookup(op_id)
        return new_error(
            "E_OUTCOME_UNKNOWN",
            "The operation was resolved by hand while it was still running; its outcome is "
            "unknown.",
            {
                "operation_id": op_id,
                "lookup": lookup,
                "dispatch_state": "unknown",
                "next_action": (
                    f"Run `{lookup}` and re-read the target before anything else; do not resend."
                ),
            },
        )

    def unencodable_notice(self, op_id: str) -> None:
        """Result.data could not be encoded (A2): the receipt stands, data.result
        is {}, and the run says so (a notice, and a defect for conformance)."""
        self.a.notice(
            Notice(
                type="result_unencodable",
                severity="warning",
                message="The change was dispatched, but the tool's result could not be encoded, "
                f"so data.result is empty; run `{self.lookup(op_id)}` for the receipt.",
            )
        )
        self.a.defect(
            "Apply returned a Result.data that cannot be encoded as JSON (UUID, Decimal, Path, set "
            "and the like); the gate replaced data.result with {}."
        )

    def tool(self) -> str:
        return self.a.tool_name()

    def dangerous_required(self) -> Error:
        return new_error(
            "E_CONFIRMATION_REQUIRED",
            "This command is dangerous: --dangerous is required in both the preview and the "
            "confirm step. Nothing was changed.",
            {
                "problem": "dangerous_flag_required",
                "dispatch_state": "not_started",
                "next_action": (
                    "Run the same command again with --dangerous added (keep --dry-run or "
                    "--confirm as they were)."
                ),
            },
        )

    def dangerous_refusal(self, spec: WriteSpec) -> Error:
        """dangerous_flag_required at plan time with the read-only preview attached,
        so the plan can be shown to the user without another call. It carries no
        token: a token is issued only to a preview run with --dangerous."""
        e = self.dangerous_required()
        try:
            pv = self.call_preview(spec.preview)
        except Error:
            return e
        e = self.attach_preview(e, spec, pv)
        if "preview" in e.details:
            e.details["next_action"] = (
                "details.preview shows the plan; run the same command again with --dangerous added "
                "to get its confirm token (keep --dry-run if you used it)."
            )
        return e

    def dangerous_arg(self) -> str:
        """ " --dangerous" for a dangerous command, for next_action texts."""
        return " --dangerous" if self.m.is_dangerous() else ""

    def token_mismatch(self, parameter: str, message: str) -> Error:
        nxt = (
            "Run the same command with --dry-run to get a token for these inputs, or confirm with "
            "the inputs the token was issued for."
        )
        if parameter == "--confirm":
            nxt = (
                "Pass the confirm_token from the latest preview unchanged, or run the command "
                "again with --dry-run for a new token."
            )
        e = conflict_error("token_mismatch", message + " Nothing was sent.", nxt)
        e.details["parameter"] = parameter
        return e

    def token_used(self, op_id: str) -> Error:
        e = conflict_error(
            "token_used",
            "This confirm token was already used; nothing was sent again.",
            f"Check the result with `{self.tool()} operation show --id {op_id}`; run --dry-run "
            "again only if another change is needed.",
        )
        e.details["operation_id"] = op_id
        return e

    def unresolved_conflict(self, op_id: str) -> Error:
        t = self.tool()
        e = conflict_error(
            "unresolved_operation",
            "An earlier operation on this target has an unknown outcome; nothing was sent.",
            f"Run `{t} operation show --id {op_id}` and check the upstream; then record the "
            "outcome "
            f"with `{t} operation resolve --id {op_id} --outcome applied|not_applied|partial "
            "--evidence <text>`.",
        )
        e.details["active_operation"] = op_id
        return e

    def lock_held(self, target: str, holder: str) -> Error:
        """lock_held at confirm (CLI-057 step 9): another confirm on the target is
        between its checks and its dispatching record."""
        e = conflict_error(
            "lock_held",
            "Another confirm on the same target is still running; nothing was sent.",
            "Run the same command again in a few seconds; the confirm token stays valid until "
            "it expires.",
        )
        e.details["target"] = target
        if holder:
            e.details["active_operation"] = holder
        return e

    def lock_targets(self, state: str, keys: list[str], op_id: str) -> Callable[[], None]:
        """Take the per-target locks of a confirm (CLI-057 step 9, A3):
        <state>/locks/<first 32 hex of sha256(target key)>.lock holding the
        operation id, every target of a batch in sorted order, with the journal
        lock's primitive and stale-break rule; still held by another confirm after
        10 s in all, lock_held. Returns the release function; the caller releases
        once the dispatching record is written."""
        d = os.path.join(state, "locks")
        ensure_private_dir(d)
        owner = op_id.encode("utf-8")
        held: list[str] = []

        def release() -> None:
            for p in reversed(held):
                release_lock(p, owner)
            held.clear()

        paths: dict[str, str] = {}
        for key in keys:
            paths[os.path.join(d, sha256_hex(raw_bytes(key))[:32] + ".lock")] = key
        deadline = time.monotonic() + TARGET_LOCK_WAIT
        for path in sorted(paths):
            try:
                got = acquire_lock(path, max(deadline - time.monotonic(), 0.0), owner)
            except OSError as exc:
                release()
                raise io_error(
                    "The confirm lock cannot be taken; nothing was sent.",
                    f"Check that {d} is writable.",
                    exc,
                ) from None
            if not got:
                release()
                raise self.lock_held(paths[path], lock_holder(path))
            held.append(path)
        return release

    def idempotency_resolved(self, op_id: str) -> Error:
        """idempotency_resolved (CLI-062): the earlier request with this key was
        resolved by hand as applied or partial, or was a batch that ended
        partial. Nothing is sent again."""
        e = conflict_error(
            "idempotency_resolved",
            "A request with this idempotency key was already resolved by hand; nothing was sent.",
            f"Run `{self.tool()} operation show --id {op_id}` to see what was recorded; if the "
            "change is missing, repeat the request with a new --idempotency-key.",
        )
        e.details["operation_id"] = op_id
        return e

    def check_unresolved(self, j: Journal, targets: list[str], exclude: set[str]) -> None:
        self._guard(lambda: self.unresolved_error(j, targets, exclude))

    def unresolved_error(self, j: Journal, targets: list[str], exclude: set[str]) -> None:
        """Raise unresolved_operation when a target is blocked (not recorded)."""
        op_id = j.blocking(targets, exclude, self.a._now())
        if op_id:
            raise self.unresolved_conflict(op_id)

    def need(
        self, targets: list[str], destinations: list[str], outputs: list[str], operations: int
    ) -> ScopeNeed:
        return ScopeNeed(
            effects=self.m.effect_strings(),
            dangerous=self.m.is_dangerous(),
            targets=list(targets),
            destinations=list(destinations),
            outputs=list(outputs),
            operations=operations,
        )

    def identity(self) -> Identity:
        """The account and host from App.identity (local configuration)."""
        from .app import Identity
        from .builtins import redact_host

        if self.a.identity is None:
            return Identity()
        try:
            ident = self.a.identity()
        except Exception as exc:
            e = as_error(exc).copy()
            if e.code == "E_UNKNOWN":
                e = new_error(
                    "E_CONFIG",
                    "The account this write would act as cannot be resolved; nothing was sent.",
                )
            e.details["dispatch_state"] = "not_started"
            raise e from None
        if ident is None:
            return Identity()
        return Identity(
            account=ident.account, host=redact_host(ident.host), extra=dict(ident.extra or {})
        )

    def call_preview(self, fn: Callable[[], Preview]) -> Preview:
        """The tool's read-only preview. Its errors keep their code; the write
        itself was not started. Raises the Error (not recorded)."""
        try:
            pv = fn()
        except Exception as exc:
            e = as_error(exc).copy()
            e.details["dispatch_state"] = "not_started"
            raise e from None
        if not isinstance(pv, Preview):
            raise internal_error("A write's preview must return an aicli Preview.")
        return pv

    def call_apply(
        self, fn: Callable[[Op], Result | None], op: Op
    ) -> tuple[Result, BaseException | None]:
        """Run apply. Any exception is an outcome to classify: nobody can tell
        whether a request went out unless the tool says so."""
        try:
            res = fn(op)
        except BaseException as exc:  # every failure is classified, interrupts included
            return Result(), exc
        if res is None:
            res = Result()
        if not isinstance(res, Result):
            return Result(), TypeError("apply returned something other than an aicli Result")
        return res, None

    def check_declared(self, pv: Preview) -> Error | None:
        """Deliver and local-file writes declare what the scope must cover."""
        if self.m.has_effect("deliver") and not pv.destinations:
            return internal_error(
                "A deliver command must list Preview.destinations so the scope can be checked."
            )
        if self.m.has_effect("artifact", "persist") and not pv.outputs:
            return internal_error(
                "A command with artifact or persist effects must list Preview.outputs so the scope "
                "can be checked."
            )
        return None

    def action(self, pv: Preview) -> str:
        return pv.action if pv.action.strip() else "Run " + self.path

    def preview_object(
        self,
        op_id: str,
        t: Target | TargetRef,
        pv: Preview,
        inputs: dict[str, Any],
        can_apply: bool,
        blocks: list[Block],
    ) -> dict[str, Any]:
        """The preview as emitted and stored (BLUEPRINT 4.3)."""
        obj: dict[str, Any] = {
            "operation_id": op_id,
            "command": self.path,
            "action": self.action(pv),
            "target": target_object(t),
            "effects": self.m.effect_strings(),
            "dangerous": self.m.is_dangerous(),
            "changes": change_objects(pv.changes),
            "inputs": inputs,
            "can_apply": can_apply,
            "blocked_by": block_objects(blocks),
        }
        if pv.base_version:
            obj["base"] = {"version": pv.base_version, "source": pv.base_source or "version"}
        if pv.destinations:
            obj["destinations"] = list(pv.destinations)
        if pv.outputs:
            obj["outputs"] = list(pv.outputs)
        try:
            return to_generic(obj)
        except (TypeError, ValueError):
            return obj

    def claims(
        self,
        op_id: str,
        b: Binding,
        ident: Identity,
        target: str,
        scope: str,
        base: str,
        exp: _dt.datetime,
    ) -> Claims:
        return Claims(
            v=2,
            tool=claim_string(self.tool()),
            tool_version=claim_string(self.a._version()),
            op=op_id,
            cmd=claim_string(self.path),
            inv=b.inv,
            inputs=dict(b.digests),
            account=claim_string(ident.account),
            host=claim_string(ident.host),
            target=target,
            scope=scope,
            base=base,
            idem=self.idem,
            exp=time_string(exp),
        )

    def with_preview(
        self, e: Error, spec: WriteSpec, sc: ScopeState, need: ScopeNeed, j: Journal
    ) -> Error:
        """Add details.preview to a scope refusal when the preview can be built (it
        is read-only), so the person granting the scope sees what was asked for, and
        widen the refusal to the destinations and outputs the preview names, so
        that one grant covers the whole write."""
        try:
            pv = self.call_preview(spec.preview)
        except Error:
            return e
        if not need.destinations and not need.outputs and (pv.destinations or pv.outputs):
            full = dataclasses.replace(
                need, destinations=list(pv.destinations), outputs=list(pv.outputs)
            )
            try:
                self.a.check_scope(sc, full, j)
            except Error as wider:
                if wider.code == e.code:
                    e = wider
        return self.attach_preview(e, spec, pv)

    def attach_preview(self, e: Error, spec: WriteSpec, pv: Preview) -> Error:
        """Put a refused write's read-only preview in details.preview. Its
        operation id is never recorded and no token is issued."""
        op_id = new_operation_id(False)
        can, blocks = can_apply_of(pv)
        inputs: dict[str, Any] = {}
        try:
            # a throw-away key: secrets show as "***"; nothing keyed with it is stored
            b = self.a.bind_invocation(self.st, self.node, self.m, os.urandom(KEY_SIZE))
            inputs = b.display
        except Error:
            pass
        e.details["preview"] = self.preview_object(op_id, spec.target, pv, inputs, can, blocks)
        if pv.untrusted:
            e.untrusted = e.untrusted + prefix_paths("preview", pv.untrusted)
        return e

    def issue(self, p: Issued) -> None:
        """Report a plan: dry-run data, or E_CONFIRMATION_REQUIRED for a bare call."""
        if self.dry_run:
            data = {
                "preview": p.pobj,
                "confirm_token": p.token,
                "expires_at": p.expires,
                "operation_id": p.id,
            }
            self.succeed(data, prefix_paths("data.preview", p.untrusted))
            return
        msg = (
            "Confirmation required: nothing was changed; details.preview shows what the command "
            "would do."
        )
        nxt = (
            "Check details.preview; if it is what the user asked for, run the same command again "
            f"with{self.dangerous_arg()} --confirm {p.token}."
        )
        if not p.can_apply:
            msg = (
                "The write cannot be applied as previewed (details.preview.blocked_by); nothing "
                "was changed."
            )
            nxt = "Resolve what details.preview.blocked_by names, then run the command again."
        e = new_error(
            "E_CONFIRMATION_REQUIRED",
            msg,
            {
                "problem": "confirmation_required",
                "preview": p.pobj,
                "confirm_token": p.token,
                "expires_at": p.expires,
                "operation_id": p.id,
                "dispatch_state": "not_started",
                "next_action": nxt,
            },
        )
        e.untrusted = prefix_paths("preview", p.untrusted)
        raise self.fail(e)

    def with_fresh_plan(self, e: Error, spec: WriteSpec) -> Error:
        """Answer token_expired and state_drift with a new plan for the same command:
        its preview, token, expiry and operation id go into the conflict details, so
        the agent checks the new preview and confirms it in one call instead of
        two. When the write cannot be planned or applied now, the conflict stands
        alone."""
        if self.m.is_dangerous() and not self.dangerous_flag:
            return e
        try:
            p = self.prepare_single(spec)
        except Error:
            return e
        if not p.can_apply:
            return e
        e.details["preview"] = p.pobj
        e.details["confirm_token"] = p.token
        e.details["expires_at"] = p.expires
        e.details["operation_id"] = p.id
        e.untrusted = e.untrusted + prefix_paths("preview", p.untrusted)
        e.details["next_action"] = (
            "details.preview is a new plan: if it still matches what the user asked for, run the "
            "same command "
            f"again with{self.dangerous_arg()} --confirm {p.token}."
        )
        return e

    # ---------------------------------------------------------------- single write

    def plan_single(self, spec: WriteSpec) -> None:
        if self.m.is_dangerous() and not self.dangerous_flag:
            raise self.fail(self.dangerous_refusal(spec))
        p = self._guard(lambda: self.prepare_single(spec, True))
        if p.replay is not None:
            self.succeed(p.replay, p.untrusted)
            return
        self.issue(p)

    def lookup_key(self, j: Journal, state: str, target: str) -> Record | None:
        """The idempotency lookup of a dry-run or bare call (P4): it runs before
        Preview, so an earlier request with this --idempotency-key is answered even
        when a Preview would refuse the duplicate. Returns the operation to replay,
        or raises the mismatch, resolved or unresolved conflict, as at confirm."""
        ident = self.identity()
        key = self.a.load_key(state, True)
        assert key is not None
        b = self.a.bind_invocation(self.st, self.node, self.m, key)
        return self.idempotent(j, b, ident, target, set(), True)

    def prepare_single(self, spec: WriteSpec, lookup: bool = False) -> Issued:
        """The plan steps of a single write (CLI-051): scope, the idempotency lookup
        when a key is given (lookup; P4), unresolved operations, the read-only
        preview, the binding, the planned record and the token. Raises the Error
        that stops it (not recorded as the run's outcome)."""
        a = self.a
        state = a.ensure_state_dir()
        j = a.open_journal(state, True)
        sc = a.load_scope(state)
        need = self.need([spec.target.key], [], [], 1)
        try:
            a.check_scope(sc, need, j)
        except Error as e:
            raise self.with_preview(e, spec, sc, need, j) from None
        if lookup and self.idem:
            prev = self.lookup_key(j, state, claim_string(spec.target.key))
            if prev is not None:
                data, untrusted, _, _ = self.replay_of(j, prev)
                return Issued(pobj={}, id="", untrusted=untrusted, replay=data)
        self.unresolved_error(j, [spec.target.key], set())
        pv = self.call_preview(spec.preview)
        de = self.check_declared(pv)
        if de is not None:
            raise de
        need.destinations, need.outputs = list(pv.destinations), list(pv.outputs)
        try:
            a.check_scope(sc, need, j)
        except Error as e:
            raise self.with_preview(e, spec, sc, need, j) from None
        ident = self.identity()
        key = a.load_key(state, True)
        assert key is not None
        b = a.bind_invocation(self.st, self.node, self.m, key)
        self.idempotent(j, b, ident, claim_string(spec.target.key), set(), False)
        op_id = new_operation_id(False)
        now = a._now()
        exp = now + TOKEN_LIFETIME
        can_apply, blocks = can_apply_of(pv)
        pobj = self.preview_object(op_id, spec.target, pv, b.display, can_apply, blocks)
        cl = self.claims(
            op_id,
            b,
            ident,
            claim_string(spec.target.key),
            sc.digest,
            claim_string(pv.base_version),
            exp,
        )
        rec = Record(
            operation_id=op_id,
            kind="single",
            command=self.path,
            status="planned",
            target=TargetRef(spec.target.key, spec.target.kind, spec.target.label),
            effects=self.m.effect_strings(),
            dangerous=self.m.is_dangerous(),
            scope_id=sc.id(),
            created_at=time_string(now),
            expires_at=time_string(exp),
            claims=cl,
            preview=pobj,
        )
        j.write(rec)
        j.prune(now)
        return mint(pobj, op_id, key, cl, can_apply, exp, pv.untrusted)

    def _guard(self, fn: Callable[[], Any]) -> Any:
        """Run fn; an Error it raises becomes the gate's outcome."""
        try:
            return fn()
        except Error as e:
            raise self.fail(e) from None

    def load_record(self, j: Journal, part: str, kind: str) -> Record:
        """The record a token names; a missing record is token_mismatch, an
        unreadable or corrupt one E_IO."""
        from .journal import CorruptRecord

        prefix = "opb_" if kind == "batch" else "op_"
        try:
            r, exists = j.read(prefix + part)
        except (CorruptRecord, OSError) as exc:
            raise io_error(
                "The operation record for this token cannot be read; nothing was sent.",
                f"Run `{self.tool()} operation show --id {prefix}{part}` and ask the user to check "
                "the state directory.",
                exc,
            ) from None
        if not exists or r is None:
            raise self.token_mismatch("--confirm", "No open operation matches this confirm token.")
        if r.kind != kind:
            raise self.token_mismatch(
                "--confirm", "The confirm token belongs to a different kind of operation."
            )
        return r

    def verify_stored_token(self, kind: str) -> tuple[str, bytes, Journal, Record]:
        """Confirm steps 1-6: format, key, record, MAC, single use, expiry. A used
        token is token_used whatever its age, so only a token that was never used
        can come back as token_expired with a fresh plan (A1)."""
        a = self.a
        parsed = parse_token(self.confirm)
        if parsed is None:
            e = self.token_mismatch(
                "--confirm", "The confirm token is not in the form this tool issues."
            )
            # Only the exact form is accepted (CLI-055); say what to drop when the
            # token came wrapped or upper-cased, as from a copied `--confirm <token>`.
            bare = self.confirm.strip().strip("<>\"' ")
            if parse_token(bare.lower()) is not None:
                e.details["next_action"] = (
                    "Pass the confirm_token exactly as the preview issued it: lower case, "
                    "without angle brackets, quotes or spaces around it."
                )
            raise e
        part, mac_hex = parsed
        state = a.ensure_state_dir()
        key = a.load_key(state, False)
        if key is None:
            raise self.token_mismatch(
                "--confirm", "No confirm key exists, so this token cannot be from this tool."
            )
        j = a.open_journal(state, False)
        rec = self.load_record(j, part, kind)
        if (
            rec.claims is None
            or rec.claims.op != rec.operation_id
            or not verify_mac(key, rec.claims, mac_hex)
        ):
            raise self.token_mismatch(
                "--confirm", "The confirm token does not match its operation."
            )
        from ._util import parse_time

        if j.is_consumed(rec.operation_id) or rec.status != "planned":
            raise self.token_used(rec.operation_id)
        exp = parse_time(rec.claims.exp)
        if exp is None or not a._now() < exp:
            raise conflict_error(
                "token_expired",
                "The confirm token has expired; nothing was sent.",
                "Run the same command again with --dry-run to get a new token.",
            )
        return state, key, j, rec

    def compare_inputs(self, c: Claims, b: Binding) -> str:
        """Confirm step 7: the command, the tool version and every bound input
        must match the claims. Returns the first differing parameter."""
        if c.tool != claim_string(self.tool()) or c.cmd != claim_string(self.path):
            return "command"
        if c.tool_version != claim_string(self.a._version()):
            return "tool_version"
        for n in sorted(set(c.inputs) | set(b.digests)):
            if c.inputs.get(n, "") != b.digests.get(n, ""):
                if n in b.labels:
                    return b.labels[n]
                return self.label_of(n)
        if c.idem != self.idem:
            return "--idempotency-key"
        if c.inv != b.inv:
            return "--confirm"
        return ""

    def input_mismatch(self, rec: Record, b: Binding, label: str) -> Error:
        """token_mismatch for a bound input that differs from the token's, with a
        hint when one side came from a file and the other was given literally: both
        bind the exact bytes, so a file's trailing newline is a difference."""
        e = self.token_mismatch(label, f"The confirm token was issued for a different {label}.")
        stored = rec.preview if isinstance(rec.preview, dict) else {}
        inputs = stored.get("inputs") if isinstance(stored.get("inputs"), dict) else {}
        for file in sorted(self.m.file_inputs):
            bound = self.m.file_inputs[file]
            if label not in ("--" + bound, "--" + file):
                continue
            then, now = inputs.get(snake(bound)), b.display.get(snake(bound))
            if then is None or now is None or _file_shown(then) == _file_shown(now):
                break
            if _file_shown(then):
                n = then.get("bytes")
                e.details["next_action"] = (
                    f"The token binds the exact bytes of the file given to --{file} ({n} bytes, "
                    "including any "
                    f"trailing newline), which differ from the literal --{bound}. Confirm with the "
                    f"same --{file}, "
                    "or run --dry-run again with the literal value."
                )
            else:
                e.details["next_action"] = (
                    f"The token binds the literal --{bound}, which differs from the file's exact "
                    "bytes (including "
                    f"any trailing newline). Confirm with --{bound} as before, or run --dry-run "
                    f"again with --{file}."
                )
            break
        return e

    def label_of(self, name: str) -> str:
        """The label of a bound input that is absent from the current invocation."""
        from .binding import label_for

        if name in ("args", "profile") or name.startswith("setting."):
            return name
        if self.node.flag(name) is not None:
            return "--" + name
        for file in sorted(self.m.file_inputs):
            if self.m.file_inputs[file] == name:
                return label_for(self.node, name, file)
        return "--" + name

    def check_drift(
        self, base: str, stored: dict[str, Any], pv: Preview, t: Target | TargetRef, op_id: str
    ) -> None:
        """Re-read the target through preview at confirm (CLI-057): a changed base
        version or a changed plan is state_drift."""

        def drift(why: str) -> Error:
            e = conflict_error(
                "state_drift",
                "The target changed since the preview (" + why + "); nothing was sent.",
                "Run the same command again with --dry-run, check the new preview, then confirm "
                "its token.",
            )
            e.details["target"] = t.key
            return e

        if claim_string(pv.base_version) != base:
            raise drift("its version differs")
        can, blocks = can_apply_of(pv)
        if not can:
            e = drift("it can no longer be applied")
            e.details["blocked_by"] = block_objects(blocks)
            raise e
        fresh = self.preview_object(op_id, t, pv, {}, True, [])
        for k in ("action", "changes", "destinations", "outputs"):
            if not json_equal(stored.get(k), fresh.get(k)):
                raise drift("the planned " + k + " differ")

    def confirm_single(self, spec: WriteSpec) -> None:
        a = self.a
        try:
            state, key, j, rec = self.verify_stored_token("single")
        except Error as e:
            if e.details.get("conflict") == "token_expired":
                e = self.with_fresh_plan(e, spec)
            raise self.fail(e) from None
        assert rec.claims is not None
        b = self._guard(lambda: a.bind_invocation(self.st, self.node, self.m, key))
        p = self.compare_inputs(rec.claims, b)
        if p:
            raise self.fail(self.input_mismatch(rec, b, p))
        if self.m.is_dangerous() and not self.dangerous_flag:
            raise self.fail(self.dangerous_required())
        release = self._guard(lambda: self.lock_targets(state, [spec.target.key], rec.operation_id))
        try:
            timeout = self.checks_single(spec, state, j, rec, b)
        finally:
            release()
        if timeout is None:
            return  # an idempotent replay: nothing was sent
        stored = rec.preview if isinstance(rec.preview, dict) else {}
        try:
            res, apply_err = self.call_apply(spec.apply, Op(rec.operation_id, self.idem, timeout))
            out = self.classify(rec.operation_id, spec.target, stored, res, apply_err)
            self.finish_record(j, rec, out)
            self.report(rec.operation_id, out, res, spec)
        except Error:
            if self.st.gate.set:
                raise  # the gate's own report
            raise self.fail(self.after_dispatch_error(rec.operation_id)) from None
        except (Exception, KeyboardInterrupt) as exc:
            # the record stays dispatching: its lease blocks the target, then it reads
            # as unknown
            raise self.fail(self.after_dispatch_error(rec.operation_id, exc)) from None

    def checks_single(
        self, spec: WriteSpec, state: str, j: Journal, rec: Record, b: Binding
    ) -> float | None:
        """Confirm steps 9 to 14 under the target lock: the single-use marker again,
        scope and budget, unresolved operations, preview and drift, identity, the
        idempotency key, the marker and the budget count, and the dispatching
        record. Returns apply's timeout, or None when an idempotent replay answered
        the request."""
        a = self.a
        assert rec.claims is not None
        if self._guard(lambda: j.is_consumed(rec.operation_id)):
            raise self.fail(self.token_used(rec.operation_id))  # lost a race on this token
        stored = rec.preview if isinstance(rec.preview, dict) else {}
        sc = a.load_scope(state)
        need = self.need(
            [spec.target.key], strings_at(stored, "destinations"), strings_at(stored, "outputs"), 0
        )
        self._guard(lambda: a.check_scope(sc, need, j))
        if sc.digest != rec.claims.scope:
            raise self.fail(self.scope_changed())
        need.operations = 1
        self._guard(lambda: a.check_scope(sc, need, j))
        self.check_unresolved(j, [spec.target.key], {rec.operation_id})
        pv = self._guard(lambda: self.call_preview(spec.preview))
        try:
            self.check_drift(rec.claims.base, stored, pv, spec.target, rec.operation_id)
        except Error as e:
            if "blocked_by" not in e.details:
                e = self.with_fresh_plan(e, spec)
            raise self.fail(e) from None
        ident = self._guard(self.identity)
        self.check_identity(rec.claims, ident)
        if claim_string(spec.target.key) != rec.claims.target:
            raise self.fail(
                self.token_mismatch(
                    "target", "The confirm token was issued for a different target."
                )
            )
        target = claim_string(spec.target.key)
        prev = self._guard(lambda: self.idempotent(j, b, ident, target, {rec.operation_id}, True))
        now = a._now()
        if prev is not None:
            self.replay(j, sc, rec, prev, now)
            return None
        self._guard(lambda: self.consume_counted(j, sc, need, rec.operation_id, 1, now))
        timeout = spec.timeout if spec.timeout > 0 else DEFAULT_TIMEOUT
        rec.status = "dispatching"
        rec.dispatched_at = time_string(now)
        rec.lease_until = time_string(now + _dt.timedelta(seconds=timeout + LEASE_GRACE))
        self._guard(lambda: j.write(rec))
        self.dispatched_id = rec.operation_id
        return timeout

    def scope_changed(self) -> Error:
        return conflict_error(
            "scope_changed",
            "The scope file changed since the preview; nothing was sent.",
            "Run the same command again with --dry-run to get a token under the current scope.",
        )

    def check_identity(self, c: Claims, ident: Identity) -> None:
        """The account and host the write acts as must be those of the token."""
        if claim_string(ident.account) != c.account:
            raise self.fail(
                self.token_mismatch(
                    "account", "The confirm token was issued for a different account."
                )
            )
        if claim_string(ident.host) != c.host:
            raise self.fail(
                self.token_mismatch("host", "The confirm token was issued for a different host.")
            )

    def consume_counted(
        self, j: Journal, sc: ScopeState, need: ScopeNeed, op_id: str, n: int, now: _dt.datetime
    ) -> None:
        """Confirm step 13 (A4): under the budget lock, create the single-use marker
        of op_id and add n operations to its scope's count in scope-usage.json. A
        marker that exists is token_used; a count that would pass
        limits.max_operations is the budget_spent refusal, and then no marker is
        created. n is 0 for a replay, which sends nothing."""
        unlock = lock_scope_usage(j.state_dir, op_id)
        try:
            if j.is_consumed(op_id):
                raise self.token_used(op_id)
            scope_id = sc.id()
            if scope_id is not None and n > 0:
                usage = read_scope_usage(j.state_dir)
                count = usage.get(scope_id, 0)
                limit = sc.file.max_operations if sc.file is not None else None
                if limit is not None and count + n > limit:
                    need.operations = n
                    raise self.a.scope_refusal(sc, "budget_spent", need, {}, count)
                usage[scope_id] = count + n
                write_scope_usage(j.state_dir, usage)
            if not j.consume(op_id, now):
                raise self.token_used(op_id)
        finally:
            unlock()

    def refund_budget(self, j: Journal, scope_id: str | None, n: int) -> None:
        """Give back the budget of batch items that were never dispatched (A4).
        Best effort: a failure only over-counts."""
        if not scope_id or n <= 0:
            return
        try:
            unlock = lock_scope_usage(j.state_dir, "")
        except Error:
            return
        try:
            usage = read_scope_usage(j.state_dir)
            usage[scope_id] = max(usage.get(scope_id, 0) - n, 0)
            write_scope_usage(j.state_dir, usage)
        except Error:
            pass
        finally:
            unlock()

    def idempotent(
        self,
        j: Journal,
        b: Binding,
        ident: Identity,
        target: str,
        exclude: set[str],
        full: bool,
    ) -> Record | None:
        """The idempotency lookup (CLI-062, CLI-057 step 12, A5) over every earlier
        operation of any command, single write or batch, confirmed with this
        --idempotency-key. They match only when the command path, inputs digest,
        target, account and host are all equal; any difference is
        idempotency_mismatch naming the latest one that differs, so a reused key
        never answers with another request's receipt. At confirm (full) their
        states then decide, the one that never sends a write twice first: one
        still unknown is unresolved_operation; one resolved by hand as applied or
        partial, or that ended partial, is idempotency_resolved; one applied is
        returned, to replay; otherwise (not applied, failed, resolved as not
        applied) None, and the request may be sent again. target is the claim
        value (a batch's is the digest of its target list)."""
        if not self.idem:
            return None
        found = j.find_idempotent(self.idem, exclude)
        for prev in found:
            c = prev.claims
            if (
                c is None
                or prev.command != self.path
                or c.cmd != claim_string(self.path)
                or c.inv != b.inv
                or c.target != target
                or c.account != claim_string(ident.account)
                or c.host != claim_string(ident.host)
            ):
                ce = conflict_error(
                    "idempotency_mismatch",
                    "This idempotency key was already used for a different request; nothing "
                    "was sent.",
                    "Use a new --idempotency-key for a different request, or repeat the "
                    "original request exactly.",
                )
                ce.details["operation_id"] = prev.operation_id
                raise ce
        if not full:
            return None
        now = self.a._now()
        for prev in found:
            if prev.resolution is None and unresolved(effective_status(prev, now)):
                raise self.unresolved_conflict(prev.operation_id)
        replayable: Record | None = None
        for prev in found:
            st = effective_status(prev, now)
            res = prev.resolution
            if (res is not None and res.outcome != "not_applied") or (
                res is None and st == "partial"
            ):
                # part or all of it took effect: sending it again could apply that twice
                raise self.idempotency_resolved(prev.operation_id)
            if res is None and st == "applied":
                if prev.kind == "batch" or prev.receipt is not None:
                    if replayable is None:
                        replayable = prev
                else:
                    # applied, with nothing to replay
                    raise self.idempotency_resolved(prev.operation_id)
        return replayable

    def replay_of(self, j: Journal, prev: Record) -> tuple[dict[str, Any], list[str], str, Record]:
        """The answer to a repeated idempotent request (CLI-062): the original
        operation's id, its receipt with replayed: true and its result (projected by
        --fields, P12). It also returns the original's id and the record the answer
        came from, which a confirm records its replay against."""
        origin, src = origin_of(j, prev)
        receipt = dict(src.receipt) if isinstance(src.receipt, dict) else {}
        receipt["replayed"] = True
        untrusted = []
        obs = receipt.get("observed")
        if isinstance(obs, dict) and obs:
            untrusted.append("data.receipt.observed")
        # The original result (the created resource's ref and url, CLI-059) comes
        # back with the replay; its paths of external text are not recorded, so all
        # of it is declared untrusted.
        result: Any = {}
        if src.result is not None:
            result = self.project_result(src.result)
            untrusted.append("data.result")
        return (
            {"operation_id": origin, "receipt": receipt, "result": result},
            untrusted,
            origin,
            src,
        )

    def replay(
        self, j: Journal, sc: ScopeState, rec: Record, prev: Record, now: _dt.datetime
    ) -> None:
        """Answer a repeated idempotent request at confirm with the original receipt
        and result (CLI-062) and record the replay; nothing is sent and nothing is
        counted against the budget."""
        data, untrusted, origin, src = self.replay_of(j, prev)
        self._guard(lambda: self.consume_counted(j, sc, ScopeNeed(), rec.operation_id, 0, now))
        rec.status = "applied"
        rec.finished_at = time_string(now)
        rec.replay_of = origin
        rec.receipt = data["receipt"]
        rec.result = src.result
        try:
            j.write(rec)
        except Error:
            self.a.notice(journal_notice())
        self.succeed(data, untrusted)

    def project_result(self, result: Any) -> Any:
        """Apply --fields to a confirmed write's result with the command's output
        schema, as App.ok does for a read (P12). Without --fields the result is
        returned whole (a write's default fields do not apply); the receipt is never
        projected."""
        if not self.st.fields.set:
            return result
        try:
            gen = to_generic(result)
        except (TypeError, ValueError):
            return result
        if gen is None:
            return result
        schema = self.a.schema_of(self.m)
        shape = schema.shape or ("array" if isinstance(gen, list) else "object")
        return self.a.project(self.st, self.m, schema, shape, gen)

    def classify(
        self,
        op_id: str,
        t: Target | TargetRef,
        stored: dict[str, Any],
        res: Result,
        apply_err: BaseException | None,
    ) -> WriteOutcome:
        """Apply's result as a receipt and the derived ok/code (CLI-059)."""
        m = self.m
        changes = m.has_effect("change", "configure")
        delivers = m.has_effect("deliver")
        persists = m.has_effect("artifact", "persist")
        cause: Error | None = None
        dispatch = "dispatched"
        observed: dict[str, Any] = {}
        mismatched = list(res.mismatched or [])
        if apply_err is None:
            o = normalize_outcome(res.outcome, m)
            if res.observed is not None:
                try:
                    gv = to_generic(res.observed)
                except (TypeError, ValueError):
                    gv = None
                if isinstance(gv, dict):
                    observed = gv
            if not res.outcome.verification:
                changes_list = (
                    stored.get("changes") if isinstance(stored.get("changes"), list) else []
                )
                v, mm = auto_verify(changes_list, observed)
                o.verification = v
                for f in mm:
                    if f not in mismatched:
                        mismatched.append(f)
            # A mismatched field is one whose read-back is in observed (CLI-059).
            kept: list[str] = []
            for f in mismatched:
                if f.split(".", 1)[0] in observed and f not in kept:
                    kept.append(f)
            dropped = len(kept) < len(mismatched)
            mismatched = kept
            if dropped:
                self.a.defect(
                    "Apply named mismatched fields that are not in Result.observed (CLI-059); "
                    "the gate dropped them."
                )
            if mismatched and o.verification in ("passed", "not_run"):
                o.verification = "failed"
            elif not observed and o.verification == "passed":
                # Verification that passed or failed shows what it observed (decision
                # 15); without a read-back the gate cannot report it as checked.
                o.verification = "not_run"
                self.a.defect(
                    "Apply reported verification passed with an empty Result.observed "
                    "(decision 15); the receipt says not_run."
                )
            elif o.verification == "failed" and not mismatched:
                # A failure that names no observed field could not be completed.
                o.verification = "incomplete"
                self.a.defect(
                    "Apply reported verification failed without a mismatched field in "
                    "Result.observed; the receipt says incomplete."
                )
            elif dropped and o.verification == "passed":
                o.verification = "incomplete"
        elif isinstance(apply_err, NotDispatchedError):
            o = Outcome(
                execution="not_started",
                change=pick(changes, "not_applied", "not_applicable"),
                delivery=pick(delivers, "not_attempted", "not_applicable"),
                persistence=pick(persists, "unchanged", "not_applicable"),
                verification="not_applicable",
            )
            cause = cause_of(apply_err.err)
            dispatch = "not_started"
            mismatched = []
        elif isinstance(apply_err, RejectedError):
            o = Outcome(
                execution="failed",
                change=pick(changes, "not_applied", "not_applicable"),
                delivery=pick(delivers, "rejected", "not_applicable"),
                persistence=pick(persists, "unchanged", "not_applicable"),
                verification="not_applicable",
                failed_stage="apply",
            )
            cause = cause_of(apply_err.err)
            mismatched = []
        else:
            o = Outcome(
                execution="unknown",
                change=pick(changes, "unknown", "not_applicable"),
                delivery=pick(delivers, "unknown", "not_applicable"),
                persistence=pick(persists, "unknown", "not_applicable"),
                verification="not_run",
            )
            dispatch = "unknown"
            mismatched = []
        o = sanitize_outcome(o)
        cause_code = ""
        if cause is not None:
            cause_code = cause.code if self.a.lookup_code(cause.code) is not None else ""
        ok, code = derive_receipt(o, cause_code)
        if code == "E_OUTCOME_UNKNOWN":
            dispatch = "unknown"
        receipt: dict[str, Any] = {
            "operation_id": op_id,
            "command": self.path,
            "target": target_object(t),
            "effects": tuple(m.effect_strings()),
            "outcome": {
                "execution": o.execution,
                "change": o.change,
                "delivery": o.delivery,
                "persistence": o.persistence,
                "verification": o.verification,
            },
            "observed": observed,
            "mismatched": tuple(mismatched),
            "replayed": False,
        }
        if o.failed_stage:
            receipt["failed_stage"] = o.failed_stage
        data: Any = {}
        unencodable = False
        if apply_err is None and res.data is not None:
            # The receipt stands whatever the tool's result is; a result that
            # cannot be encoded becomes {} (A2).
            try:
                data = to_generic(res.data)
            except (TypeError, ValueError, RecursionError):
                data, unencodable = {}, True
            if data is None:
                data = {}
        return WriteOutcome(
            receipt=receipt,
            ok=ok,
            code=code,
            cause=cause,
            dispatch=dispatch,
            status=record_status(ok, code, o),
            outcome=o,
            observed=observed,
            result_data=data,
            unencodable=unencodable,
        )

    def finish_record(self, j: Journal, rec: Record, out: WriteOutcome) -> None:
        """Store the final status and receipt (confirm step 18). The record is read
        again under its lock: one that was resolved by hand while apply ran keeps
        its resolution (the late receipt is kept beside it), and the outcome of
        this run is reported as unknown (out.resolved_meanwhile, A6)."""
        unlock = None
        try:
            unlock = j.lock(rec.operation_id)
        except Error:
            pass
        try:
            try:
                cur, exists = j.read(rec.operation_id)
            except Exception:
                cur, exists = None, False
            if exists and cur is not None and cur.resolution is not None:
                out.resolved_meanwhile = True
                cur.late_receipt = to_generic(out.receipt)
                try:
                    j.write(cur)
                except Error:
                    self.a.notice(journal_notice())
                return
            now = time_string(self.a._now())
            rec.status = out.status
            rec.finished_at = now
            rec.lease_until = None
            rec.receipt = to_generic(out.receipt)
            if out.ok:
                try:
                    stored = to_generic(out.result_data)
                    if len(go_json(stored, escape_html=True).encode("utf-8")) <= MAX_STORED_RESULT:
                        rec.result = stored
                except (TypeError, ValueError):
                    pass
            if not out.ok:
                msg = truncate_runes(out.cause.message, 200) if out.cause is not None else ""
                rec.error = RecordError(out.code, msg)
            try:
                j.write(rec)
            except Error:
                self.a.notice(journal_notice())
        finally:
            if unlock is not None:
                unlock()

    def report(
        self, op_id: str, out: WriteOutcome, res: Result, spec: WriteSpec | None = None
    ) -> None:
        """Emit a confirmed write per the receipt rules (confirm step 19)."""
        t = self.tool()
        lookup = f"{t} operation show --id {op_id}"
        if out.resolved_meanwhile:
            raise self.fail(self.resolved_meanwhile_error(op_id))
        if out.ok:
            untrusted: list[str] = []
            if out.unencodable:
                self.unencodable_notice(op_id)
            else:
                untrusted = prefix_paths("data.result", untrusted_paths(res.untrusted))
            if out.observed:
                untrusted.append("data.receipt.observed")
            self.succeed(
                {
                    "operation_id": op_id,
                    "receipt": out.receipt,
                    "result": self.project_result(out.result_data),
                },
                untrusted,
            )
            return
        if out.code == "E_OUTCOME_UNKNOWN":
            target_key = spec.target.key if spec is not None else ""
            check = f"Read {target_key} upstream"
            rb = (spec.read_back if spec is not None else "").strip()
            if rb:
                check = f"Run `{t} {rb}`"
            e = new_error(
                out.code,
                "The write may have been applied; nothing will be retried. Look the operation up "
                "before doing anything else.",
                {
                    "operation_id": op_id,
                    "lookup": lookup,
                    "receipt": out.receipt,
                    "dispatch_state": "unknown",
                    "next_action": (
                        f"Do not resend. {check} to see whether the change landed, then record "
                        "what you found "
                        f"with `{t} operation resolve --id {op_id} --outcome "
                        "applied|not_applied|partial --evidence <text>`."
                    ),
                },
            )
        elif out.code == "E_PARTIAL":
            e = new_error(
                out.code,
                "Part of the write took effect; details.receipt shows which parts.",
                {
                    "operation_id": op_id,
                    "receipt": out.receipt,
                    "dispatch_state": "dispatched",
                    "next_action": "Read details.receipt; never resend the whole request. "
                    f"`{lookup}` shows the record.",
                },
            )
        elif out.code == "E_VERIFY_FAILED":
            e = new_error(
                out.code,
                "The write was applied, but the read-back disagrees with the request "
                "(details.receipt.mismatched).",
                {
                    "operation_id": op_id,
                    "receipt": out.receipt,
                    "dispatch_state": "dispatched",
                    "next_action": "Re-read the target and tell the user which fields differ; do "
                    "not resend blindly.",
                },
            )
        elif out.code == "E_VERIFY_INCOMPLETE":
            e = new_error(
                out.code,
                "The write was applied, but the read-back could not be completed.",
                {
                    "operation_id": op_id,
                    "receipt": out.receipt,
                    "dispatch_state": "dispatched",
                    "next_action": "Re-read the target to check the change before relying on it.",
                },
            )
        else:
            if out.cause is not None and out.cause.code == out.code:
                e = out.cause.copy()
            else:
                e = new_error(out.code, "The write was not applied.")
            # The receipt moves to error.details.receipt when ok is false (CLI-059); a
            # dispatched write's error always says what happened to it (decision 1).
            e.details["operation_id"] = op_id
            e.details["receipt"] = out.receipt
            e.details["dispatch_state"] = out.dispatch
            if out.observed:
                e.untrusted = e.untrusted + ["receipt.observed"]
            own = e.details.get("next_action")
            na = own if isinstance(own, str) else ""
            if out.dispatch == "not_started" and not na.strip():
                e.details["next_action"] = (
                    "Nothing was sent. Fix the cause, then run the command again with --dry-run "
                    "for a new token."
                )
            elif na == "":
                e.details["next_action"] = (
                    "The upstream refused the write and nothing changed; fix the request, then run "
                    "--dry-run again."
                )
            if not e.message.strip():
                e.message = "The write was not applied."
            raise self.fail(e)
        if out.observed:
            e.untrusted = e.untrusted + ["receipt.observed"]
        raise self.fail(e)

    # ---------------------------------------------------------------- batch write

    def preview_items(self, items: list[BatchItem]) -> list[ItemPlan]:
        """Preview every item. An item the upstream says cannot be changed (not
        found, invalid, forbidden, conflict, ambiguous) is blocked; any other
        failure fails the batch."""
        plans = []
        for it in items:
            p = ItemPlan(item=it)
            try:
                pv = self.call_preview(it.preview)
            except Error as e:
                if e.code in (
                    "E_NOT_FOUND",
                    "E_VALIDATION",
                    "E_FORBIDDEN",
                    "E_CONFLICT",
                    "E_AMBIGUOUS",
                ):
                    p.can = False
                    p.blocks = [Block(e.code.removeprefix("E_").lower(), e.message)]
                else:
                    raise
            else:
                de = self.check_declared(pv)
                if de is not None:
                    raise de from None
                p.pv = pv
                p.can, p.blocks = can_apply_of(pv)
            plans.append(p)
        return plans

    def plan_batch(self, items: list[BatchItem]) -> None:
        a = self.a
        if self.m.is_dangerous() and not self.dangerous_flag:
            raise self.fail(self.dangerous_required())
        state = self._guard(a.ensure_state_dir)
        j = self._guard(lambda: a.open_journal(state, True))
        keys = [it.target.key for it in items]
        sc = a.load_scope(state)
        need = self.need(keys, [], [], len(items))
        self._guard(lambda: a.check_scope(sc, need, j))
        # With --idempotency-key, an earlier batch with this key is answered before
        # any Preview runs (P4).
        if self.idem:
            prev = self._guard(lambda: self.lookup_key(j, state, digest_list(keys)))
            if prev is not None:
                data, untrusted, _, _ = self._guard(lambda: self.batch_replay_of(j, prev))
                self.succeed(data, untrusted)
                return
        self.check_unresolved(j, keys, set())
        plans = self._guard(lambda: self.preview_items(items))
        for p in plans:
            need.destinations.extend(p.pv.destinations)
            need.outputs.extend(p.pv.outputs)
        self._guard(lambda: a.check_scope(sc, need, j))
        ident = self._guard(self.identity)
        key = self._guard(lambda: a.load_key(state, True))
        assert key is not None
        b = self._guard(lambda: a.bind_invocation(self.st, self.node, self.m, key))
        self._guard(lambda: self.idempotent(j, b, ident, digest_list(keys), set(), False))
        batch_id = new_operation_id(True)
        now = a._now()
        exp = now + TOKEN_LIFETIME
        can_apply = True
        aggregate: list[Block] = []
        targets: list[Any] = []
        preview_items: list[Any] = []
        bases: list[str] = []
        untrusted: list[str] = []
        item_ids: list[str] = []
        for p in plans:
            p.id = new_operation_id(False)
            item_ids.append(p.id)
            p.pobj = self.preview_object(p.id, p.item.target, p.pv, {}, p.can, p.blocks)
            if not p.can:
                can_apply = False
                for bl in p.blocks:
                    aggregate.append(Block(bl.kind, p.item.target.key + ": " + bl.message))
            targets.append(target_object(p.item.target))
            preview_items.append(
                {
                    "target": target_object(p.item.target),
                    "operation_id": p.id,
                    "changes": p.pobj["changes"],
                    "can_apply": p.can,
                    "blocked_by": p.pobj["blocked_by"],
                }
            )
            bases.append(claim_string(p.pv.base_version))
            untrusted.extend(prefix_paths("items[]", p.pv.untrusted))
        action = self.action(plans[0].pv)
        if len(plans) > 1:
            action = f"{action} ({len(plans)} targets)"
        pobj: dict[str, Any] = {
            "operation_id": batch_id,
            "command": self.path,
            "action": action,
            "effects": self.m.effect_strings(),
            "dangerous": self.m.is_dangerous(),
            "inputs": b.display,
            "can_apply": can_apply,
            "blocked_by": block_objects(aggregate),
            "targets": targets,
            "total": len(plans),
            "items": preview_items,
        }
        if need.destinations:
            pobj["destinations"] = list(need.destinations)
        if need.outputs:
            pobj["outputs"] = list(need.outputs)
        try:
            pobj = to_generic(pobj)
        except (TypeError, ValueError):
            pass
        cl = self.claims(batch_id, b, ident, digest_list(keys), sc.digest, digest_list(bases), exp)
        for p in plans:
            ir = Record(
                operation_id=p.id,
                kind="item",
                batch_id=batch_id,
                command=self.path,
                status="planned",
                target=TargetRef(p.item.target.key, p.item.target.kind, p.item.target.label),
                effects=self.m.effect_strings(),
                dangerous=self.m.is_dangerous(),
                scope_id=sc.id(),
                created_at=time_string(now),
                expires_at=time_string(exp),
                preview=p.pobj,
            )
            self._guard(lambda ir=ir: j.write(ir))
        rec = Record(
            operation_id=batch_id,
            kind="batch",
            command=self.path,
            status="planned",
            effects=self.m.effect_strings(),
            dangerous=self.m.is_dangerous(),
            scope_id=sc.id(),
            created_at=time_string(now),
            expires_at=time_string(exp),
            claims=cl,
            preview=pobj,
            items=item_ids,
        )
        self._guard(lambda: j.write(rec))
        j.prune(now)
        self.issue(self._guard(lambda: mint(pobj, batch_id, key, cl, can_apply, exp, untrusted)))

    def confirm_batch(self, spec: BatchSpec, items: list[BatchItem]) -> None:
        a = self.a
        state, key, j, rec = self._guard(lambda: self.verify_stored_token("batch"))
        assert rec.claims is not None
        b = self._guard(lambda: a.bind_invocation(self.st, self.node, self.m, key))
        p = self.compare_inputs(rec.claims, b)
        if p:
            raise self.fail(
                self.token_mismatch(p, f"The confirm token was issued for a different {p}.")
            )
        if self.m.is_dangerous() and not self.dangerous_flag:
            raise self.fail(self.dangerous_required())
        item_recs: list[Record] = []
        for op_id in rec.items or []:
            try:
                ir, exists = j.read(op_id)
            except Exception as exc:  # corrupt or unreadable
                ir, exists, err = None, True, exc
            else:
                err = None
            if not exists or ir is None or ir.kind != "item" or ir.batch_id != rec.operation_id:
                raise self.fail(
                    io_error(
                        "A planned item of this batch cannot be read; nothing was sent.",
                        f"Run `{self.tool()} operation show --id {rec.operation_id}`, then run the "
                        "command again with --dry-run.",
                        err,
                    )
                )
            if ir.status != "planned":
                raise self.fail(self.token_used(rec.operation_id))
            item_recs.append(ir)
        keys = [it.target.key for it in items]
        release = self._guard(lambda: self.lock_targets(state, keys, rec.operation_id))
        try:
            ready = self.checks_batch(spec, items, state, j, rec, item_recs, b)
        finally:
            release()
        if ready is None:
            return  # an idempotent replay of the whole batch: nothing was sent
        timeout, sc_id = ready
        continue_on_error = not self.m.is_dangerous()
        f = self.node.flag("continue-on-error")
        if f is not None:
            continue_on_error = bool(f.value)
        if spec.continue_on_error is not None:
            continue_on_error = spec.continue_on_error
        try:
            self.run_batch(j, rec, items, item_recs, timeout, continue_on_error, sc_id)
        except Error:
            if self.st.gate.set:
                raise  # the gate's own report
            raise self.fail(self.after_dispatch_error(rec.operation_id)) from None
        except (Exception, KeyboardInterrupt) as exc:
            raise self.fail(self.after_dispatch_error(rec.operation_id, exc)) from None

    def checks_batch(
        self,
        spec: BatchSpec,
        items: list[BatchItem],
        state: str,
        j: Journal,
        rec: Record,
        item_recs: list[Record],
        b: Binding,
    ) -> tuple[float, str | None] | None:
        """Confirm steps 9 to 14 of a batch, under every item target's lock: the
        single-use marker again, scope and budget (each item counts), unresolved
        operations, targets, previews and drift, identity, the batch's idempotency
        key, the marker and the budget count, and the dispatching record. Returns
        apply's timeout per item and the scope id, or None when an idempotent
        replay answered the request."""
        a = self.a
        assert rec.claims is not None
        if self._guard(lambda: j.is_consumed(rec.operation_id)):
            raise self.fail(self.token_used(rec.operation_id))  # lost a race on this token
        keys = [it.target.key for it in items]
        stored = rec.preview if isinstance(rec.preview, dict) else {}
        sc = a.load_scope(state)
        need = self.need(keys, strings_at(stored, "destinations"), strings_at(stored, "outputs"), 0)
        self._guard(lambda: a.check_scope(sc, need, j))
        if sc.digest != rec.claims.scope:
            raise self.fail(self.scope_changed())
        need.operations = len(items)
        self._guard(lambda: a.check_scope(sc, need, j))
        exclude = {rec.operation_id, *(rec.items or [])}
        self.check_unresolved(j, keys, exclude)
        if digest_list(keys) != rec.claims.target or len(items) != len(item_recs):
            raise self.fail(
                self.token_mismatch(
                    "target", "The confirm token was issued for a different set of targets."
                )
            )
        bases = []
        for it, ir in zip(items, item_recs, strict=False):
            if ir.target is None or it.target.key != ir.target.key:
                raise self.fail(
                    self.token_mismatch(
                        "target", "The confirm token was issued for a different set of targets."
                    )
                )
            pv = self._guard(lambda it=it: self.call_preview(it.preview))
            bases.append(claim_string(pv.base_version))
            item_stored = ir.preview if isinstance(ir.preview, dict) else {}
            base = ""
            bm = item_stored.get("base")
            if isinstance(bm, dict) and isinstance(bm.get("version"), str):
                base = bm["version"]
            self._guard(
                lambda it=it, ir=ir, pv=pv, item_stored=item_stored, base=base: self.check_drift(
                    claim_string(base), item_stored, pv, it.target, ir.operation_id
                )
            )
        if digest_list(bases) != rec.claims.base:
            raise self.fail(
                conflict_error(
                    "state_drift",
                    "A target changed since the preview; nothing was sent.",
                    "Run the same command again with --dry-run, check the new preview, then "
                    "confirm its token.",
                )
            )
        ident = self._guard(self.identity)
        self.check_identity(rec.claims, ident)
        # the batch's own key is looked up like a single write's (A5)
        prev = self._guard(
            lambda: self.idempotent(j, b, ident, digest_list(keys), {rec.operation_id}, True)
        )
        now = a._now()
        if prev is not None:
            self.replay_batch(j, sc, rec, item_recs, prev, now)
            return None
        self._guard(lambda: self.consume_counted(j, sc, need, rec.operation_id, len(items), now))
        timeout = spec.timeout if spec.timeout > 0 else DEFAULT_TIMEOUT
        rec.status = "dispatching"
        rec.dispatched_at = time_string(now)
        rec.lease_until = time_string(
            now + _dt.timedelta(seconds=timeout * len(items) + LEASE_GRACE)
        )
        self._guard(lambda: j.write(rec))
        self.dispatched_id = rec.operation_id
        return timeout, sc.id()

    def batch_replay_of(
        self, j: Journal, prev: Record
    ) -> tuple[dict[str, Any], list[str], Record, list[tuple[dict[str, Any], Any]]]:
        """The answer to a repeated batch (A5): the original batch's id, its per-item
        receipts (replayed: true) and results (projected by --fields, P12), and a
        summary. It also returns the record the answer came from and, per item, the
        receipt and result a confirm records its replay with. Items that cannot be
        read make the replay impossible: raises E_IO, and nothing is sent."""
        origin, src = origin_of(j, prev)
        entries: list[Any] = []
        copies: list[tuple[dict[str, Any], Any]] = []
        observed_any = result_any = False
        for item_id in src.items or []:
            err: BaseException | None = None
            try:
                it, exists = j.read(item_id)
            except (CorruptRecord, OSError, Error) as exc:
                it, exists, err = None, False, exc
            if not exists or it is None or it.target is None or it.receipt is None:
                raise io_error(
                    "The earlier batch with this idempotency key cannot be read, so its "
                    "result cannot be repeated; nothing was sent.",
                    f"Run `{self.tool()} operation show --id {origin}` and ask the user to "
                    "check the state directory.",
                    err,
                )
            receipt = dict(it.receipt) if isinstance(it.receipt, dict) else {}
            receipt["replayed"] = True
            entry: dict[str, Any] = {
                "target": it.target.key,
                "operation_id": it.operation_id,
                "ok": True,
                "receipt": receipt,
            }
            obs = receipt.get("observed")
            if isinstance(obs, dict) and obs:
                observed_any = True
            if it.result is not None:
                entry["result"] = self.project_result(it.result)
                result_any = True
            entries.append(entry)
            copies.append((receipt, it.result))
        untrusted = []
        if observed_any:
            untrusted.append("data.items[].receipt.observed")
        if result_any:
            untrusted.append("data.items[].result")
        n = len(entries)
        summary = {"total": n, "succeeded": n, "failed": 0, "unknown": 0, "skipped": 0}
        data = {"operation_id": origin, "items": entries, "summary": summary}
        return data, untrusted, src, copies

    def replay_batch(
        self,
        j: Journal,
        sc: ScopeState,
        rec: Record,
        item_recs: list[Record],
        prev: Record,
        now: _dt.datetime,
    ) -> None:
        """Answer a repeated batch at confirm (A5): nothing is sent, nothing is
        counted, and the new batch and its items are recorded as replays of the
        originals."""
        data, untrusted, src, copies = self._guard(lambda: self.batch_replay_of(j, prev))
        self._guard(lambda: self.consume_counted(j, sc, ScopeNeed(), rec.operation_id, 0, now))
        src_items = src.items or []
        for i, ir in enumerate(item_recs):
            if i >= len(src_items):
                break
            ir.status, ir.finished_at, ir.replay_of = "applied", time_string(now), src_items[i]
            ir.receipt, ir.result = copies[i]
            try:
                j.write(ir)
            except Error:
                self.a.notice(journal_notice())
        rec.status, rec.finished_at = "applied", time_string(now)
        rec.replay_of = data["operation_id"]
        try:
            j.write(rec)
        except Error:
            self.a.notice(journal_notice())
        self.succeed(data, untrusted)

    def run_batch(
        self,
        j: Journal,
        rec: Record,
        items: list[BatchItem],
        item_recs: list[Record],
        timeout: float,
        continue_on_error: bool,
        scope_id: str | None = None,
    ) -> None:
        """Apply the items in order and report the batch (CLI-063). Each item's
        upstream idempotency key is <key>/<first 16 hex of sha256(target key)>,
        stable across dry-runs (CLI-062); items never dispatched give their budget
        back (A4)."""
        m = self.m
        results: list[Any] = []
        first_fail: WriteOutcome | None = None
        succeeded = failed = unknown_n = skipped = never_sent = 0
        any_applied = any_dispatched = stop = observed_any = False
        result_untrusted: list[str] = []
        try:
            for it, ir in zip(items, item_recs, strict=False):
                now = self.a._now()
                if stop:
                    o = sanitize_outcome(
                        Outcome(
                            execution="not_started",
                            change=pick(
                                m.has_effect("change", "configure"), "not_applied", "not_applicable"
                            ),
                            delivery=pick(
                                m.has_effect("deliver"), "not_attempted", "not_applicable"
                            ),
                            persistence=pick(
                                m.has_effect("artifact", "persist"), "unchanged", "not_applicable"
                            ),
                            verification="not_applicable",
                        )
                    )
                    receipt = self.skipped_receipt(ir.operation_id, it.target, o)
                    ir.status = "not_applied"
                    ir.finished_at = time_string(now)
                    ir.receipt = to_generic(receipt)
                    try:
                        j.write(ir)
                    except Error:
                        self.a.notice(journal_notice())
                    results.append(
                        {
                            "target": it.target.key,
                            "operation_id": ir.operation_id,
                            "ok": False,
                            "receipt": receipt,
                        }
                    )
                    skipped += 1
                    never_sent += 1
                    continue
                ir.status = "dispatching"
                ir.dispatched_at = time_string(now)
                ir.lease_until = time_string(now + _dt.timedelta(seconds=timeout + LEASE_GRACE))
                try:
                    j.write(ir)
                except Error as e:
                    out = WriteOutcome(ok=False, code=e.code, cause=e, dispatch="not_started")
                    results.append(
                        {
                            "target": it.target.key,
                            "operation_id": ir.operation_id,
                            "ok": False,
                            "error": self.item_error(out),
                        }
                    )
                    failed += 1
                    never_sent += 1
                    if first_fail is None:
                        first_fail = out
                    stop = True
                    continue
                res, apply_err = self.call_apply(
                    it.apply, Op(ir.operation_id, self.item_key(it.target.key), timeout)
                )
                stored = ir.preview if isinstance(ir.preview, dict) else {}
                out = self.classify(ir.operation_id, it.target, stored, res, apply_err)
                self.finish_record(j, ir, out)
                if out.resolved_meanwhile:
                    # resolved by hand while apply still ran: the item's outcome is unknown (A6)
                    out.ok, out.code, out.cause, out.dispatch = (
                        False,
                        "E_OUTCOME_UNKNOWN",
                        None,
                        "unknown",
                    )
                if out.dispatch != "not_started":
                    any_dispatched = True
                entry: dict[str, Any] = {
                    "target": it.target.key,
                    "operation_id": ir.operation_id,
                    "ok": out.ok,
                    "receipt": out.receipt,
                }
                if out.observed:
                    observed_any = True
                o = out.outcome
                if (
                    o.change in ("applied", "partial")
                    or o.delivery in ("delivered", "partial")
                    or o.persistence in ("saved", "partial")
                ):
                    any_applied = True
                if out.ok:
                    succeeded += 1
                    if res.data is not None:
                        entry["result"] = (
                            out.result_data
                            if out.unencodable
                            else self.project_result(out.result_data)
                        )
                        if out.unencodable:
                            self.unencodable_notice(ir.operation_id)
                        else:
                            result_untrusted.extend(
                                prefix_paths("data.items[].result", untrusted_paths(res.untrusted))
                            )
                elif out.code == "E_OUTCOME_UNKNOWN":
                    unknown_n += 1
                    entry["error"] = self.item_error(out)
                else:
                    failed += 1
                    entry["error"] = self.item_error(out)
                    if first_fail is None:
                        first_fail = out
                results.append(entry)
                if not out.ok and not continue_on_error:
                    stop = True
        finally:
            # items never dispatched (skipped after a stop) give their unit back (A4)
            self.refund_budget(j, scope_id, never_sent)
        summary = {
            "total": len(items),
            "succeeded": succeeded,
            "failed": failed,
            "unknown": unknown_n,
            "skipped": skipped,
        }
        if unknown_n > 0:
            status = "unknown"
        elif failed + skipped > 0 and any_applied:
            status = "partial"
        elif failed + skipped > 0:
            status = "not_applied"
        else:
            status = "applied"
        try:
            unlock = j.lock(rec.operation_id)
        except Error:
            self.a.notice(journal_notice())
        else:
            try:
                rec.status = status
                rec.finished_at = time_string(self.a._now())
                rec.lease_until = None
                try:
                    j.write(rec)
                except Error:
                    self.a.notice(journal_notice())
            finally:
                unlock()
        untrusted = list(result_untrusted)
        if observed_any:
            untrusted.append("data.items[].receipt.observed")
        if status == "applied":
            self.succeed(
                {"operation_id": rec.operation_id, "items": results, "summary": summary}, untrusted
            )
            return
        lookup = f"{self.tool()} operation show --id {rec.operation_id}"
        if unknown_n > 0:
            e = new_error(
                "E_OUTCOME_UNKNOWN",
                "Some items of the batch may have been applied; nothing will be retried. Look the "
                "operation up first.",
                {
                    "lookup": lookup,
                    "dispatch_state": "unknown",
                    "next_action": (
                        f"Run `{lookup}` and check the items marked unknown before any retry; "
                        "resolve each with "
                        f"`{self.tool()} operation resolve`."
                    ),
                },
            )
        elif any_applied:
            e = new_error(
                "E_PARTIAL",
                "Part of the batch took effect; details.items shows each item.",
                {
                    "dispatch_state": "dispatched",
                    "next_action": "Read details.items; retry only the failed items, never the "
                    "whole batch.",
                },
            )
        else:
            code = first_fail.code if first_fail is not None else "E_SERVER"
            if (
                first_fail is not None
                and first_fail.cause is not None
                and first_fail.cause.code == code
            ):
                e = first_fail.cause.copy()
                e.message = "No item of the batch was applied: " + e.message.removesuffix(".") + "."
            else:
                e = new_error(code, "No item of the batch was applied.")
            e.details["dispatch_state"] = "dispatched" if any_dispatched else "not_started"
            e.details["next_action"] = (
                "Read details.items, fix the cause, then run the command again with --dry-run."
            )
        e.details["operation_id"] = rec.operation_id
        e.details["items"] = results
        e.details["summary"] = summary
        if observed_any:
            e.untrusted = e.untrusted + ["items[].receipt.observed"]
        raise self.fail(e)

    def item_key(self, target_key: str) -> str:
        """A batch item's upstream idempotency key (CLI-062): <key>/<first 16 hex
        characters of sha256(item target key)>, the same for every dry-run of the
        batch and distinct per target. "" without --idempotency-key."""
        if not self.idem:
            return ""
        return self.idem + "/" + sha256_hex(raw_bytes(target_key))[:16]

    def skipped_receipt(self, op_id: str, t: Target, o: Outcome) -> dict[str, Any]:
        return {
            "operation_id": op_id,
            "command": self.path,
            "target": target_object(t),
            "effects": tuple(self.m.effect_strings()),
            "outcome": {
                "execution": o.execution,
                "change": o.change,
                "delivery": o.delivery,
                "persistence": o.persistence,
                "verification": o.verification,
            },
            "observed": {},
            "mismatched": (),
            "replayed": False,
        }

    def item_error(self, out: WriteOutcome) -> dict[str, Any]:
        """The compact per-item error of a batch result."""
        spec = self.a.lookup_code(out.code)
        msg = spec.meaning if spec is not None else ""
        if out.cause is not None and out.cause.code == out.code and out.cause.message.strip():
            msg = out.cause.message
        return {
            "code": out.code,
            "retryable": bool(spec and spec.retryable),
            "message": truncate_runes(msg, 160),
        }


class GateMixin:
    def _start_gate(self: App, cmd: Any, batch: bool) -> GateRun:
        st, node = self._run_for(cmd)
        g = GateRun(self, st, node)
        st.write_calls += 1
        if st.write_calls > 1:
            raise g.fail(internal_error("A command can pass the write gate once per run."))
        m = self.meta_for(node)
        if m is None or not m.is_write():
            raise g.fail(
                internal_error(
                    "This command is not registered as a write (Meta.effects), so the gate refuses "
                    "it."
                )
            )
        g.m = m
        if batch != bool(m.batch_flag):
            raise g.fail(
                internal_error(
                    "Commands with Meta.batch_flag use App.write_batch; others use App.write."
                )
            )
        root_flags = self._root.persistent
        for name in EXCLUDED_FROM_BINDING:
            if name == "help":
                continue
            f = node.flags().get(name)
            if f is not None and f is not root_flags.get(name):
                raise g.fail(
                    internal_error(
                        f"The command defines --{name}, which shadows a global control flag; the "
                        "gate refuses it."
                    )
                )
        g.dry_run = bool(node.flag("dry-run") and node.flag("dry-run").value)
        g.dangerous_flag = bool(node.flag("dangerous") and node.flag("dangerous").value)
        confirm = node.flag("confirm")
        g.confirm = confirm.string() if confirm is not None else ""
        g.confirm_set = bool(confirm is not None and confirm.changed)
        idem = node.flag("idempotency-key")
        if idem is not None and idem.changed:
            g.idem = claim_string(idem.string())
        if g.dry_run and g.confirm_set:
            e = usage(
                "conflicting_flags",
                "--confirm",
                "--dry-run and --confirm cannot be combined; nothing was changed.",
            )
            e.details["next_action"] = (
                "Drop --dry-run: run the same command with --confirm <token> only (or drop "
                "--confirm to preview again)."
            )
            raise g.fail(e)
        return g

    def write(self: App, cmd: Any, spec: WriteSpec) -> None:
        """Run a single-target write through the gate. Returns on success; raises
        the gate's Error otherwise (the error is already the run's outcome)."""
        g = self._start_gate(cmd, False)
        if spec.preview is None or spec.apply is None or not spec.target.key.strip():
            raise g.fail(internal_error("App.write needs WriteSpec.target.key, preview and apply."))
        e = check_target_key(spec.target.key)
        if e is not None:
            raise g.fail(e)
        if g.confirm_set:
            g.confirm_single(spec)
        else:
            g.plan_single(spec)

    def write_batch(self: App, cmd: Any, spec: BatchSpec) -> None:
        """Run a batch write through the gate (CLI-063): one preview listing every
        target with per-item operation ids, one token for the batch, per-item
        receipts and a summary. The batch is ok only when every item succeeded;
        otherwise E_OUTCOME_UNKNOWN if any item is unknown, else E_PARTIAL if any
        item applied, else the first failed item's code. Items are de-duplicated
        by target key, order kept."""
        g = self._start_gate(cmd, True)
        items: list[BatchItem] = []
        seen: set[str] = set()
        for it in spec.items:
            if it.preview is None or it.apply is None or not it.target.key.strip():
                raise g.fail(internal_error("Every BatchItem needs target.key, preview and apply."))
            e = check_target_key(it.target.key)
            if e is not None:
                raise g.fail(e)
            if it.target.key in seen:
                continue
            seen.add(it.target.key)
            items.append(it)
        if not items:
            e = usage(
                "missing_parameter",
                "--" + g.m.batch_flag,
                "The batch names no targets; nothing was changed.",
            )
            e.details["next_action"] = f"Give at least one target with --{g.m.batch_flag}."
            raise g.fail(e)
        if g.confirm_set:
            g.confirm_batch(spec, items)
        else:
            g.plan_batch(items)
