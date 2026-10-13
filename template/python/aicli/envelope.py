# Vendored from ai-native-cli-spec template/python/aicli.
# Do not edit in tool repos; run scripts/sync-spec.js.
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sean Guo

"""The envelope (CLI-001 .. CLI-005, CLI-040, CLI-042, CLI-046, CLI-047): one
writer for every response, --fields projection, meta, notices, the error size
budget and the text format for people."""

from __future__ import annotations

import dataclasses
import io
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ._util import (
    Ordered,
    RawNumber,
    go_bool_string,
    go_float,
    go_json,
    to_generic,
    to_generic_data,
    truncate_runes,
)
from .contract_gen import (
    CORE_CODES,
    ERROR_BUDGET_CHARS,
    NOTICE_SEVERITY_VALUES,
    OPERATION_DONE_STATUS_VALUES,
    PREVIEW_REQUIRED_KEYS,
    SCHEMA_VERSION,
    SUCCESS_BUDGET_CHARS,
)
from .errors import Error, as_error, internal_error
from .token import valid_operation_id

if TYPE_CHECKING:
    import datetime as _dt

    from ._tree import Node
    from .app import App
    from .registry import Meta, SchemaDef


@dataclass
class Notice:
    """A typed warning attached to meta.notices (CLI-047, CLI-071, CLI-081).
    Update notices (type update_available) also carry the version fields."""

    type: str
    severity: str
    message: str
    current_version: str = ""
    latest_version: str = ""
    command: str = ""
    checked_at: str = ""

    def to_json(self) -> Ordered:
        o = Ordered(type=self.type, severity=self.severity, message=self.message)
        for k in ("current_version", "latest_version", "command", "checked_at"):
            v = getattr(self, k)
            if v:
                o[k] = v
        return o


@dataclass
class Result_:
    """What a run reports: success data or an error, plus untrusted paths."""

    set: bool = False
    data: Any = None  # generic JSON value
    raw: Any = None  # the value as the command passed it, for --format raw
    untrusted: list[str] = field(default_factory=list)
    err: Error | None = None
    library: bool = (
        False  # the library's own output (gate results, operation show): sized to the budget
    )


@dataclass
class FieldSelection:
    """The parsed --fields value (CLI-040)."""

    set: bool = False
    all: bool = False
    add: bool = False
    names: list[str] = field(default_factory=list)


@dataclass
class RunState:
    """The per-execute state: output mode, the executing command and what it
    reported."""

    start: _dt.datetime
    start_real: float
    format: str = "json"
    pretty: bool = False
    quiet: bool = False
    args: list[str] = field(default_factory=list)
    cmd: Node | None = None
    fields: FieldSelection = field(default_factory=FieldSelection)
    notices: list[Notice] = field(default_factory=list)
    out: Result_ = field(default_factory=Result_)
    gate: Result_ = field(default_factory=Result_)  # authoritative once set
    cmd_err: BaseException | None = None
    panic_err: Error | None = None
    help_shown: bool = False
    write_calls: int = 0
    positionals: list[str] = field(default_factory=list)
    positional_consumed: bool = False
    files: dict[str, Any] = field(default_factory=dict)
    stdin_flag: str = ""
    stream: Any = None  # an NDJSON stream (stream.Stream) once the command starts one
    debugged: set[str] = field(default_factory=set)  # library debug lines written this run
    profile: Any = None  # the resolved config profile (settings.ProfileState), once read


def prefix_paths(prefix: str, paths: list[str]) -> list[str]:
    """Root relative paths at prefix ("data" -> "data.items[].title")."""
    out = []
    for p in paths:
        p = p.strip()
        if not p:
            continue
        if p == prefix or p.startswith(prefix + ".") or p.startswith(prefix + "[]"):
            out.append(p)
        elif p.startswith("[]"):
            out.append(prefix + p)
        else:
            out.append(prefix + "." + p)
    return out


def schema_untrusted(s: SchemaDef, shape: str) -> list[str]:
    """A schema's untrusted fields as envelope paths for its shape."""
    out = []
    for f in s.untrusted:
        f = f.strip()
        if not f:
            continue
        if shape == "page":
            out.append("data.items[]." + f)
        elif shape == "array":
            out.append("data[]." + f)
        else:
            out.append("data." + f)
    return out


def clean_paths(paths: list[str]) -> list[str]:
    """De-duplicate and sort untrusted paths."""
    return sorted({p.strip() for p in paths if p.strip()})


def keep_keys(v: Any, keep: set[str]) -> Any:
    if not isinstance(v, dict):
        return v
    return {k: val for k, val in v.items() if k in keep}


def encode_envelope(env: Ordered, pretty: bool = False) -> bytes:
    return (go_json(env, pretty=pretty) + "\n").encode("utf-8")


class EnvelopeMixin:
    """App methods for output, projection and the single writer."""

    # ---------------------------------------------------------------- streams

    def _stdout(self: App) -> Any:
        if self.stdout is not None:
            return self.stdout
        import sys

        return sys.stdout

    def _stderr(self: App) -> Any:
        if self.stderr is not None:
            return self.stderr
        import sys

        return sys.stderr

    def _stdin(self: App) -> Any:
        if self.stdin is not None:
            return self.stdin
        import sys

        return sys.stdin

    @staticmethod
    def _write(stream: Any, data: bytes) -> None:
        """Write bytes exactly (no newline translation, UTF-8) to a binary stream,
        to the binary buffer of a text stream, or as text to a text-only stream."""
        buf = getattr(stream, "buffer", None)
        if buf is not None:
            try:
                stream.flush()
            except (OSError, ValueError):
                pass
            buf.write(data)
            buf.flush()
            return
        if isinstance(stream, io.TextIOBase):
            stream.write(data.decode("utf-8", "replace"))
        else:
            try:
                stream.write(data)
            except TypeError:
                stream.write(data.decode("utf-8", "replace"))
        flush = getattr(stream, "flush", None)
        if flush is not None:
            flush()

    # ---------------------------------------------------------------- notices and output

    def notice(self: App, n: Notice) -> None:
        """Add a notice to the current response's meta.notices."""
        st = self._run
        if st is None:
            return
        if not n.type.strip():
            n = dataclasses.replace(n, type="notice")
        if n.severity not in NOTICE_SEVERITY_VALUES:
            n = dataclasses.replace(n, severity="info")
        if n in st.notices:
            return
        st.notices.append(n)

    def ok(self: App, cmd: Any, data: Any, *untrusted: str) -> None:
        """Report success with data (CLI-002). untrusted lists paths inside data
        that carry external content, relative to data (for example "description"
        or "items[].title"); the command's output schema adds its untrusted
        fields automatically. --fields and default fields apply when the
        command's schema declares them. Write commands report through write or
        write_batch instead."""
        st, node = self._run_for(cmd)
        m = self.meta_for(node)
        if m is not None and m.is_write():
            raise internal_error(
                "A write command reported success without the write gate; it must use App.write "
                "or App.write_batch."
            )
        try:
            gen = to_generic_data(data)
        except (TypeError, ValueError):
            raise internal_error("The command's output cannot be encoded as JSON.") from None
        schema = self.schema_of(m)
        shape = schema.shape or ("array" if isinstance(gen, list) else "object")
        gen = self.project(st, m, schema, shape, gen)
        paths = prefix_paths("data", list(untrusted))
        paths += schema_untrusted(schema, shape)
        st.out = Result_(set=True, data=gen, raw=data, untrusted=paths)

    def project(
        self: App, st: RunState, m: Meta | None, schema: SchemaDef, shape: str, v: Any
    ) -> Any:
        """Apply --fields and default fields to a command's output (CLI-040).
        Pagination keys are never removed and "ref" is always kept."""
        defaults = list(schema.default_fields)
        if m is not None and m.default_fields:
            defaults = list(m.default_fields)
        keep: set[str] | None = None
        sel = st.fields
        if sel.all:
            pass
        elif sel.set and not sel.add:
            keep = set(sel.names)
        elif sel.set and sel.add:
            if defaults:
                keep = set(defaults) | set(sel.names)
        elif defaults:
            keep = set(defaults)
        if keep is None:
            return v
        keep.add("ref")
        if shape == "page":
            if not isinstance(v, dict):
                return v
            items = v.get("items")
            if isinstance(items, list):
                v["items"] = [keep_keys(it, keep) for it in items]
            return v
        if shape == "array":
            if isinstance(v, list):
                return [keep_keys(it, keep) for it in v]
            return v
        return keep_keys(v, keep)

    # ---------------------------------------------------------------- finish and emit

    def _finish(self: App, st: RunState) -> int:
        """Turn the run's outcome into exactly one response; return the exit code."""
        if st.gate.set:
            res = st.gate
        elif st.panic_err is not None:
            res = Result_(set=True, err=st.panic_err)
        elif st.cmd_err is not None:
            res = Result_(set=True, err=as_error(st.cmd_err))
        elif st.out.set:
            res = st.out
        elif st.help_shown:
            return 0
        else:
            res = Result_(set=True, data={}, raw=None)
        self._attach_update_notice(st)
        if st.stream is not None:
            code = self._emit_stream(st, res)
        else:
            code = self._emit(st, res)
        self._record_coverage(st, code)
        return code

    def _meta(self: App, st: RunState, untrusted: list[str], notices: bool = True) -> Ordered:
        meta = Ordered(duration_ms=int((time.monotonic() - st.start_real) * 1000))
        if st.notices and notices:
            meta["notices"] = [n.to_json() for n in st.notices]
        if untrusted:
            meta["untrusted"] = untrusted
        return meta

    def _emit(self: App, st: RunState, res: Result_) -> int:
        stdout, stderr = self._stdout(), self._stderr()
        if res.err is None:
            untrusted = clean_paths(res.untrusted)
            if st.format == "text":
                self._write(stdout, write_text(res.data).encode("utf-8"))
                self._text_notices(st, stderr)
                return 0
            if st.format == "raw":
                if isinstance(res.raw, (bytes, bytearray)):
                    self._write(stdout, bytes(res.raw))
                    return 0
                if isinstance(res.raw, str):
                    self._write(stdout, res.raw.encode("utf-8"))
                    return 0
                try:
                    self._write(stdout, (go_json(res.data) + "\n").encode("utf-8"))
                    return 0
                except (TypeError, ValueError):
                    res = Result_(
                        set=True, err=internal_error("The command's output cannot be encoded.")
                    )
            else:
                meta = self._meta(st, untrusted)
                env = Ordered(
                    ok=True,
                    schema_version=SCHEMA_VERSION,
                    data=fit_success_budget(res.data, meta) if res.library else res.data,
                    meta=meta,
                )
                try:
                    self._write(stdout, encode_envelope(env, st.pretty))
                    return 0
                except (TypeError, ValueError):
                    res = Result_(
                        set=True,
                        err=internal_error("The command's output cannot be encoded as JSON."),
                    )
        assert res.err is not None
        obj, code, err_untrusted = self.normalize_error(res.err)
        untrusted = clean_paths(err_untrusted + res.untrusted)
        meta = self._meta(st, untrusted)
        obj = fit_error_budget(obj, meta, self.tool_name())
        if st.format == "text":
            self._write(stderr, write_text_error(obj).encode("utf-8"))
            self._text_notices(st, stderr)
            return code
        env = Ordered(ok=False, schema_version=SCHEMA_VERSION, error=obj, meta=meta)
        try:
            out = encode_envelope(env, st.pretty)
        except (TypeError, ValueError):
            obj = Ordered(
                code="E_UNKNOWN",
                message="The error could not be encoded as JSON.",
                retryable=False,
                details={
                    "dispatch_state": obj["details"].get("dispatch_state"),
                    "next_action": "Report this bug in the tool.",
                },
            )
            env = Ordered(
                ok=False,
                schema_version=SCHEMA_VERSION,
                error=obj,
                meta=Ordered(duration_ms=meta["duration_ms"]),
            )
            out = encode_envelope(env)
            code = CORE_CODES["E_UNKNOWN"].exit
        self._write(stdout, out)
        return code

    def _text_notices(self: App, st: RunState, stream: Any) -> None:
        if st.quiet:
            return
        for n in st.notices:
            self._write(stream, f"notice ({n.severity}): {n.message}\n".encode())


# ---------------------------------------------------------------- error budget


def error_size(obj: Ordered, meta: Ordered) -> int:
    """Characters of the compact error envelope (without the newline)."""
    env = Ordered(ok=False, schema_version=SCHEMA_VERSION, error=obj, meta=meta)
    try:
        return len(go_json(env))
    except (TypeError, ValueError):
        return 0


def fit_error_budget(obj: Ordered, meta: Ordered, tool: str) -> Ordered:
    """Shrink an error object until its envelope fits the error budget (CLI-042):
    optional hints first; then the receipts and results of batch items that
    succeeded; then long strings; then lists by the contract's shortening rules (a
    batch preview one to one with omitted, batch items failed first with omitted);
    and finally only what each code requires. tool names the tool in the note that
    says where the full details are."""
    budget = ERROR_BUDGET_CHARS
    if error_size(obj, meta) <= budget:
        return obj
    d = dict(obj["details"])
    obj = Ordered(obj)
    obj["details"] = d

    def fits() -> bool:
        return error_size(obj, meta) <= budget

    def fits_marked() -> bool:
        # what the caller gets: mark_shortened appends a note to next_action
        c = Ordered(obj)
        c["details"] = dict(obj["details"])
        return error_size(mark_shortened(c, tool), meta) <= budget

    for k in ("available", "usage", "expected"):
        d.pop(k, None)
    dym = d.get("did_you_mean")
    if isinstance(dym, (list, tuple)) and len(dym) > 3:
        d["did_you_mean"] = list(dym[:3])
    for k in ("upstream_message", "cause"):
        if isinstance(d.get(k), str):
            d[k] = truncate_runes(d[k], 200)
    if fits():
        return obj
    if "items" in d:
        d["items"] = compact_ok_items(d["items"])
        if fits_marked():
            return mark_shortened(obj, tool)
    for key in ("preview", "receipt"):
        if key in d:
            d[key] = shorten_strings(d[key], 80)
    if fits_marked():
        return mark_shortened(obj, tool)
    # A local scope refusal is acted on by a person: its scope details (path,
    # missing, grant_example) outlive the optional preview of the refused write.
    if obj["code"] == "E_FORBIDDEN" and "scope" in d and "preview" in d:
        del d["preview"]
        if fits_marked():
            return mark_shortened(obj, tool)
    if "scope" in d:
        d["scope"] = shorten_strings(d["scope"], 80)
    if fits_marked():
        return mark_shortened(obj, tool)
    all_items = d.get("items")
    for cut in (10, 5, 3, 1):
        p = d.get("preview")
        if isinstance(p, dict):
            p = shorten_batch_preview(p, cut)
            if "changes" in p:
                p = dict(p)
                p["changes"] = head_of(p["changes"], cut)
            d["preview"] = p
        if all_items is not None:
            d["items"], d["omitted"] = trim_batch_items(all_items, True, cut)
        if fits_marked():
            return mark_shortened(obj, tool)
    # Last resort: what each code requires, and a minimal preview, receipt and batch list.
    keep = {
        "dispatch_state",
        "next_action",
        "problem",
        "conflict",
        "parameter",
        "confirm_token",
        "expires_at",
        "operation_id",
        "lookup",
        "action",
        "resume",
        "summary",
        "active_operation",
        "omitted",
        "stage",
        "current_version",
        "binary_replaced",
        "skill_sync_status",
        "target_version",
        "skill_sync_command",
        "http_status",
    }
    minimal: dict[str, Any] = {k: v for k, v in d.items() if k in keep}
    if isinstance(d.get("preview"), dict):
        minimal["preview"] = minimal_preview(d["preview"])
    if isinstance(d.get("receipt"), dict):
        minimal["receipt"] = minimal_receipt(d["receipt"])
    if all_items is not None:
        minimal["items"], minimal["omitted"] = trim_batch_items(all_items, True, 1)
        items = minimal["items"]
        if isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict):
            if isinstance(items[0].get("receipt"), dict):
                m = dict(items[0])
                m["receipt"] = minimal_receipt(m["receipt"])
                minimal["items"] = [m]
    if "candidates" in d:
        minimal["candidates"] = head_of(d["candidates"], 3)
    if isinstance(d.get("scope"), dict):
        s = d["scope"]
        ms: dict[str, Any] = {"status": s.get("status"), "path": s.get("path")}
        if "scope_id" in s:
            ms["scope_id"] = s["scope_id"]
        if "missing" in s:
            ms["missing"] = shorten_strings(s["missing"], 60)
        if "grant_example" in s:
            ms["grant_example"] = shorten_strings(s["grant_example"], 60)
        minimal["scope"] = ms
    obj["details"] = minimal
    obj["message"] = truncate_runes(obj["message"], 300)
    obj = mark_shortened(obj, tool)
    minimal = obj["details"]
    if fits():
        return obj
    # Still over (long paths, escaped twice in JSON): shorten every string that is
    # not an id, a token, a time or an enum.
    for k in list(minimal):
        if k in (
            "confirm_token",
            "operation_id",
            "active_operation",
            "expires_at",
            "lookup",
            "dispatch_state",
            "problem",
            "conflict",
            "action",
            "stage",
            "skill_sync_status",
            "next_action",
        ):
            continue
        minimal[k] = shorten_strings(minimal[k], 48)
    if fits():
        return obj
    # Finally drop what the code does not require: the preview, then the example grant.
    # A token never goes without the preview it confirms: a conflict's fresh plan
    # loses both, and its next step becomes a new dry-run.
    if (
        obj["code"] != "E_CONFIRMATION_REQUIRED"
        or minimal.get("problem") != "confirmation_required"
    ):
        if "preview" in minimal and "confirm_token" in minimal:
            minimal.pop("confirm_token", None)
            minimal.pop("expires_at", None)
            if obj["code"] == "E_CONFLICT":
                minimal.pop("operation_id", None)
                minimal["next_action"] = (
                    "Run the same command again with --dry-run, check the new preview, "
                    "then confirm its token."
                )
        minimal.pop("preview", None)
    if fits():
        return obj
    if isinstance(minimal.get("scope"), dict):
        s = minimal["scope"]
        ms = {"status": s.get("status"), "path": s.get("path"), "scope_id": s.get("scope_id")}
        if "missing" in s and s.get("status") == "insufficient":
            ms["missing"] = shorten_strings(s["missing"], 32)
        minimal["scope"] = ms
    return obj


def compact_ok_items(v: Any) -> Any:
    """Batch items with the receipt and result of every item that succeeded left
    out (the first shortening step of contract.json batch.shortening)."""
    if not isinstance(v, list):
        return v
    out = []
    for it in v:
        if not isinstance(it, dict) or it.get("ok") is not True:
            out.append(it)
            continue
        out.append({k: val for k, val in it.items() if k not in ("receipt", "result")})
    return out


def trim_batch_items(v: Any, compact: bool, n: int) -> tuple[list[Any], int]:
    """At most n batch items, the items with ok false first (all of them before any
    item that succeeded), each group in input order, and how many were left out.
    Items that succeeded carry no receipt or result once trimmed."""
    items = v if isinstance(v, list) else []
    if compact:
        items = compact_ok_items(items)
    failed = [it for it in items if not (isinstance(it, dict) and it.get("ok") is True)]
    succeeded = [it for it in items if isinstance(it, dict) and it.get("ok") is True]
    ordered = failed + succeeded
    n = max(n, 1)
    if len(ordered) <= n:
        return ordered, 0
    return ordered[:n], len(ordered) - n


def _count(x: Any) -> int | None:
    """An integer count, also from a stored record's number text."""
    if isinstance(x, bool):
        return None
    if isinstance(x, int):
        return x
    if isinstance(x, RawNumber) and x.isdigit():
        return int(x)
    return None


def shorten_batch_preview(p: dict[str, Any], n: int) -> dict[str, Any]:
    """The first n targets and items of a batch preview, one to one in input order,
    with omitted set to the number left out (contract.json preview.shortening);
    total still counts every target. A single-target preview is returned as it is."""
    targets = p.get("targets") if isinstance(p.get("targets"), list) else []
    items = p.get("items")
    if not isinstance(items, list) or n < 1:
        return p
    if len(targets) <= n and len(items) <= n:
        return p
    out = dict(p)
    keep = max(min(n, len(targets), len(items)), 1)
    out["targets"] = list(targets[: min(keep, len(targets))])
    out["items"] = list(items[: min(keep, len(items))])
    total = _count(p.get("total"))
    if total is None:
        total = len(targets)
    out["omitted"] = total - min(keep, len(targets))
    return out


def mark_shortened(obj: Ordered, tool: str) -> Ordered:
    if obj["details"].get("omitted") == 0 and not isinstance(obj["details"].get("omitted"), bool):
        del obj["details"]["omitted"]
    na = obj["details"].get("next_action")
    if isinstance(na, str) and na and "shortened" not in na:
        obj["details"]["next_action"] = na + shortened_note(obj, tool)
    return obj


def shortened_note(obj: Ordered, tool: str) -> str:
    """Where the full details are, when they exist: a preview comes back whole from
    --dry-run (the success budget is larger), the items of a batch from operation
    list --batch (operation show of a large batch lists only some), a single
    recorded operation from operation show. A refused write was never recorded."""
    d = obj["details"]
    batch_id = d.get("operation_id")
    if obj["code"] == "E_CONFIRMATION_REQUIRED" and "preview" in d:
        return " (Details were shortened to fit; --dry-run returns the full preview.)"
    if "items" in d and valid_operation_id(batch_id) and batch_id.startswith("opb_"):
        command = "operation list --batch " + batch_id
        if tool:
            command = tool + " " + command
        return " (Details were shortened to fit; `" + command + "` lists every item.)"
    if "items" in d or "receipt" in d:
        return " (Details were shortened to fit; `operation show --id` returns them in full.)"
    return " (Details were shortened to fit.)"


def minimal_preview(p: dict[str, Any]) -> dict[str, Any]:
    """A preview's required keys, its target (no changes) or its first batch target
    and item (one to one, with omitted), and empty inputs."""
    out = {k: p[k] for k in PREVIEW_REQUIRED_KEYS if k in p}
    out["inputs"] = {}
    for k in ("destinations", "outputs", "base"):
        if k in p:
            out[k] = shorten_strings(head_of(p[k], 3), 60)
    if "target" in p:
        out["target"] = p["target"]
        out["changes"] = []
    if "items" in p:
        short = shorten_batch_preview(p, 1)
        out["total"] = short.get("total")
        out["targets"] = short.get("targets")
        items = []
        for it in short.get("items") or []:
            if isinstance(it, dict):
                it = dict(it)
                it["changes"] = []
            items.append(it)
        out["items"] = items
        if "omitted" in short:
            out["omitted"] = short["omitted"]
    return out


def minimal_receipt(r: dict[str, Any]) -> dict[str, Any]:
    """Every key a receipt requires and what its outcome needs: mismatched for a
    failed read-back, failed_stage for a failed execution, and the observed values
    (shortened) that verification passed or failed on."""
    out = {
        k: r[k]
        for k in (
            "operation_id",
            "command",
            "target",
            "effects",
            "outcome",
            "replayed",
            "mismatched",
            "failed_stage",
        )
        if k in r
    }
    obs = r.get("observed") if isinstance(r.get("observed"), dict) else {}
    mismatched = [str(x) for x in r.get("mismatched") or []]
    small: dict[str, Any] = {}
    for k in sorted(obs):
        if len(small) >= 3 and k not in mismatched:
            continue
        small[k] = shorten_strings(obs[k], 40)
    out["observed"] = small
    return out


def success_size(data: Any, meta: Ordered) -> int:
    """Characters of the compact success envelope (without the newline)."""
    try:
        return len(go_json(Ordered(ok=True, schema_version=SCHEMA_VERSION, data=data, meta=meta)))
    except (TypeError, ValueError):
        return 0


def fit_success_budget(data: Any, meta: Ordered) -> Any:
    """Keep the gate's own large results within the success budget (CLI-042) by the
    contract's shortening rules: a batch result leaves out the receipt and result
    of items that succeeded, then lists fewer items (failed ones first) with
    omitted; a dry-run's batch preview keeps its first targets and items, one to
    one, with omitted. Other data is the tool's own and is never cut here."""
    budget = SUCCESS_BUDGET_CHARS
    if success_size(data, meta) <= budget:
        return data
    if not isinstance(data, dict):
        return data
    out = dict(data)

    def fits() -> bool:
        return success_size(out, meta) <= budget

    if "items" in out and "summary" in out:
        out["items"] = compact_ok_items(out["items"])
        if fits():
            return out
        for n in (200, 100, 50, 20, 10, 5, 1):
            items, omitted = trim_batch_items(data["items"], True, n)
            out["items"] = items
            out.pop("omitted", None)
            if omitted > 0:
                out["omitted"] = omitted
            if fits():
                return out
    elif "preview" in out and "confirm_token" in out:
        p = out["preview"]
        if not isinstance(p, dict) or "items" not in p:
            return data
        for n in (200, 100, 50, 20, 10, 5, 1):
            out["preview"] = shorten_batch_preview(p, n)
            if fits():
                return out
    return out


def fit_operation_budget(v: dict[str, Any], tool: str) -> tuple[dict[str, Any], Notice | None]:
    """Keep a batch operation's view within the success budget (CLI-042) by the
    contract's operation.shortening rule. The preview first keeps its first targets
    and items, one to one, with omitted (preview.shortening). If the view is still
    too large, items lists the items that need attention first (status not in
    done_status), then the done ones, each group in input order, cut to fit, and
    items_omitted counts the rest; the returned notice then names operation list
    --batch, which pages through every item. The fit test keeps room for meta, the
    notice included. An item's receipt is in its own operation show."""
    items = v.get("items")
    if not isinstance(items, list):
        return v, None

    def fits(x: dict[str, Any], n: Notice | None) -> bool:
        # meta is added later: keep room for it.
        meta = Ordered(duration_ms=0)
        if n is not None:
            meta["notices"] = [n.to_json()]
        return success_size(x, meta) <= SUCCESS_BUDGET_CHARS - 500

    if fits(v, None):
        return v, None
    out = dict(v)
    p = v.get("preview")
    if isinstance(p, dict) and "items" in p:
        for n in (200, 100, 50, 20, 10, 5, 1):
            out["preview"] = shorten_batch_preview(p, n)
            if fits(out, None):
                return out, None
    ordered = attention_first(items)
    batch_id = v["operation_id"] if isinstance(v.get("operation_id"), str) else ""
    notice: Notice | None = None
    for n in (200, 100, 50, 20, 10, 5, 1):
        if n >= len(ordered):
            continue
        out["items"] = ordered[:n]
        out["items_omitted"] = len(ordered) - n
        notice = items_omitted_notice(tool, batch_id, n, len(ordered))
        if fits(out, notice):
            break
    return out, notice


def attention_first(items: list[Any]) -> list[Any]:
    """Batch items in the order of a shortened view (operation.shortening): the
    items whose status is not in done_status first, then the done ones, each group
    in input order."""

    def done(it: Any) -> bool:
        return isinstance(it, dict) and it.get("status") in OPERATION_DONE_STATUS_VALUES

    return [it for it in items if not done(it)] + [it for it in items if done(it)]


def items_omitted_notice(tool: str, batch_id: str, listed: int, total: int) -> Notice:
    """The meta notice of a shortened batch view: how many items it lists and the
    command that pages through all of them."""
    command = f"{tool} operation list --batch {batch_id}"
    return Notice(
        type="items_omitted",
        severity="info",
        message=f"operation show lists {listed} of the batch's {total} items, those that need "
        f"attention first; run `{command}` to page through all of them.",
        command=command,
    )


def head_of(v: Any, n: int) -> Any:
    if isinstance(v, list) and len(v) > n:
        return list(v[:n])
    return v


def shorten_strings(v: Any, n: int) -> Any:
    """Cut long strings in maps and lists (tuples stand for Go's []string and are
    left as they are, as Go leaves typed string slices)."""
    if isinstance(v, str) and not isinstance(v, RawNumber):
        return truncate_runes(v, n)
    if isinstance(v, dict):
        return {
            k: (
                val
                if k in ("confirm_token", "operation_id", "expires_at")
                else shorten_strings(val, n)
            )
            for k, val in v.items()
        }
    if isinstance(v, list):
        return [shorten_strings(x, n) for x in v]
    return v


# ---------------------------------------------------------------- text output


def write_text(v: Any) -> str:
    """Data for people (--format text). It is not a parseable contract."""
    b: list[str] = []
    render_text(b, to_generic(v), "")
    return "".join(b) or "ok\n"


def render_text(b: list[str], v: Any, indent: str) -> None:
    if isinstance(v, dict):
        for k in sorted(v):
            val = v[k]
            if is_scalar(val):
                b.append(f"{indent}{k}: {scalar_text(val)}\n")
                continue
            b.append(f"{indent}{k}:\n")
            render_text(b, val, indent + "  ")
    elif isinstance(v, list):
        if not v:
            b.append(f"{indent}(none)\n")
        for item in v:
            if is_scalar(item):
                b.append(f"{indent}- {scalar_text(item)}\n")
                continue
            if isinstance(item, dict) and all_scalar(item):
                parts = [k + "=" + scalar_text(item[k]) for k in sorted(item)]
                b.append(f"{indent}- {'  '.join(parts)}\n")
                continue
            b.append(f"{indent}-\n")
            render_text(b, item, indent + "  ")
    else:
        b.append(f"{indent}{scalar_text(v)}\n")


def is_scalar(v: Any) -> bool:
    return not isinstance(v, (dict, list, tuple))


def all_scalar(m: dict[str, Any]) -> bool:
    for v in m.values():
        if not is_scalar(v):
            if isinstance(v, (list, tuple)):
                if all(is_scalar(x) for x in v):
                    continue
            return False
    return True


def scalar_text(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, RawNumber):
        return str.__str__(v)
    if isinstance(v, str):
        return v
    if isinstance(v, (list, tuple)):
        return ",".join(scalar_text(e) for e in v)
    if isinstance(v, bool):
        return go_bool_string(v)
    if isinstance(v, float):
        return go_float(v)
    return str(v)


def write_text_error(obj: Ordered) -> str:
    d = obj["details"]
    b = [f"error {obj['code']}: {obj['message']}\n"]
    na = d.get("next_action")
    if isinstance(na, str) and na:
        b.append(f"next: {na}\n")
    for k in (
        "problem",
        "conflict",
        "parameter",
        "operation_id",
        "confirm_token",
        "expires_at",
        "lookup",
    ):
        if d.get(k) is not None:
            b.append(f"{k}: {scalar_text(d[k])}\n")
    if "preview" in d:
        b.append("preview:\n")
        try:
            render_text(b, to_generic(d["preview"]), "  ")
        except (TypeError, ValueError):
            pass
    return "".join(b)
