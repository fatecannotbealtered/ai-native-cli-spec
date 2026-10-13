# Vendored from ai-native-cli-spec template/python/aicli.
# Do not edit in tool repos; run scripts/sync-spec.js.
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sean Guo

"""What a confirm token binds (CLI-054): the normalised invocation, file and
stdin inputs read once and bound by content, secrets bound by keyed digest."""

from __future__ import annotations

import hashlib
import hmac
import os
import stat
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ._tree import Flag, FlagValueError, Node
from ._util import (
    RawNumber,
    canonical_json,
    claim_string,
    claim_strings,
    parse_go_bool,
    raw_bytes,
    sha256_hex,
    snake,
    valid_utf8,
)
from .contract_gen import EXCLUDED_FROM_BINDING
from .errors import Error, internal_error, io_error, usage
from .registry import flag_type

if TYPE_CHECKING:
    from .app import App
    from .envelope import RunState
    from .registry import Meta

MAX_FILE_INPUT_BYTES = 64 << 20
SECRET_DOMAIN = b"aicli-secret/2\x00"


@dataclass
class FileInput:
    """The content of a file flag, read once per run so the bytes the tool sends
    are the bytes the token binds."""

    path: str
    content: bytes


@dataclass
class Binding:
    """A normalised invocation: every supplied parameter except the control
    flags, by canonical name, with file contents and secrets as digests."""

    command: str
    values: dict[str, str] = field(default_factory=dict)  # name -> bound value
    digests: dict[str, str] = field(default_factory=dict)  # name -> sha256 of the bound value
    display: dict[str, Any] = field(default_factory=dict)  # snake_case name -> preview.inputs value
    labels: dict[str, str] = field(default_factory=dict)  # name -> label used in token_mismatch
    inv: str = ""

    def add(self, name: str, value: str, shown: Any, label: str) -> None:
        self.values[name] = value
        self.digests[name] = "sha256:" + sha256_hex(value.encode("utf-8"))
        head, dot, rest = name.partition(".")
        if dot:  # setting.<name> is shown as inputs.setting.<name> (keys stay snake_case)
            nested = self.display.setdefault(snake(head), {})
            if isinstance(nested, dict):
                nested[rest] = shown
        else:
            self.display[snake(name)] = shown
        self.labels[name] = label


def secret_digest(key: bytes, name: str, value: bytes) -> str:
    """The keyed digest that binds a secret without revealing it."""
    mac = hmac.new(key, SECRET_DOMAIN + name.encode("utf-8") + b"\x00" + value, hashlib.sha256)
    return "hmac-sha256:" + mac.hexdigest()


def content_value(content: bytes, secret: bool, key: bytes, name: str) -> str:
    """Content bound by digest: sha256 for plain content, an HMAC keyed with the
    confirm key for secrets."""
    if secret:
        return secret_digest(key, name, content)
    return "sha256:" + sha256_hex(content)


def display_string(s: str) -> Any:
    """A value as shown, or its digest and size when it is long."""
    if len(s) <= 256 and valid_utf8(s):
        return s
    b = raw_bytes(s)
    return {"sha256": sha256_hex(b), "bytes": len(b)}


def flag_value(f: Flag) -> str:
    """A flag's bound value: Value.String() for scalars, a JSON array for slices
    (order kept). Invalid UTF-8 is bound by the digest of its raw bytes."""
    if f.multiple:
        return canonical_json(claim_strings(f.slice_strings())).decode("utf-8")
    return claim_string(f.string())


def is_id_name(name: str) -> bool:
    """A name CLI-007 reserves for ids: id, iid, or one ending in _id."""
    return name in ("id", "iid") or name.endswith("_id")


def display_flag(f: Flag) -> Any:
    """A flag's value as shown in preview.inputs. An input named id, iid or *_id
    shows as a string whatever the flag's type, since ids are strings (CLI-007,
    P2): --iid 5 shows "iid":"5"."""
    if f.multiple:
        return [display_string(s) for s in f.slice_strings()]
    s = f.string()
    if is_id_name(snake(f.name)):
        return display_string(s)
    typ, _ = flag_type(f)
    if typ == "boolean":
        try:
            return parse_go_bool(s)
        except ValueError:
            pass
    elif typ in ("integer", "number"):
        try:
            float(s)
            return RawNumber(s)
        except ValueError:
            pass
    return display_string(s)


def label_for(c: Node, bound: str, file: str) -> str:
    """The parameter a file-bound value is named by: the literal flag when the
    command has one, else the file flag."""
    if c.flag(bound) is not None:
        return "--" + bound
    return "--" + file


def too_large(flag: str) -> Error:
    e = usage(
        "input_too_large",
        "--" + flag,
        f"The input given to --{flag} is larger than {MAX_FILE_INPUT_BYTES >> 20} MiB.",
    )
    e.details["next_action"] = "Use a smaller input."
    return e


class BindingMixin:
    """App methods for file inputs, resolved values and the invocation binding."""

    def read_file_input(self: App, cmd: Any, flag: str) -> bytes | None:
        """The content of a file-input flag declared in Meta.file_inputs ("-" reads
        stdin). The file is read once per run; the write gate binds the digest of
        exactly these bytes. None when the flag was not given. Always read file
        inputs through this method, never with open(), or the token would bind
        bytes that differ from the ones sent."""
        st, node = self._run_for(cmd)
        m = self.meta_for(node)
        if m is None or not m.file_inputs.get(flag):
            raise internal_error("read_file_input needs a flag declared in Meta.file_inputs.")
        fi = self._file_input(st, node, flag)
        if fi is None:
            return None
        return bytes(fi.content)

    def set_resolved(self: App, cmd: Any, flag: str, *values: str) -> None:
        """Replace a flag's value with its resolved form before write (for example a
        ref or URL resolved to an id), so equivalent inputs bind identically
        (CLI-054: bind resolved values, not input forms). Slice flags take every
        value. Only call it for flags the user gave; it keeps the flag marked as
        given."""
        _, node = self._run_for(cmd)
        f = node.flag(flag)
        if f is None or not values:
            raise internal_error("set_resolved needs a defined flag and at least one value.")
        try:
            f.replace([str(v) for v in values], self._click_ctx(node))
        except FlagValueError:
            raise usage(
                "invalid_value", "--" + flag, f"--{flag} has a value of the wrong type."
            ) from None

    def changed(self: App, cmd: Any, flag: str) -> bool:
        """Whether the flag was given on the command line (or resolved)."""
        _, node = self._run_for(cmd)
        f = node.flag(flag)
        return bool(f is not None and f.changed)

    def _file_input(self: App, st: RunState, node: Node, flag: str) -> FileInput | None:
        """Read (once) and cache the content behind a file flag."""
        if flag in st.files:
            return st.files[flag]
        f = node.flag(flag)
        if f is None or not f.changed:
            return None
        path = f.string()
        if path == "-":
            if st.stdin_flag and st.stdin_flag != flag:
                raise usage("conflicting_flags", "--" + flag, "Only one flag can read stdin (-).")
            st.stdin_flag = flag
            stream = self._stdin()
            try:
                src = getattr(stream, "buffer", stream)
                data = src.read(MAX_FILE_INPUT_BYTES + 1)
                if isinstance(data, str):
                    data = data.encode("utf-8")
            except (OSError, ValueError) as exc:
                raise io_error(
                    "stdin could not be read; nothing was sent.",
                    "Pass the input as a file instead.",
                    exc,
                ) from None
            if len(data) > MAX_FILE_INPUT_BYTES:
                raise too_large(flag)
            content = bytes(data)
        else:
            try:
                info = os.stat(path)
            except FileNotFoundError:
                e = usage(
                    "invalid_value", "--" + flag, f"The file given to --{flag} does not exist."
                )
                e.details["next_action"] = (
                    "Check the path; relative paths are relative to the current directory."
                )
                raise e from None
            except OSError as exc:
                raise io_error(
                    f"The file given to --{flag} could not be read; nothing was sent.",
                    "Check the file's permissions.",
                    exc,
                ) from None
            if stat.S_ISDIR(info.st_mode):
                raise usage(
                    "invalid_value", "--" + flag, f"--{flag} needs a file, not a directory."
                )
            if info.st_size > MAX_FILE_INPUT_BYTES:
                raise too_large(flag)
            try:
                with open(path, "rb") as fh:
                    content = fh.read(MAX_FILE_INPUT_BYTES + 1)
            except OSError as exc:
                raise io_error(
                    f"The file given to --{flag} could not be opened; nothing was sent.",
                    "Check the file's permissions.",
                    exc,
                ) from None
            if len(content) > MAX_FILE_INPUT_BYTES:
                raise too_large(flag)
        fi = FileInput(path=path, content=content)
        st.files[flag] = fi
        return fi

    def bind_invocation(self: App, st: RunState, c: Node, m: Meta, key: bytes) -> Binding:
        """The normalised invocation of the running command. key keys the digests of
        secret values (HMAC), so no unsalted hash of a secret is ever stored. When
        the tool declares settings, the active profile's name (whatever chose it) is
        bound as the input profile and every setting's resolved value as
        setting.<name> (CLI-054, CLI-101)."""
        b = Binding(command=c.path)
        bound_from_file: dict[str, str] = {}
        for file in sorted(m.file_inputs):
            bound_from_file.setdefault(m.file_inputs[file], file)
        for f in c.sorted_flags():
            if not f.changed or f.name in EXCLUDED_FROM_BINDING:
                continue
            if self.settings is not None and f.name == "profile" and f.param is None:
                continue  # aicli's root --profile: bound below as the active profile
            if f.name in m.file_inputs:
                bound = m.file_inputs[f.name]
                fi = self._file_input(st, c, f.name)
                assert fi is not None
                secret = m.is_secret(f.name) or m.is_secret(bound)
                shown: Any = {"sha256": sha256_hex(fi.content), "bytes": len(fi.content)}
                if secret:
                    shown = "***"
                b.add(
                    bound,
                    content_value(fi.content, secret, key, bound),
                    shown,
                    label_for(c, bound, f.name),
                )
                continue
            if f.name in bound_from_file:
                secret = m.is_secret(f.name)
                value = f.string()
                shown = "***" if secret else display_string(value)
                b.add(
                    f.name,
                    content_value(raw_bytes(value), secret, key, f.name),
                    shown,
                    "--" + f.name,
                )
                continue
            value = flag_value(f)
            if m.is_secret(f.name):
                value = secret_digest(key, f.name, value.encode("utf-8"))
                shown = "***"
            else:
                shown = display_flag(f)
            b.add(f.name, value, shown, "--" + f.name)
        if not st.positional_consumed and st.positionals:
            enc = canonical_json(claim_strings(st.positionals)).decode("utf-8")
            b.add("args", enc, [display_string(s) for s in st.positionals], "args")
        if self.settings is not None:
            self._bind_settings(b, key)
        try:
            body = canonical_json(b.digests)
        except (TypeError, ValueError):
            raise internal_error("The invocation could not be encoded.") from None
        b.inv = "sha256:" + sha256_hex(body)
        return b

    def _bind_settings(self: App, b: Binding, key: bytes) -> None:
        """The active profile as input profile ("" when none is active; the
        requested one a Meta.new_profile command creates, P6) and each declared
        setting's resolved value as setting.<name>, or its error reason when it
        cannot be resolved: a secret one by keyed digest (shown as ***)."""
        ps = self._profile_state()
        name = ps.name or ps.unknown or ""
        b.add("profile", claim_string(name), display_string(name), "profile")
        for s in self.settings or []:
            bound = "setting." + s.name
            try:
                value, _ = self.setting(s.name)
            except Error as e:
                value = "error: " + e.message
            if s.secret:
                b.add(bound, secret_digest(key, bound, raw_bytes(value)), "***", bound)
            else:
                b.add(bound, claim_string(value), display_string(value), bound)
