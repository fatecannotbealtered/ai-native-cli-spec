# Vendored from ai-native-cli-spec template/python/aicli.
# Do not edit in tool repos; run scripts/sync-spec.js.
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sean Guo

"""Contract errors (CLI-010 .. CLI-018): the Error type, its constructors, the
upstream HTTP mapping and the normalisation that turns an Error into the error
object of the envelope."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ._util import Ordered, truncate_runes
from .contract_gen import (
    CONFLICT_VALUES,
    CORE_CODES,
    DISPATCH_STATES,
    EXIT_CODE_MEANINGS,
    HTTP_STATUS_CONFLICTS,
    HTTP_STATUS_MAP,
    HUMAN_ACTION_VALUES,
    PROBLEM_VALUES,
)

if TYPE_CHECKING:
    from .app import App


class Error(Exception):
    """The error a command raises to report a contract error (CLI-010). The
    library turns it into the error envelope: it adds retryable and the exit
    code from the contract table, and fills details.dispatch_state
    ("not_started" unless set) and details.next_action (a default per code
    unless set). untrusted lists paths inside details that carry external
    content, relative to error.details (for example "upstream_message")."""

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        untrusted: list[str] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details: dict[str, Any] = dict(details or {})
        self.untrusted: list[str] = list(untrusted or [])
        self.cause = cause

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def copy(self) -> Error:
        """A shallow copy whose details and untrusted paths can be changed."""
        return Error(
            self.code,
            self.message,
            dict(self.details),
            untrusted=list(self.untrusted),
            cause=self.cause,
        )


@dataclass(frozen=True)
class Candidate:
    """One match of an ambiguous name (E_AMBIGUOUS)."""

    ref: str
    label: str = ""


@dataclass(frozen=True)
class CodeSpec:
    """An extension error code from the tool's contract-ext.json (CLI-018)."""

    exit: int
    retryable: bool
    meaning: str = ""


def new_error(code: str, message: str, details: dict[str, Any] | None = None) -> Error:
    """An error with the given code and details (copied)."""
    return Error(code, message, dict(details or {}))


def usage(problem: str, parameter: str, message: str) -> Error:
    """A command-line mistake (E_USAGE, exit 2). problem is one of the
    contract's problem values; parameter names the flag (with dashes) or
    argument."""
    e = new_error("E_USAGE", message, {"problem": problem})
    if parameter:
        e.details["parameter"] = parameter
    return e


def validation(parameter: str, expected: str, message: str) -> Error:
    """An input the tool or upstream rejected (E_VALIDATION, exit 2)."""
    e = new_error("E_VALIDATION", message, {"problem": "invalid_value"})
    if parameter:
        e.details["parameter"] = parameter
    if expected:
        e.details["expected"] = expected
    return e


def not_found(message: str, next_action: str = "") -> Error:
    """A missing resource (E_NOT_FOUND, exit 3)."""
    e = new_error("E_NOT_FOUND", message)
    if next_action:
        e.details["next_action"] = next_action
    return e


def ambiguous(message: str, candidates: list[Candidate]) -> Error:
    """A name that matched several resources (E_AMBIGUOUS, exit 2). Each
    candidate is {ref, label} (CLI-048); a candidate without a label shows its
    ref. Labels are external text and are declared untrusted. A name that matched
    fewer than two resources is not ambiguous: that is a bug in the tool and is
    reported as E_UNKNOWN."""
    items = [
        {"ref": c.ref, "label": c.label if (c.label or "").strip() else c.ref}
        for c in candidates
        if (c.ref or "").strip()
    ]
    if len(items) < 2:
        return internal_error(
            "Ambiguous needs at least two candidates with a ref; a single match is not ambiguous."
        )
    e = new_error("E_AMBIGUOUS", message, {"candidates": items})
    e.details["next_action"] = (
        "Pick one of details.candidates (ask the user if unsure) and run the command again "
        "with its ref."
    )
    e.untrusted = ["candidates[].label"]
    return e


def forbidden(message: str, details: dict[str, Any] | None = None) -> Error:
    """A refused action (E_FORBIDDEN, exit 4). Upstream refusals may add
    details.required (the role or permission needed); local scope refusals are
    built by the gate and carry details.scope."""
    return new_error("E_FORBIDDEN", message, details)


def human_required(action: str, resume: str, message: str) -> Error:
    """A person must act first (E_HUMAN_REQUIRED, exit 9). action is one of
    sign_in, approve, solve_challenge, scan_qr, install_component, other;
    resume is the exact command to run afterwards."""
    return new_error(
        "E_HUMAN_REQUIRED",
        message,
        {
            "action": action,
            "resume": resume,
            "next_action": "Ask the user to complete details.action, then run details.resume.",
        },
    )


def from_http(status: int, message: str, upstream: str = "", retry_after: float = 0.0) -> Error:
    """Map an upstream HTTP status to a contract error by status only, never by
    message text (CLI-016). status 0 means the connection failed before
    anything was sent (E_NETWORK). upstream is the upstream's own error text: it
    goes to details.upstream_message, marked untrusted, never into the message.
    retry_after (seconds) > 0 becomes details.retry_after_ms on 429.

    dispatch_state is not_started: a read changes nothing, and a write's apply
    step reports what was sent through the gate (decision 1). During a write's
    apply step, wrap the result with rejected() only when the upstream
    definitively refused the request; any other error is an unknown outcome."""
    code = http_code(status)
    e = new_error(code, message)
    if status > 0:
        e.details["http_status"] = status
    e.details["dispatch_state"] = "not_started"
    if code == "E_VALIDATION":
        e.details["problem"] = "input_too_large" if status == 413 else "invalid_value"
    elif code == "E_CONFLICT":
        e.details["conflict"] = HTTP_STATUS_CONFLICTS.get(str(status)) or "upstream_conflict"
        e.details["next_action"] = (
            "The upstream refused because of its current state; re-read the target, then run "
            "the command again with --dry-run."
        )
    elif code == "E_RATE_LIMITED":
        ms = int(retry_after * 1000) if retry_after else 0
        if ms > 0:
            e.details["retry_after_ms"] = ms
    if upstream.strip():
        e.details["upstream_message"] = truncate_runes(upstream, 500)
        e.untrusted.append("upstream_message")
    return e


def http_code(status: int) -> str:
    """The code for a status, using the contract's http_status_map."""
    if status <= 0:
        return HTTP_STATUS_MAP["connection_failure_before_send"]
    if str(status) in HTTP_STATUS_MAP:
        return HTTP_STATUS_MAP[str(status)]
    if 500 <= status <= 599:
        return HTTP_STATUS_MAP["5xx"]
    if status == 410:
        return "E_NOT_FOUND"
    if 400 <= status <= 499:
        return "E_VALIDATION"
    return "E_UNKNOWN"


def find_error(exc: BaseException | None) -> Error | None:
    """The first Error in exc, its wrapped error (.err) or its explicit cause
    chain (raise X from Y), like Go's errors.As."""
    seen = 0
    while exc is not None and seen < 32:
        if isinstance(exc, Error):
            return exc
        inner = getattr(exc, "err", None)
        exc = inner if isinstance(inner, BaseException) else exc.__cause__
        seen += 1
    return None


def as_error(exc: BaseException) -> Error:
    """Any exception as an Error. Unknown exceptions become E_UNKNOWN with the
    exception text in details.cause (marked untrusted: it may carry upstream
    text). A read that times out or is interrupted changed nothing (decision 1);
    a write's apply errors never come here, the gate classifies them."""
    e = find_error(exc)
    if e is not None:
        return e
    if isinstance(exc, TimeoutError):
        return new_error(
            "E_TIMEOUT",
            "The command ran out of time before the upstream answered.",
            {
                "dispatch_state": "not_started",
                "next_action": "Retry with a narrower request; nothing was changed by a read.",
            },
        )
    if isinstance(exc, KeyboardInterrupt):
        return new_error(
            "E_INTERRUPTED",
            "The command was interrupted.",
            {
                "dispatch_state": "not_started",
                "next_action": "Run the command again if it is still needed.",
            },
        )
    text = str(exc) or type(exc).__name__
    e = new_error(
        "E_UNKNOWN",
        "The command failed with an internal error; details.cause has the error text.",
        {"cause": truncate_runes(text, 300)},
    )
    e.untrusted = ["cause"]
    e.cause = exc
    return e


def list_len(v: Any) -> int:
    """The length of a JSON-array-like value, or -1 when v is not a list."""
    if v is None:
        return -1
    if isinstance(v, (list, tuple)):
        return len(v)
    try:
        from ._util import to_generic

        g = to_generic(v)
    except (TypeError, ValueError):
        return -1
    return len(g) if isinstance(g, list) else -1


def internal_error(message: str) -> Error:
    """A library or tool programming error as E_UNKNOWN."""
    return new_error(
        "E_UNKNOWN",
        message,
        {"next_action": "This is a bug in the tool; report it with the command you ran."},
    )


def io_error(message: str, next_action: str = "", cause: BaseException | None = None) -> Error:
    """A local file or state store failure (E_IO); nothing was sent."""
    e = new_error("E_IO", message, {"dispatch_state": "not_started"})
    if next_action:
        e.details["next_action"] = next_action
    e.cause = cause
    return e


def conflict_error(conflict: str, message: str, next_action: str) -> Error:
    """An E_CONFLICT with the given details.conflict value."""
    return new_error(
        "E_CONFLICT",
        message,
        {"conflict": conflict, "dispatch_state": "not_started", "next_action": next_action},
    )


CODE_PATTERN = re.compile(r"E_[A-Z0-9][A-Z0-9_]*")


class ErrorsMixin:
    """App methods for codes, default next actions and error normalisation."""

    def lookup_code(self: App, code: str) -> CodeSpec | None:
        """A code from the core table, then from the tool's extensions."""
        row = CORE_CODES.get(code)
        if row is not None:
            return CodeSpec(row.exit, row.retryable, row.meaning)
        spec = (self.ext_codes or {}).get(code)
        if spec is not None and CODE_PATTERN.fullmatch(code):
            return spec
        return None

    def check_ext_codes(self: App) -> list[str]:
        """Problems with the tool's extension codes (CLI-018)."""
        problems = []
        allowed = set(EXIT_CODE_MEANINGS)
        for name in sorted(self.ext_codes or {}):
            spec = self.ext_codes[name]
            if not CODE_PATTERN.fullmatch(name):
                problems.append(f'extension code "{name}" does not match ^E_[A-Z0-9][A-Z0-9_]*$')
            elif name in CORE_CODES:
                problems.append(f'extension code "{name}" shadows a core code')
            elif spec.exit not in allowed or spec.exit == 0:
                problems.append(
                    f'extension code "{name}" uses exit {spec.exit}, which is not a documented '
                    "failure exit code"
                )
            elif not spec.meaning.strip():
                problems.append(f'extension code "{name}" has no meaning')
        return problems

    def default_next_action(self: App, code: str, details: dict[str, Any]) -> str:
        """The next step used when an error does not name one. A usage or validation
        error that names its parameter says what to give it, from the details the
        agent already has, instead of sending it to --help."""
        tool = self.tool_name()
        path = tool
        st = self._run
        if st is not None and st.cmd is not None:
            path = st.cmd.command_path
        param = details.get("parameter")
        param = param.strip() if isinstance(param, str) else ""
        if code in ("E_USAGE", "E_VALIDATION") and param and "expected" in details:
            return f"Give {param} a value from details.expected and run the command again."
        if code == "E_USAGE" and param and details.get("problem") == "missing_parameter":
            if "usage" in details:
                return f"Add {param} (details.usage shows the form) and run the command again."
            return f"Add {param} and run the command again."
        defaults = {
            "E_USAGE": f"Run `{path} --help` for its arguments and flags.",
            "E_VALIDATION": "Fix the value named in details.parameter and run the command again.",
            "E_AMBIGUOUS": "Pick one of details.candidates and run the command again with its ref.",
            "E_UNSUPPORTED": "Do not retry this request; use a supported alternative.",
            "E_NOT_FOUND": "Check the identifier; list the resources to find the right one.",
            "E_AUTH": (
                "Ask the user to configure or refresh the credentials; "
                f"`{tool} doctor` shows what is missing."
            ),
            "E_FORBIDDEN": (
                "Stop and tell the user which permission is missing; do not retry with other "
                "credentials."
            ),
            "E_CONFIG": f"Run `{tool} doctor` and fix the reported configuration.",
            "E_CONFIRMATION_REQUIRED": (
                "Review the preview, then run the same command again with --confirm <token>."
            ),
            "E_CONFLICT": "Re-read the current state, then run the command again with --dry-run.",
            "E_OUTCOME_UNKNOWN": (
                f"Run `{tool} operation list --status unknown` and look the operation up before "
                "doing anything else."
            ),
            "E_NETWORK": "Check connectivity to the upstream, then retry.",
            "E_RATE_LIMITED": (
                "Wait details.retry_after_ms (or a few seconds when absent), then retry."
            ),
            "E_SERVER": (
                "Retry later; if it keeps failing, report the upstream failure to the user."
            ),
            "E_TIMEOUT": "Retry with a narrower request or a longer timeout.",
            "E_HUMAN_REQUIRED": "Ask the user to complete details.action, then run details.resume.",
            "E_PARTIAL": (
                "Read details.receipt or details.items to see what took effect; never resend the "
                "whole request."
            ),
            "E_VERIFY_FAILED": (
                "Read details.receipt.mismatched and re-read the target before changing it again."
            ),
            "E_VERIFY_INCOMPLETE": "Re-read the target to check the change before relying on it.",
            "E_INTEGRITY": "Do not retry; tell the user the integrity check failed.",
            "E_IO": "Check the local file or the tool's state directory named in the message.",
            "E_INTERRUPTED": "Run the command again if it is still needed.",
        }
        return defaults.get(code, "Read message and details; report the error if it persists.")

    def normalize_error(self: App, e: Error | None) -> tuple[Ordered, int, list[str]]:
        """The error object, exit code and untrusted paths (rooted at the
        envelope) for e. Unknown codes become E_UNKNOWN so the code, exit code
        and retryable always agree with the contract table."""
        if e is None:
            e = internal_error("A nil error was returned.")
        code = e.code
        details = dict(e.details)
        spec = self.lookup_code(code)
        if spec is None:
            details["undeclared_code"] = code
            code = "E_UNKNOWN"
            spec = self.lookup_code(code)
            assert spec is not None
        if code == "E_AMBIGUOUS" and list_len(details.get("candidates")) < 2:
            # An ambiguity without two candidates cannot be acted on: a tool bug.
            details["undeclared_code"] = code
            details.pop("candidates", None)
            code = "E_UNKNOWN"
            spec = self.lookup_code(code)
            assert spec is not None
        ds = details.get("dispatch_state")
        if not isinstance(ds, str) or ds not in DISPATCH_STATES:
            details["dispatch_state"] = "not_started"
        # dispatch_state is fixed for some codes (CLI-011, CLI-015, error-details schema).
        if code in ("E_USAGE", "E_CONFIRMATION_REQUIRED", "E_NETWORK"):
            details["dispatch_state"] = "not_started"
        elif code == "E_OUTCOME_UNKNOWN":
            if details["dispatch_state"] == "not_started":
                details["dispatch_state"] = "unknown"
        elif code in ("E_PARTIAL", "E_VERIFY_FAILED", "E_VERIFY_INCOMPLETE"):
            details["dispatch_state"] = "dispatched"
        na = details.get("next_action")
        if not isinstance(na, str) or not na.strip():
            details["next_action"] = self.default_next_action(code, details)
        # An empty suggestion list helps nobody and the schema forbids it (minItems 1).
        if "did_you_mean" in details and list_len(details["did_you_mean"]) == 0:
            del details["did_you_mean"]
        if code in ("E_USAGE", "E_VALIDATION"):
            p = details.get("problem")
            if not isinstance(p, str) or p not in PROBLEM_VALUES:
                details["problem"] = "invalid_value"
        elif code == "E_CONFLICT":
            c = details.get("conflict")
            if not isinstance(c, str) or c not in CONFLICT_VALUES:
                details["conflict"] = "upstream_conflict"
        elif code == "E_HUMAN_REQUIRED":
            act = details.get("action")
            if not isinstance(act, str) or act not in HUMAN_ACTION_VALUES:
                details["action"] = "other"
            r = details.get("resume")
            if not isinstance(r, str) or not r.strip():
                st = self._run
                details["resume"] = (
                    st.cmd.command_path
                    if st is not None and st.cmd is not None
                    else self.tool_name()
                )
        message = (e.message or "").strip() or spec.meaning
        untrusted = []
        for p in e.untrusted:
            p = p.removeprefix("error.details.")
            if p:
                untrusted.append("error.details." + p)
        obj = Ordered()
        obj["code"] = code
        obj["message"] = message
        obj["retryable"] = spec.retryable
        obj["details"] = details
        return obj, spec.exit, untrusted
