# Vendored from ai-native-cli-spec template/python/aicli.
# Do not edit in tool repos; run scripts/sync-spec.js.
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sean Guo

"""The conformance suite (VER-001, BLUEPRINT 6): G00-G10 for every write command
and R01-R06 for the tool, run against a Harness. From pytest:

    def test_conformance():
        aicli.run_conformance(harness())   # raises AssertionError listing every failure
"""

from __future__ import annotations

import datetime as _dt
import decimal
import io
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import click

from ._tree import Flag, Node
from ._util import go_bool_string, parse_go_bool, time_string, truncate_runes
from .binding import label_for
from .contract_gen import (
    COMMAND_BUDGET_CHARS,
    EXCLUDED_FROM_BINDING,
    INDEX_BUDGET_CHARS,
    WRITE_EFFECTS,
)
from .keystore import KEY_SIZE
from .registry import flag_type, is_global
from .scope import validate_scope_file
from .token import parse_token
from .validate import (
    ContractViolation,
    validate_changelog,
    validate_context,
    validate_doctor,
    validate_envelope,
    validate_operation_view,
    validate_reference_command,
    validate_reference_full,
    validate_reference_index,
    validate_stream,
)

if TYPE_CHECKING:
    from .app import App
    from .registry import Meta


class TB(Protocol):
    """What the suite reports through (a pytest-free testing.TB)."""

    def error(self, msg: str) -> None: ...

    def fatal(self, msg: str) -> None: ...

    def log(self, msg: str) -> None: ...


class Recorder:
    """A TB that collects the reports."""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.fatals: list[str] = []
        self.logs: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def fatal(self, msg: str) -> None:
        self.fatals.append(msg)

    def log(self, msg: str) -> None:
        self.logs.append(msg)

    def has(self, prefix: str) -> bool:
        return any(e.startswith(prefix) for e in self.errors + self.fatals)

    def raise_if_failed(self, what: str = "aicli conformance") -> None:
        problems = self.fatals + self.errors
        if problems:
            raise AssertionError(
                f"{what} failed ({len(problems)}):\n" + "\n".join("  " + p for p in problems)
            )


@dataclass
class Harness:
    """Connects the conformance suite to a tool."""

    # a fresh App and command tree for every call; env holds the variables the
    # suite sets (state dir, scope file), and the suite also overlays them on App.env
    new_app: Callable[[dict[str, str]], App]
    # the number of write requests the fake upstream has seen so far
    writes: Callable[[], int]
    # restores the fake upstream's seed state between checks
    reset: Callable[[], None] | None = None
    # each write command path ("note create") -> the arguments of a valid write,
    # without the command path and without gate flags
    samples: dict[str, list[str]] = field(default_factory=dict)
    # the scope granted for positive checks (written to a temp file); None grants
    # every effect, dangerous included, for an hour
    scope: dict[str, Any] | None = None
    # enables R06: the file the tool's own tests recorded executed commands into
    # (set <PREFIX>_COVERAGE_FILE to it while those tests run)
    coverage_file: str = ""


def run_conformance(h: Harness, tb: TB | None = None) -> Recorder | None:
    """Run the conformance checks against a tool: G00-G10 for every write command
    and R01-R05 (R06 with Harness.coverage_file). Every failure is reported with
    its check ID; without tb, an AssertionError lists them all."""
    rec = Recorder() if tb is None else None
    t: TB = tb if tb is not None else rec  # type: ignore[assignment]
    if h.new_app is None or h.writes is None:
        t.fatal("aicli conformance: Harness.new_app and Harness.writes are required")
    else:
        try:
            s = _Suite(t, h)
        except ValueError as exc:
            t.fatal(f"aicli conformance: {exc}")
        else:
            try:
                s.run_all()
            finally:
                s.cleanup()
    if rec is not None:
        rec.raise_if_failed()
    return rec


def check_coverage(h: Harness, tb: TB | None = None) -> None:
    """Run only R06 (VER-003): every registry command must appear in
    Harness.coverage_file. Call it after all of the tool's own tests ran."""
    rec = Recorder() if tb is None else None
    t: TB = tb if tb is not None else rec  # type: ignore[assignment]
    if h.new_app is None or not h.coverage_file:
        t.fatal("aicli coverage: Harness.new_app and Harness.coverage_file are required")
    else:
        if h.writes is None:
            h.writes = lambda: 0
        try:
            s = _Suite(t, h)
        except ValueError as exc:
            t.fatal(f"aicli coverage: {exc}")
        else:
            try:
                s.check_coverage()
            finally:
                s.cleanup()
    if rec is not None:
        rec.raise_if_failed("aicli coverage")


@dataclass
class _State:
    dir: str
    scope_file: str
    scope_on: bool


@dataclass
class _Run:
    label: str
    exit: int
    stdout: bytes
    stderr: bytes
    env: dict[str, Any] | None
    writes: int
    defects: list[str] = field(default_factory=list)  # tool bugs the library corrected

    def code(self) -> str:
        e = (self.env or {}).get("error")
        return e.get("code", "") if isinstance(e, dict) else ""

    def details(self) -> dict[str, Any]:
        e = (self.env or {}).get("error")
        d = e.get("details") if isinstance(e, dict) else None
        return d if isinstance(d, dict) else {}

    def data(self) -> dict[str, Any]:
        d = (self.env or {}).get("data")
        return d if isinstance(d, dict) else {}

    def describe(self) -> str:
        out = (
            self.stdout.decode("utf-8", "replace").strip()
            or self.stderr.decode("utf-8", "replace").strip()
        )
        return f"exit {self.exit}, {truncate_runes(out, 600)}"


@dataclass
class _Arg:
    positional: bool
    name: str = ""
    value: str = ""
    has_value: bool = False
    tokens: list[str] = field(default_factory=list)


_NUMBER = re.compile(r"-?[0-9]+")
_FLOAT = re.compile(r"-?[0-9]+\.[0-9]+")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_COLOUR = re.compile(r"#[0-9A-Fa-f]{6}")
_DATE_TIME = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(\.[0-9]+)?"
    r"(Z|[+-][0-9]{2}:[0-9]{2})"
)


def _is_token(x: Any) -> bool:
    return parse_token(x) is not None


def _list_has(x: Any, want: str) -> bool:
    return isinstance(x, list) and want in x


class _Suite:
    def __init__(self, t: TB, h: Harness) -> None:
        if h.scope is not None:
            try:
                raw = json.dumps(h.scope).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Harness.scope cannot be encoded: {exc}") from None
            reason = validate_scope_file(raw)
            if reason:
                raise ValueError(f"Harness.scope is not a valid scope file: {reason}")
        self.t = t
        self.h = h
        self.root = tempfile.mkdtemp(prefix="aicli-conformance-")
        probe = h.new_app({})
        if probe is None or probe.root is None:
            shutil.rmtree(self.root, ignore_errors=True)
            raise ValueError("Harness.new_app returned no App or no root")
        probe._ensure_tree()
        self.probe = probe
        self.tool = probe.tool_name()
        self.prefix = probe._prefix()
        self.counter = 0
        self.seen: list[tuple[str, bytes, bool]] = []  # label, stdout, an NDJSON stream
        self.outputs: list[bytes] = []

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def errorf(self, check: str, msg: str) -> None:
        self.t.error(f"{check}: {msg}")

    # ---------------------------------------------------------------- state dirs

    def new_state(self, scope_on: bool) -> _State:
        self.counter += 1
        base = os.path.join(self.root, f"s{self.counter:03d}")
        os.makedirs(base, mode=0o700, exist_ok=True)
        st = _State(os.path.join(base, "state"), os.path.join(base, "scope.json"), scope_on)
        self.write_scope(st, None)
        return st

    def scope_object(self) -> dict[str, Any]:
        if self.h.scope is not None:
            return dict(self.h.scope)
        now = _dt.datetime.now(_dt.timezone.utc)
        return {
            "scope_id": "scp_aicli_conformance",
            "tool": self.tool,
            "granted_by": "aicli conformance suite",
            "granted_at": time_string(now - _dt.timedelta(minutes=1)),
            "expires_at": time_string(now + _dt.timedelta(hours=1)),
            "effects": list(WRITE_EFFECTS),
            "dangerous": True,
        }

    def write_scope(self, st: _State, change: dict[str, Any] | None) -> None:
        obj = self.scope_object()
        obj.update(change or {})
        with open(st.scope_file, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(obj, fh, indent=2)
        if os.name != "nt":
            os.chmod(st.scope_file, 0o600)  # only its owner may write a scope file (SEC-011)

    def writes(self) -> int:
        return self.h.writes()

    def reset(self) -> None:
        if self.h.reset is not None:
            self.h.reset()

    # ---------------------------------------------------------------- running

    def run(self, st: _State, label: str, args: list[str], stdin: str = "") -> _Run:
        env = {
            self.prefix + "_STATE_DIR": st.dir,
            self.prefix + "_SCOPE_FILE": st.scope_file if st.scope_on else "",
            self.prefix + "_COVERAGE_FILE": "",
        }
        app = self.h.new_app(dict(env))
        if app is None:
            self.t.fatal("aicli conformance: Harness.new_app returned None")
            return _Run(label, -1, b"", b"", None, 0)
        app.state_dir = st.dir
        app._offline = True  # the suite never uses the network (doctor's update refresh)
        orig = app.env

        def overlay(k: str) -> str:
            if k in env:
                return env[k]
            if orig is not None:
                return orig(k)
            return os.environ.get(k, "")

        app.env = overlay
        out, err = io.BytesIO(), io.BytesIO()
        app.stdout, app.stderr = out, err
        app.stdin = io.BytesIO((stdin or "aicli conformance input\n").encode("utf-8"))
        before = self.writes()
        code = app.execute(list(args))
        r = _Run(
            label,
            code,
            out.getvalue(),
            err.getvalue(),
            None,
            self.writes() - before,
            list(app.defects),
        )
        self.outputs.extend([r.stdout, r.stderr])
        if r.stdout.strip().startswith(b"{"):
            try:
                text = r.stdout.decode("utf-8")
                doc, _ = json.JSONDecoder().raw_decode(text.lstrip())
                r.env = doc if isinstance(doc, dict) else None
            except ValueError:
                pass
            self.seen.append((label + ": " + " ".join(args), r.stdout, is_stream_output(r.stdout)))
        return r

    # ---------------------------------------------------------------- driver

    def run_all(self) -> None:
        for p in self.probe.check_tree():
            self.errorf("R02", p)
        self.check_reference()
        self.check_ux()
        self.check_self_description()
        writes = [
            c
            for c in self.probe.commands()
            if (m := self.probe.meta_for(c)) is not None and m.is_write()
        ]
        paths = [c.path for c in writes]
        for path in self.h.samples:
            if self.probe.command_by_path(path) is None or path not in paths:
                self.errorf("G00", f'Harness.samples names "{path}", which is not a write command')
        for c in writes:
            if c.path not in self.h.samples:
                self.errorf("G00", f'write command "{c.path}" has no sample in Harness.samples')
                continue
            self.check_write(c, strip_path(self.h.samples[c.path], c.path))
        self.check_envelopes()
        self.check_coverage()

    # ---------------------------------------------------------------- R01

    def check_reference(self) -> None:
        st = self.new_state(False)
        r = self.run(st, "R01", ["reference"])
        if r.exit != 0:
            self.errorf("R01", f"reference failed: {r.describe()}")
        else:
            self._valid("R01", "reference", validate_reference_full, r.data())
        r = self.run(st, "R01", ["reference", "--index"])
        if r.exit != 0:
            self.errorf("R01", f"reference --index failed: {r.describe()}")
            return
        if (n := _rune_len(r.stdout)) > INDEX_BUDGET_CHARS:
            self.errorf("R01", f"reference --index is {n} characters, over {INDEX_BUDGET_CHARS}")
        self._valid("R01", "reference --index", validate_reference_index, r.data())
        for g in r.data().get("groups", []) or []:
            path = g.get("path", "") if isinstance(g, dict) else ""
            gr = self.run(st, "R01", ["reference", "--index", path])
            if gr.exit != 0:
                self.errorf("R01", f"reference --index {path} failed: {gr.describe()}")
                continue
            if (n := _rune_len(gr.stdout)) > INDEX_BUDGET_CHARS:
                self.errorf(
                    "R01", f"reference --index {path} is {n} characters, over {INDEX_BUDGET_CHARS}"
                )
        for c in self.probe.commands():
            cr = self.run(st, "R01", ["reference", "--command", c.path])
            if cr.exit != 0:
                self.errorf("R01", f'reference --command "{c.path}" failed: {cr.describe()}')
                continue
            if (n := _rune_len(cr.stdout)) > COMMAND_BUDGET_CHARS:
                self.errorf(
                    "R01",
                    f'reference --command "{c.path}" is {n} characters, over '
                    f"{COMMAND_BUDGET_CHARS}",
                )
            self._valid(
                "R01", f'reference --command "{c.path}"', validate_reference_command, cr.data()
            )

    def _valid(self, check: str, what: str, fn: Callable[[Any], None], data: Any) -> None:
        try:
            fn(data)
        except ContractViolation as exc:
            self.errorf(check, f"{what}: {exc}")

    # ---------------------------------------------------------------- R03

    def check_ux(self) -> None:
        st = self.new_state(False)
        leaf = next(
            (c for c in self.probe.commands() if not c.builtin), None
        ) or self.probe.command_by_path("context")
        assert leaf is not None
        words = leaf.path.split()
        last = words[-1]
        typo = last[:-1] if len(last) > 3 else last + "x"
        args = words[:-1] + [typo]
        r = self.run(st, "R03", args)
        full = self.tool + " " + leaf.path
        if r.exit != 2 or r.code() != "E_USAGE" or r.details().get("problem") != "unknown_command":
            self.errorf(
                "R03",
                f'an unknown command ("{" ".join(args)}") must be E_USAGE unknown_command: '
                f"{r.describe()}",
            )
        elif not _list_has(r.details().get("did_you_mean"), full):
            self.errorf(
                "R03",
                f'unknown command "{" ".join(args)}" should suggest "{full}", got '
                f"{r.details().get('did_you_mean')}",
            )
        r = self.run(st, "R03", [*words, "--aicli-no-such-flag"])
        d = r.details()
        if (
            r.exit != 2
            or r.code() != "E_USAGE"
            or d.get("problem") != "unknown_flag"
            or d.get("parameter") != "--aicli-no-such-flag"
        ):
            self.errorf(
                "R03", f"an unknown flag must be E_USAGE unknown_flag naming it: {r.describe()}"
            )
        r = self.run(st, "R03", ["context", "aicli-unexpected-argument"])
        if (
            r.exit != 2
            or r.code() != "E_USAGE"
            or r.details().get("problem") != "unexpected_argument"
        ):
            self.errorf(
                "R03",
                "an unexpected positional argument must be E_USAGE unexpected_argument: "
                f"{r.describe()}",
            )
        r = self.run(st, "R03", ["context", "--format", "raw"])
        if (
            r.exit != 2
            or r.code() != "E_USAGE"
            or r.details().get("problem") != "unsupported_format"
        ):
            self.errorf(
                "R03",
                "an unsupported --format must be E_USAGE unsupported_format in JSON: "
                f"{r.describe()}",
            )

    # ---------------------------------------------------------------- R05

    def check_self_description(self) -> None:
        st = self.new_state(False)
        r = self.run(st, "R05", ["context"])
        if r.exit != 0:
            self.errorf("R05", f"context failed: {r.describe()}")
        else:
            self._valid("R05", "context", validate_context, r.data())
        r = self.run(st, "R05", ["doctor"])
        if r.exit != 0:
            self.errorf("R05", f"doctor failed: {r.describe()}")
        else:
            self._valid("R05", "doctor", validate_doctor, r.data())
        r = self.run(st, "R05", ["operation", "list"])
        if r.exit != 0:
            self.errorf("R05", f"operation list on an empty journal failed: {r.describe()}")
        elif not isinstance(r.data().get("items"), list) or r.data()["items"]:
            self.errorf(
                "R05", f"operation list on an empty journal must return no items: {r.describe()}"
            )
        r = self.run(st, "R05", ["changelog"])
        if r.exit != 0:
            self.errorf("R05", f"changelog failed: {r.describe()}")
        else:
            self._valid("R05", "changelog", validate_changelog, r.data())

    # ---------------------------------------------------------------- writes

    def check_write(self, c: Node, sample: list[str]) -> None:
        path = c.path
        m = self.probe.meta_for(c)
        assert m is not None
        words = path.split()
        base = [*words, *sample]

        def gate(*extra: str) -> list[str]:
            out = [*base, *extra]
            if m.is_dangerous():
                out.append("--dangerous")
            return out

        self.reset()
        st = self.new_state(True)

        # G01: a bare call is refused with the preview and a token.
        r = self.run(st, "G01", gate())
        d = r.details()
        if r.writes != 0:
            self.errorf("G01", f"{path}: a bare call sent {r.writes} write(s)")
        elif (
            r.exit != 5
            or r.code() != "E_CONFIRMATION_REQUIRED"
            or d.get("problem") != "confirmation_required"
        ):
            self.errorf(
                "G01",
                f"{path}: a bare call must be exit 5 E_CONFIRMATION_REQUIRED "
                f"confirmation_required: {r.describe()}",
            )
        elif (
            d.get("preview") is None
            or d.get("operation_id") is None
            or not _is_token(d.get("confirm_token"))
        ):
            p = d.get("preview")
            if isinstance(p, dict) and p.get("can_apply") is False:
                self.errorf(
                    "G01",
                    f"{path}: the sample's preview cannot apply (blocked_by "
                    f"{p.get('blocked_by')}); fix the sample or the fake upstream",
                )
            else:
                self.errorf(
                    "G01",
                    f"{path}: the refusal must carry preview, confirm_token and operation_id: "
                    f"{r.describe()}",
                )

        # G02: dry-run returns the preview and a canonical token; nothing is sent.
        r = self.run(st, "G02", gate("--dry-run"))
        token = r.data().get("confirm_token")
        if r.writes != 0:
            self.errorf("G02", f"{path}: --dry-run sent {r.writes} write(s)")
        elif r.exit != 0 or not _is_token(token) or r.data().get("preview") is None:
            self.errorf(
                "G02",
                f"{path}: --dry-run must return exit 0 with a preview and a canonical token: "
                f"{r.describe()}",
            )
            return
        assert isinstance(token, str)

        # G06: no output mode bypasses the gate.
        for mode in (["--format", "text"], ["--pretty"], ["--quiet"], ["--compact"], ["--json"]):
            r = self.run(st, "G06", gate(*mode))
            if r.writes != 0 or r.exit != 5:
                self.errorf(
                    "G06",
                    f"{path} {' '.join(mode)} without a token must be refused with exit 5 and send "
                    "nothing: "
                    f"{r.describe()} (writes {r.writes})",
                )
        r = self.run(st, "G06", gate("--format", "raw"))
        if r.writes != 0 or r.exit not in (5, 2):
            self.errorf(
                "G06",
                f"{path} --format raw without a token must be refused: {r.describe()} (writes "
                f"{r.writes})",
            )
        r = self.run(st, "G06", gate("--format", "text", "--dry-run"))
        if r.writes != 0 or r.exit != 0:
            self.errorf(
                "G06",
                f"{path} --format text --dry-run must preview without sending: {r.describe()}",
            )

        # G03: every mutated input is token_mismatch naming the parameter.
        self.check_mutations(st, c, m, sample, token)

        # G04: the exact confirm dispatches; a repeat is token_used. A command whose
        # only effects are configure, persist or artifact may change only local
        # things (the tool's keyring entry, its settings file, a local file) and send
        # nothing upstream: a receipt with change applied or persistence saved counts
        # as its one write (P7).
        r = self.run(st, "G04", gate("--confirm", token))
        reported = r.data().get("receipt") is not None or r.details().get("receipt") is not None
        if m.batch_flag:
            reported = (
                r.data().get("items") is not None and r.data().get("summary") is not None
            ) or (r.details().get("items") is not None and r.details().get("summary") is not None)
        local_only = not m.has_effect("change", "deliver")
        if local_only and r.writes < 1 and not _applied_locally(r):
            self.errorf(
                "G04",
                f"{path}: the exact confirm sent no write, and its receipt shows no applied "
                f"change or saved file: {r.describe()}",
            )
        elif not local_only and r.writes < 1:
            self.errorf("G04", f"{path}: the exact confirm sent no write: {r.describe()}")
        elif not reported:
            self.errorf(
                "G04",
                f"{path}: the confirm must report a receipt (per item for a batch): {r.describe()}",
            )
        elif r.exit != 0:
            self.t.log(
                f"G04: {path}: the confirm dispatched and reported {r.code()} (exit {r.exit}) per "
                "its receipt"
            )
        for d in r.defects:
            self.errorf("G04", f"{path}: {d}")
        op_id = r.data().get("operation_id")
        if isinstance(op_id, str) and op_id:
            show = self.run(st, "G04", ["operation", "show", "--id", op_id])
            if show.exit != 0:
                self.errorf(
                    "G04",
                    f"{path}: operation show cannot find the confirmed write: {show.describe()}",
                )
            else:
                try:
                    validate_operation_view(show.data())
                except ContractViolation as exc:
                    self.errorf("G04", f"{path}: operation show: {exc}")
                else:
                    status = show.data().get("status")
                    if status in ("dispatching", "unknown", "planned", "expired"):
                        self.errorf(
                            "G04", f"{path}: after a reported confirm the operation is {status}"
                        )
        r = self.run(st, "G04", gate("--confirm", token))
        if (
            r.writes != 0
            or r.exit != 6
            or r.code() != "E_CONFLICT"
            or r.details().get("conflict") != "token_used"
        ):
            self.errorf(
                "G04",
                f"{path}: repeating the confirm must be E_CONFLICT token_used and send nothing: "
                f"{r.describe()} (writes {r.writes})",
            )
        self.check_secrets(c, m, sample, st)

        # G05: re-encoded tokens are rejected.
        self.reset()
        st2 = self.new_state(True)
        r = self.run(st2, "G05", gate("--dry-run"))
        t2 = r.data().get("confirm_token")
        r = self.run(st2, "G05", gate("--dry-run"))
        t3 = r.data().get("confirm_token")
        if not _is_token(t2) or not _is_token(t3):
            self.errorf("G05", f"{path}: --dry-run did not return tokens: {r.describe()}")
        else:
            assert isinstance(t2, str) and isinstance(t3, str)
            variants = {
                "upper case": t2.upper(),
                "upper-case id": t2[:4] + t2[4:30].upper() + t2[30:],
                "padded": t2 + "=",
                "truncated": t2[:-1],
                "extra char": t2 + "0",
                "leading space": " " + t2,
                "other op id": t3[:31] + t2[31:],
                "other mac": t2[:31] + t3[31:],
                "no prefix": t2[4:],
            }
            for name in sorted(variants):
                r = self.run(st2, "G05", gate("--confirm", variants[name]))
                if r.writes != 0 or r.exit == 0:
                    self.errorf(
                        "G05",
                        f"{path}: a re-encoded token ({name}) must be rejected and send nothing: "
                        f"{r.describe()} (writes {r.writes})",
                    )
                elif r.code() != "E_CONFLICT" or r.details().get("conflict") != "token_mismatch":
                    self.errorf(
                        "G05",
                        f"{path}: a re-encoded token ({name}) must be E_CONFLICT token_mismatch: "
                        f"{r.describe()}",
                    )

        # G07: without a scope, dry-run and confirm are E_FORBIDDEN with details.scope.
        st2.scope_on = False
        r = self.run(st2, "G07", gate("--dry-run"))
        self.expect_forbidden(path, "dry-run", r)
        if _is_token(t2):
            r = self.run(st2, "G07", gate("--confirm", t2))  # type: ignore[arg-type]
            self.expect_forbidden(path, "confirm", r)
        st2.scope_on = True

        # G10: a scope changed between dry-run and confirm is scope_changed.
        r = self.run(st2, "G10", gate("--dry-run"))
        t4 = r.data().get("confirm_token")
        self.write_scope(st2, {"notes": "changed by the aicli conformance suite"})
        r = self.run(st2, "G10", gate("--confirm", str(t4)))
        if (
            r.writes != 0
            or r.exit != 6
            or r.code() != "E_CONFLICT"
            or r.details().get("conflict") != "scope_changed"
        ):
            self.errorf(
                "G10",
                f"{path}: a changed scope must be E_CONFLICT scope_changed and send nothing: "
                f"{r.describe()} (writes {r.writes})",
            )
        self.write_scope(st2, None)

        # G09: dangerous commands need --dangerous in both steps.
        if m.is_dangerous():
            r = self.run(st2, "G09", [*base, "--dry-run"])
            if (
                r.writes != 0
                or r.exit != 5
                or r.details().get("problem") != "dangerous_flag_required"
            ):
                self.errorf(
                    "G09",
                    f"{path}: --dry-run without --dangerous must be exit 5 "
                    f"dangerous_flag_required: {r.describe()}",
                )
            r = self.run(st2, "G09", gate("--dry-run"))
            t5 = r.data().get("confirm_token")
            r = self.run(st2, "G09", [*base, "--confirm", str(t5)])
            if (
                r.writes != 0
                or r.exit != 5
                or r.details().get("problem") != "dangerous_flag_required"
            ):
                self.errorf(
                    "G09",
                    f"{path}: --confirm without --dangerous must be exit 5 "
                    f"dangerous_flag_required: {r.describe()}",
                )

        # G08: a corrupt key or a blocked journal refuses with E_IO.
        st3 = self.new_state(True)
        r = self.run(st3, "G08", gate("--dry-run"))
        t6 = str(r.data().get("confirm_token"))
        key_path = os.path.join(st3.dir, "confirm.key")
        for bad in (b"corrupt", bytes([7]) * (KEY_SIZE + 1)):
            with open(key_path, "wb") as fh:
                fh.write(bad)
            if os.name != "nt":
                os.chmod(key_path, 0o600)
            for step in (gate("--confirm", t6), gate(), gate("--dry-run")):
                r = self.run(st3, "G08", step)
                if r.writes != 0 or r.exit != 1 or r.code() != "E_IO":
                    self.errorf(
                        "G08",
                        f'{path}: with a corrupt key, "{" ".join(step[len(base) :])}" must be E_IO '
                        "and send nothing: "
                        f"{r.describe()} (writes {r.writes})",
                    )
        st4 = self.new_state(True)
        os.makedirs(st4.dir, mode=0o700, exist_ok=True)
        if os.name != "nt":
            os.chmod(st4.dir, 0o700)
        with open(os.path.join(st4.dir, "operations"), "wb") as fh:
            fh.write(b"not a directory")
        for step in (gate(), gate("--dry-run")):
            r = self.run(st4, "G08", step)
            if r.writes != 0 or r.exit != 1 or r.code() != "E_IO":
                self.errorf(
                    "G08",
                    f'{path}: with the journal path blocked, "{" ".join(step[len(base) :])}" must '
                    "be E_IO and send nothing: "
                    f"{r.describe()} (writes {r.writes})",
                )

    def expect_forbidden(self, path: str, step: str, r: _Run) -> None:
        scope = r.details().get("scope")
        if r.writes != 0 or r.exit != 4 or r.code() != "E_FORBIDDEN" or not isinstance(scope, dict):
            self.errorf(
                "G07",
                f"{path}: {step} without a scope must be E_FORBIDDEN with details.scope and send "
                "nothing: "
                f"{r.describe()} (writes {r.writes})",
            )

    # ---------------------------------------------------------------- G03

    def check_mutations(self, st: _State, c: Node, m: Meta, sample: list[str], token: str) -> None:
        """G03: each sample input changed, dropped or added between the dry-run and
        the confirm must be token_mismatch naming it, with nothing sent."""
        path = c.path
        words = path.split()
        args = parse_sample(c, sample)
        positionals = sum(1 for a in args if a.positional)

        def confirm(
            label: str, mutated: list[_Arg], stdin: str, expect: str, required_drop: bool
        ) -> None:
            full = [*words, *join_args(mutated), "--confirm", token]
            if m.is_dangerous():
                full.append("--dangerous")
            r = self.run(st, "G03", full, stdin)
            if r.writes != 0 or r.exit == 0:
                self.errorf(
                    "G03",
                    f"{path}: {label} was confirmed or sent {r.writes} write(s): {r.describe()}",
                )
            elif r.code() == "E_CONFLICT" and r.details().get("conflict") == "token_mismatch":
                if r.details().get("parameter") != expect:
                    self.errorf(
                        "G03",
                        f'{path}: {label} must name "{expect}", named '
                        f'"{r.details().get("parameter")}"',
                    )
            elif r.code() == "E_CONFLICT":
                self.errorf(
                    "G03",
                    f"{path}: {label} passed the input check and failed later "
                    f"({r.details().get('conflict')}); "
                    "the token does not bind it",
                )
            elif required_drop and r.code() == "E_USAGE":
                pass
            elif r.exit in (2, 3):
                self.t.log(
                    f"G03: {path}: {label} was rejected before the gate ({r.code()}); its binding "
                    "was not exercised"
                )
            else:
                self.errorf(
                    "G03",
                    f'{path}: {label} must be E_CONFLICT token_mismatch naming "{expect}": '
                    f"{r.describe()}",
                )

        tmp = os.path.join(self.root, f"mut{self.counter:03d}")
        os.makedirs(tmp, exist_ok=True)
        for i, a in enumerate(args):
            label = "changing " + describe_arg(a)
            if a.positional:
                expect = "--" + m.positional if (m.positional and positionals == 1) else "args"
            else:
                expect = self.label_for(c, m, a.name)
            if a.has_value and a.name not in EXCLUDED_FROM_BINDING:
                mutated = list(args)
                stdin = ""
                f = c.flag(a.name) if not a.positional else None
                if a.positional:
                    value = mutate_value(None, m, "", a.value)
                    mutated[i] = _Arg(True, "", value, True, [value])
                elif f is not None and m.file_inputs.get(f.name) and a.value == "-":
                    stdin = "aicli conformance input (changed)\n"
                elif f is not None and m.file_inputs.get(f.name):
                    p = os.path.join(tmp, f"changed-{i}.txt")
                    with open(p, "w", encoding="utf-8", newline="\n") as fh:
                        fh.write("aicli conformance changed file content\n")
                    mutated[i] = with_flag_value(a, p)
                else:
                    mutated[i] = with_flag_value(a, mutate_value(f, m, a.name, a.value))
                confirm(label, mutated, stdin, expect, False)
            if not a.positional and a.name not in EXCLUDED_FROM_BINDING:
                dropped = args[:i] + args[i + 1 :]
                f = c.flag(a.name)
                required = f is not None and (f.required or a.name == m.batch_flag)
                confirm("dropping " + describe_arg(a), dropped, "", expect, required)
        present = {a.name for a in args if not a.positional}
        add: Flag | None = None
        for f in c.sorted_flags():
            if (
                f.name in present
                or f.hidden
                or is_global(f)
                or f.required
                or f.name == m.positional
            ):
                continue
            bound = m.file_inputs.get(f.name)
            if bound and bound in present:
                continue
            if any(b == f.name and file in present for file, b in m.file_inputs.items()):
                continue
            add = f
            break
        if add is not None:
            value = "aicli-conformance"
            doc = m.params.get(add.name)
            if doc is not None and doc.enum:
                value = doc.enum[0]
            elif add.param is not None and isinstance(add.param.type, click.Choice):
                value = str(add.param.type.choices[0])
            typ, _ = flag_type(add)
            if typ in ("integer", "number"):
                value = "1"
            if m.file_inputs.get(add.name):
                p = os.path.join(tmp, "added.txt")
                with open(p, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write("aicli conformance added file\n")
                value = p
            extra = _Arg(False, add.name, value, True, [f"--{add.name}={value}"])
            if add.no_opt_def_val:
                extra.tokens = [f"--{add.name}"]
                if add.type_name() == "bool" and add.def_value == "true":
                    extra.tokens = [f"--{add.name}=false"]
            confirm(
                "adding --" + add.name, [*args, extra], "", self.label_for(c, m, add.name), False
            )

    def label_for(self, c: Node, m: Meta, flag: str) -> str:
        """The parameter a token_mismatch names for a flag (binding's rule)."""
        if flag in m.file_inputs:
            return label_for(c, m.file_inputs[flag], flag)
        return "--" + flag

    def check_secrets(self, c: Node, m: Meta, sample: list[str], st: _State) -> None:
        """SEC-021: secret sample values never appear in any output or in the state
        directory."""
        secrets = [
            a.value
            for a in parse_sample(c, sample)
            if not a.positional and a.has_value and m.is_secret(a.name) and len(a.value) >= 4
        ]
        if not secrets:
            return
        for out in self.outputs:
            for sec in secrets:
                if sec.encode("utf-8") in out:
                    self.errorf("G04", f"{c.path}: a secret value appeared in the output (SEC-021)")
                    return
        for dirpath, _, files in os.walk(st.dir):
            for name in files:
                try:
                    with open(os.path.join(dirpath, name), "rb") as fh:
                        b = fh.read()
                except OSError:
                    continue
                for sec in secrets:
                    if sec.encode("utf-8") in b:
                        self.errorf(
                            "G04", f"{c.path}: a secret value was stored in {name} (SEC-021)"
                        )

    # ---------------------------------------------------------------- R04 and R06

    def check_envelopes(self) -> None:
        # validate_envelope covers the envelope, details, budgets, snake_case keys,
        # ids and UTC times (CLI-007); validate_stream does the same for each line
        # of a stream and checks that the summary is last.
        for label, raw, stream in self.seen:
            try:
                (validate_stream if stream else validate_envelope)(raw)
            except ContractViolation as exc:
                self.errorf("R04", f"{label}: {exc}")

    def check_coverage(self) -> None:
        if not self.h.coverage_file:
            return
        try:
            with open(self.h.coverage_file, "rb") as fh:
                lines = fh.read(64 << 20).decode("utf-8", "replace").splitlines()
        except OSError as exc:
            self.errorf("R06", f"the coverage file cannot be read: {exc}")
            return
        seen = set()
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and isinstance(rec.get("command"), str) and rec["command"]:
                seen.add(rec["command"])
        for c in self.probe.commands():
            if c.builtin:
                continue
            if c.path not in seen:
                self.errorf(
                    "R06",
                    f'command "{c.path}" was never executed by the tool\'s own tests (VER-003)',
                )


# ---------------------------------------------------------------- samples


def is_stream_output(raw: bytes) -> bool:
    """stdout that holds more than one JSON document, one per line: an NDJSON
    stream (CLI-006), which R04 checks with validate_stream."""
    try:
        text = raw.decode("utf-8")
        first, end = json.JSONDecoder().raw_decode(text.lstrip())
    except ValueError:
        return False
    head = text.lstrip()
    rest = head[end:].strip()
    return bool(rest) and rest[0] == "{" and "\n" not in head[:end]


def _applied_locally(r: _Run) -> bool:
    """A confirmed write whose receipt (or a batch item's) shows change applied or
    persistence saved (P7)."""
    receipts: list[Any] = []
    for part in (r.data(), r.details()):
        receipts.append(part.get("receipt"))
        items = part.get("items")
        for it in items if isinstance(items, list) else []:
            if isinstance(it, dict):
                receipts.append(it.get("receipt"))
    for receipt in receipts:
        outcome = receipt.get("outcome") if isinstance(receipt, dict) else None
        if isinstance(outcome, dict) and (
            outcome.get("change") == "applied" or outcome.get("persistence") == "saved"
        ):
            return True
    return False


def strip_path(sample: list[str], path: str) -> list[str]:
    """Samples that repeat the command path are tolerated."""
    words = path.split()
    if len(sample) >= len(words) and sample[: len(words)] == words:
        return list(sample[len(words) :])
    return list(sample)


def parse_sample(c: Node, sample: list[str]) -> list[_Arg]:
    out = []
    i = 0
    while i < len(sample):
        tok = sample[i]
        if not tok.startswith("-") or tok == "-":
            out.append(_Arg(True, "", tok, True, [tok]))
            i += 1
            continue
        from .app import split_flag

        name, value, inline = split_flag(tok)
        f = c.flag(name)
        arg = _Arg(False, f.name if f is not None else name, "", False, [tok])
        if inline:
            arg.value, arg.has_value = value, True
        elif f is not None and not f.no_opt_def_val and i + 1 < len(sample):
            arg.value, arg.has_value = sample[i + 1], True
            arg.tokens.append(sample[i + 1])
            i += 1
        out.append(arg)
        i += 1
    return out


def join_args(args: list[_Arg]) -> list[str]:
    out: list[str] = []
    for a in args:
        out.extend(a.tokens)
    return out


def with_flag_value(a: _Arg, value: str) -> _Arg:
    tokens = [a.tokens[0], value] if len(a.tokens) == 2 else [f"--{a.name}={value}"]
    return _Arg(a.positional, a.name, value, True, tokens)


def describe_arg(a: _Arg) -> str:
    if a.positional:
        return "the positional argument " + json.dumps(a.value, ensure_ascii=False)
    return "--" + a.name


def _format_f(x: float) -> str:
    """strconv.FormatFloat(x, 'f', -1, 64)."""
    d = decimal.Decimal(repr(x))
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _next_day(value: str) -> str | None:
    """An ISO date (YYYY-MM-DD) or an RFC 3339 date-time one day later, in the
    same form (Go's time.DateOnly and time.RFC3339Nano): the fraction keeps its
    digits without trailing zeros, and a zero offset is Z. None when value is
    neither."""
    if _DATE.fullmatch(value):
        try:
            d = _dt.date.fromisoformat(value)
            return (d + _dt.timedelta(days=1)).isoformat()
        except (ValueError, OverflowError):
            return None
    m = _DATE_TIME.fullmatch(value)
    if m is None:
        return None
    zone = m.group(8)
    try:
        d = _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        t = _dt.time(int(m.group(4)), int(m.group(5)), int(m.group(6)))
        if zone != "Z":
            hours, minutes = int(zone[1:3]), int(zone[4:6])
            if hours > 23 or minutes > 59:
                return None
            if hours == 0 and minutes == 0:
                zone = "Z"
        d += _dt.timedelta(days=1)
    except (ValueError, OverflowError):
        return None
    frac = (m.group(7) or "").rstrip("0")
    if frac == ".":
        frac = ""
    return f"{d.isoformat()}T{t.isoformat()}{frac}{zone}"


def mutate_value(f: Flag | None, m: Meta, name: str, value: str) -> str:
    """A different value of the same type, so G03 reaches the gate instead of
    failing the tool's own validation (P9): another allowed value of an enum, a
    boolean flipped, an integer or a float plus 1, an ISO date or date-time one day
    later, a #rrggbb colour inverted; anything else with -mutated appended. A list
    gets one more element."""
    if f is not None:
        doc = m.params.get(name)
        enum = list(doc.enum) if doc is not None and doc.enum else []
        if not enum and f.param is not None and isinstance(f.param.type, click.Choice):
            enum = [str(x) for x in f.param.type.choices]
        if len(enum) > 1:
            for e in enum:
                if e != value:
                    return e
        if f.multiple:
            # The sample's own last element decides: a list of integers gets another
            # integer.
            last = value.split(",")[-1].strip()
            if _NUMBER.fullmatch(last):
                return value + "," + str(int(last) + 1000)
            return value + ",aicli-mutated"
        typ, _ = flag_type(f)
        if typ == "boolean":
            try:
                return go_bool_string(not parse_go_bool(value))
            except ValueError:
                pass
        elif typ == "number":
            try:
                return _format_f(float(value) + 1)
            except ValueError:
                pass
    if _NUMBER.fullmatch(value):
        return str(int(value) + 1)
    if _FLOAT.fullmatch(value):
        return _format_f(float(value) + 1)
    if _COLOUR.fullmatch(value):
        return f"#{0xFFFFFF ^ int(value[1:], 16):06x}"
    later = _next_day(value)
    if later is not None:
        return later
    return value + "-mutated"


def _rune_len(b: bytes) -> int:
    return len(b.rstrip(b"\n").decode("utf-8", "replace"))
