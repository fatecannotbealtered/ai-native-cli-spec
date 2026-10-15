# Vendored from ai-native-cli-spec template/python/aicli.
# Do not edit in tool repos; run scripts/sync-spec.js.
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sean Guo

"""The library's view of a click tree: nodes (commands and groups) and flags
with the parsing rules of the Go library (cobra and pflag), so both libraries
accept the same command lines, find the same command and bind the same values.

The tool's click objects are never modified: an App builds its own nodes over
them (and over the built-in commands it creates), and keeps every parsed value
here for the duration of one run.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import click

from ._util import (
    go_bool_string,
    go_format_g,
    go_value_string,
    parse_go_bool,
    parse_go_int,
    read_csv_record,
    write_csv_record,
)

if TYPE_CHECKING:
    from .registry import Meta

try:  # click >= 8.3 marks a parameter without a value with a sentinel
    from click._utils import UNSET as CLICK_UNSET  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - older click
    CLICK_UNSET = object()


# ---------------------------------------------------------------- flag errors


class FlagParseError(Exception):
    """A pflag parse error: not_exist, value_required, invalid_value or
    invalid_syntax."""

    def __init__(
        self,
        kind: str,
        *,
        name: str = "",
        shorthands: str = "",
        flag: Flag | None = None,
        value: str = "",
        specified: str = "",
    ) -> None:
        super().__init__(kind)
        self.kind = kind
        self.name = name
        self.shorthands = shorthands
        self.flag = flag
        self.value = value
        self.specified = specified


class FlagValueError(ValueError):
    """A flag value that does not convert to the flag's type."""


# ---------------------------------------------------------------- flags

# kinds: bool (NoOptDefVal "true"), flag (a click flag_value), count, string, int,
# float, value (any other click type: Choice, Path, ...). A multiple flag holds a
# list of its element kind (string, int, float, bool or value).


class Flag:
    """One flag of a command, with pflag's semantics and this run's value."""

    def __init__(
        self,
        name: str,
        *,
        kind: str = "string",
        shorthand: str = "",
        multiple: bool = False,
        usage: str = "",
        hidden: bool = False,
        required: bool = False,
        def_value: str = "",
        default: Any = None,
        param: click.Parameter | None = None,
        owner: Node | None = None,
        split: bool = True,
        off_names: tuple[str, ...] = (),
        unsupported: str = "",
    ) -> None:
        self.name = name
        self.kind = kind
        self.shorthand = shorthand
        self.multiple = multiple
        self.usage = usage
        self.hidden = hidden
        self.required = required
        self.def_value = def_value  # pflag DefValue: the default as text
        self.default = default
        self.param = param
        self.owner = owner
        self.split = split  # multiple string values split on commas (pflag StringSlice)
        self.off_names = off_names  # click's --no-x names of a boolean flag
        self.unsupported = unsupported
        self.no_opt_def_val = ""
        if kind == "bool" and not multiple:
            self.no_opt_def_val = "true"
        elif kind == "count":
            self.no_opt_def_val = "+1"
        elif kind == "flag" and param is not None:
            self.no_opt_def_val = go_value_string(getattr(param, "flag_value", None))
        self.reset()

    # ---------------------------------------------------------------- state

    def reset(self) -> None:
        self.changed = False
        self.value: Any = self._initial_value()
        self.items: list[Any] = []

    def _initial_value(self) -> Any:
        if self.multiple:
            return None
        if self.kind == "bool":
            return bool(self.default) if self.param is None else self.default
        if self.kind == "count":
            return 0
        return self.default

    # ---------------------------------------------------------------- set / string

    def convert(self, raw: str, ctx: click.Context | None) -> Any:
        """One value converted to the flag's (element) type."""
        if self.kind in ("bool",) or (self.multiple and self.kind == "bool"):
            try:
                return parse_go_bool(raw)
            except ValueError as exc:
                raise FlagValueError(str(exc)) from exc
        if self.kind == "flag" and raw == self.no_opt_def_val and self.param is not None:
            return self.param.flag_value  # type: ignore[attr-defined]
        if self.param is not None:
            try:
                return self.param.type.convert(raw, self.param, ctx)
            except click.UsageError as exc:
                raise FlagValueError(exc.format_message()) from exc
            except (ValueError, TypeError) as exc:
                raise FlagValueError(str(exc)) from exc
        if self.kind == "int":
            try:
                return parse_go_int(raw)
            except ValueError as exc:
                raise FlagValueError(str(exc)) from exc
        if self.kind == "float":
            try:
                return float(raw)
            except ValueError as exc:
                raise FlagValueError(str(exc)) from exc
        return raw

    def set(self, raw: str, ctx: click.Context | None) -> None:
        """pflag Value.Set: parse raw and mark the flag given."""
        if self.multiple:
            parts = self._split(raw)
            values = [self.convert(p, ctx) for p in parts]
            if not self.changed:
                self.items = values
            else:
                self.items = self.items + values
        elif self.kind == "count":
            if raw == "+1":
                self.value = (self.value or 0) + 1
            else:
                try:
                    self.value = parse_go_int(raw)
                except ValueError as exc:
                    raise FlagValueError(str(exc)) from exc
        else:
            self.value = self.convert(raw, ctx)
        self.changed = True

    def set_off(self) -> None:
        """click's --no-x for a boolean flag."""
        self.value = False
        self.changed = True

    def _split(self, raw: str) -> list[str]:
        if self.kind in ("int", "float"):
            return raw.split(",") if raw != "" else []
        if not self.split:
            return [raw]
        try:
            return read_csv_record(raw)
        except ValueError as exc:
            raise FlagValueError(str(exc)) from exc

    def replace(self, values: list[str], ctx: click.Context | None) -> None:
        """pflag SliceValue.Replace (or Set of the first value for scalars)."""
        if self.multiple:
            self.items = [self.convert(v, ctx) for v in values]
        else:
            self.value = self.convert(values[0], ctx)
        self.changed = True

    def slice_strings(self) -> list[str]:
        """pflag SliceValue.GetSlice: each element as text."""
        return [_elem_string(v) for v in self.items]

    def string(self) -> str:
        """pflag Value.String()."""
        if self.multiple:
            return "[" + write_csv_record(self.slice_strings()) + "]"
        if self.kind == "bool":
            return go_bool_string(bool(self.value))
        if self.value is None:
            return ""
        return _elem_string(self.value)

    def type_name(self) -> str:
        """pflag Value.Type() of the flag."""
        elem = self.elem_type_name()
        if self.multiple:
            return {"bool": "boolSlice", "int": "intSlice", "float64": "float64Slice"}.get(
                elem, "stringSlice"
            )
        return elem

    def elem_type_name(self) -> str:
        if self.kind == "count":
            return "count"
        if self.kind == "bool":
            return "bool"
        if self.kind == "int":
            return "int"
        if self.kind == "float":
            return "float64"
        if self.param is not None and isinstance(self.param.type, click.types.BoolParamType):
            return "bool"
        return "string"

    def click_value(self) -> Any:
        """The value handed to click for this run (before click processing)."""
        if self.multiple:
            return tuple(self.items)
        return self.value


def _elem_string(v: Any) -> str:
    if isinstance(v, float):
        return go_format_g(v)
    if isinstance(v, _dt.timedelta):
        return go_value_string(v)
    return go_value_string(v)


def flag_name_of(param: click.Option) -> str:
    """The long flag name of a click option, without dashes."""
    for opt in param.opts:
        if opt.startswith("--"):
            return opt[2:]
    return (param.name or "").replace("_", "-")


def shorthand_of(param: click.Option) -> str:
    for opt in param.opts:
        if len(opt) == 2 and opt[0] == "-" and opt[1] != "-":
            return opt[1]
    return ""


def flag_from_option(param: click.Option, owner: Node, meta_type: str = "") -> Flag:
    """The library flag for a click option."""
    unsupported = ""
    multiple = bool(param.multiple)
    if param.nargs not in (1, -1) and not multiple:
        unsupported = "nargs"
    if getattr(param, "prompt", None):
        unsupported = "prompt"
    t = param.type
    if getattr(param, "count", False):
        kind = "count"
    elif param.is_flag and getattr(param, "is_bool_flag", False):
        kind = "bool"
    elif param.is_flag:
        kind = "flag"
    elif isinstance(t, click.types.IntParamType):
        kind = "int"
    elif isinstance(t, click.types.FloatParamType):
        kind = "float"
    elif isinstance(t, click.types.BoolParamType):
        kind = "bool" if multiple else "value"
    elif isinstance(t, click.types.StringParamType):
        kind = "string"
    else:
        kind = "value"
    split = not isinstance(t, (click.Path, click.File)) and meta_type not in ("path", "json")
    off = tuple(o[2:] for o in getattr(param, "secondary_opts", []) if o.startswith("--"))
    default = getattr(param, "default", None)
    if default is CLICK_UNSET:
        default = None
    if callable(default):
        default_text, default_value = "", None
    else:
        default_text, default_value = _def_value(default, multiple, kind), default
    return Flag(
        flag_name_of(param),
        kind=kind,
        shorthand=shorthand_of(param),
        multiple=multiple,
        usage=(param.help or "").strip() if isinstance(param, click.Option) else "",
        hidden=bool(getattr(param, "hidden", False)),
        required=bool(param.required),
        def_value=default_text,
        default=None if multiple else default_value,
        param=param,
        owner=owner,
        split=split,
        off_names=off,
        unsupported=unsupported,
    )


def _def_value(default: Any, multiple: bool, kind: str) -> str:
    """pflag's DefValue for a click default."""
    if default is None:
        return ""
    if multiple:
        try:
            items = list(default)
        except TypeError:
            items = [default]
        if not items:
            return "[]"
        return "[" + write_csv_record([_elem_string(x) for x in items]) + "]"
    if kind == "bool":
        return go_bool_string(bool(default))
    return _elem_string(default)


# ---------------------------------------------------------------- nodes


class Node:
    """A command or group of the tree: the tool's click object or a built-in."""

    def __init__(
        self,
        name: str,
        cmd: click.Command | None,
        parent: Node | None,
        *,
        builtin: bool = False,
        internal: bool = False,
    ) -> None:
        self.name = name
        self.cmd = cmd
        self.parent = parent
        self.builtin = builtin
        self.internal = internal  # the help command (cobra's own)
        self.children: list[Node] = []
        self.aliases: list[str] = []
        self.short = ""
        self.long = ""
        self.hidden = bool(getattr(cmd, "hidden", False)) if cmd is not None else False
        self.local: dict[str, Flag] = {}
        self.persistent: dict[str, Flag] = {}
        self.arguments: list[click.Argument] = []
        self._merged: dict[str, Flag] | None = None

    def __repr__(self) -> str:
        return f"<Node {self.command_path!r}>"

    # ---------------------------------------------------------------- tree

    @property
    def root(self) -> Node:
        n = self
        while n.parent is not None:
            n = n.parent
        return n

    @property
    def path(self) -> str:
        """The command path without the tool name ("note create")."""
        words = []
        n: Node | None = self
        while n is not None and n.parent is not None:
            words.append(n.name)
            n = n.parent
        return " ".join(reversed(words))

    @property
    def command_path(self) -> str:
        """cobra's CommandPath: the path with the tool name ("notes note create")."""
        p = self.path
        return self.root.name + (" " + p if p else "")

    def has_subcommands(self) -> bool:
        return any(not c.internal for c in self.children)

    def ancestors(self) -> Iterator[Node]:
        n = self.parent
        while n is not None:
            yield n
            n = n.parent

    def is_under(self, ancestor: Node) -> bool:
        return any(p is ancestor for p in self.ancestors())

    def has_alias(self, word: str) -> bool:
        return word in self.aliases

    def child(self, word: str) -> Node | None:
        """A direct subcommand by name or alias (cobra's own help included)."""
        for sub in self.children:
            if sub.name == word or sub.has_alias(word):
                return sub
        return None

    def walk(self) -> Iterator[Node]:
        yield self
        for c in self.children:
            yield from c.walk()

    # ---------------------------------------------------------------- flags

    def flags(self) -> dict[str, Flag]:
        """Every flag that applies: local, then persistent from here up (cobra's
        Flags() after merging the parents' persistent flags)."""
        if self._merged is None:
            merged: dict[str, Flag] = {}
            for name, f in self.local.items():
                merged.setdefault(name, f)
            for name, f in self.persistent.items():
                merged.setdefault(name, f)
            for p in self.ancestors():
                for name, f in p.persistent.items():
                    merged.setdefault(name, f)
            self._merged = merged
        return self._merged

    def sorted_flags(self) -> list[Flag]:
        """Flags in pflag's VisitAll order (by name)."""
        return [self.flags()[k] for k in sorted(self.flags())]

    def local_flags(self) -> dict[str, Flag]:
        out = dict(self.local)
        for name, f in self.persistent.items():
            out.setdefault(name, f)
        return out

    def inherited_flags(self) -> dict[str, Flag]:
        local = self.local_flags()
        out: dict[str, Flag] = {}
        for p in self.ancestors():
            for name, f in p.persistent.items():
                if name not in local:
                    out.setdefault(name, f)
        return out

    def flag(self, name: str) -> Flag | None:
        """cobra lookupFlag: a long flag or a one-letter shorthand, inherited
        flags included."""
        if not name:
            return None
        f = self.flags().get(name)
        if f is not None:
            return f
        if len(name) == 1:
            return self.shorthand_flag(name)
        return None

    def shorthand_flag(self, s: str) -> Flag | None:
        for f in self.flags().values():
            if f.shorthand == s:
                return f
        return None

    def off_flag(self, name: str) -> Flag | None:
        """The boolean flag a click --no-x name switches off."""
        for f in self.flags().values():
            if name in f.off_names:
                return f
        return None

    def add_local(self, f: Flag) -> None:
        self.local.setdefault(f.name, f)
        self._merged = None


# ---------------------------------------------------------------- positionals


class UsePos:
    """A positional argument of a command (<name>, [name], <name...>)."""

    def __init__(self, name: str, required: bool, variadic: bool, count: int = 1) -> None:
        self.name = name
        self.required = required
        self.variadic = variadic
        self.count = count


def positionals_of(node: Node, meta: Meta | None) -> list[UsePos]:
    """The positionals a command takes: its click arguments, or the one
    Meta.positional fills when it declares none."""
    out = []
    for a in node.arguments:
        variadic = a.nargs == -1
        out.append(UsePos(a.name or "arg", bool(a.required), variadic, 1 if variadic else a.nargs))
    if not out and meta is not None and meta.positional:
        # The positional stands in for its flag: <name> when the flag is required
        # (Go tools write `Use: "get <id>"`), [name] when it is optional.
        f = node.flag(meta.positional)
        if f is not None:
            out.append(UsePos(meta.positional, f.required, False))
    return out


def usage_tokens(pos: list[UsePos]) -> str:
    """The positionals as the group help lists them: <name> or [name]."""
    return " ".join(f"<{p.name}>" if p.required else f"[{p.name}]" for p in pos)


# ---------------------------------------------------------------- parsing (pflag)


def parse_flags(node: Node, args: list[str], ctx: click.Context | None) -> list[str]:
    """Parse args against node's flags with pflag's rules (interspersed
    flags, --name=value, -n value, -nvalue, combined short flags, "--"). Returns
    the positional arguments; raises FlagParseError."""
    flags = node.flags()
    out: list[str] = []
    i = 0
    while i < len(args):
        s = args[i]
        i += 1
        if len(s) == 0 or s[0] != "-" or len(s) == 1:
            out.append(s)
            continue
        if s[1] == "-":
            if len(s) == 2:  # "--" terminates the flags
                out.extend(args[i:])
                break
            i = _parse_long(node, flags, s, args, i, ctx)
        else:
            i = _parse_short(node, s, args, i, ctx)
    return out


def _parse_long(
    node: Node, flags: dict[str, Flag], s: str, args: list[str], i: int, ctx: click.Context | None
) -> int:
    name = s[2:]
    if len(name) == 0 or name[0] in "-=":
        raise FlagParseError("invalid_syntax", specified=s)
    name, eq, value = name.partition("=")
    f = flags.get(name)
    if f is None:
        off = node.off_flag(name)
        if off is not None and not eq:
            off.set_off()
            return i
        raise FlagParseError("not_exist", name=name)
    if eq:
        pass
    elif f.no_opt_def_val:
        value = f.no_opt_def_val
    elif i < len(args):
        value = args[i]
        i += 1
    else:
        raise FlagParseError("value_required", flag=f, specified=name)
    try:
        f.set(value, ctx)
    except FlagValueError as exc:
        raise FlagParseError("invalid_value", flag=f, value=value, specified=name) from exc
    return i


def _parse_short(node: Node, s: str, args: list[str], i: int, ctx: click.Context | None) -> int:
    shorthands = s[1:]
    while shorthands:
        c = shorthands[0]
        f = node.shorthand_flag(c)
        if f is None:
            raise FlagParseError("not_exist", name=c, shorthands=shorthands)
        rest = shorthands[1:]
        if len(shorthands) > 2 and shorthands[1] == "=":
            value = shorthands[2:]
            rest = ""
        elif f.no_opt_def_val:
            value = f.no_opt_def_val
        elif len(shorthands) > 1:
            value = shorthands[1:]
            rest = ""
        elif i < len(args):
            value = args[i]
            i += 1
        else:
            raise FlagParseError("value_required", flag=f, specified=c)
        try:
            f.set(value, ctx)
        except FlagValueError as exc:
            raise FlagParseError("invalid_value", flag=f, value=value, specified=c) from exc
        shorthands = rest
    return i


# ---------------------------------------------------------------- finding (cobra)


def _has_no_opt(name: str, flags: dict[str, Flag], node: Node) -> bool:
    f = flags.get(name)
    if f is None:
        return node.off_flag(name) is not None
    return f.no_opt_def_val != ""


def _short_has_no_opt(name: str, node: Node) -> bool:
    if not name:
        return False
    f = node.shorthand_flag(name[0])
    return f is not None and f.no_opt_def_val != ""


def strip_flags(args: list[str], node: Node) -> list[str]:
    """cobra's stripFlags: the words of args that are not flags or flag values."""
    if not args:
        return args
    flags = node.flags()
    commands: list[str] = []
    rest = list(args)
    while rest:
        s = rest.pop(0)
        if s == "--":
            break
        if (s.startswith("--") and "=" not in s and not _has_no_opt(s[2:], flags, node)) or (
            s.startswith("-")
            and "=" not in s
            and len(s) == 2
            and not _short_has_no_opt(s[1:], node)
        ):
            if len(rest) <= 1:
                break
            rest.pop(0)
            continue
        if s != "" and not s.startswith("-"):
            commands.append(s)
    return commands


def args_minus_first(args: list[str], node: Node, x: str) -> list[str]:
    """cobra's argsMinusFirstX: args without the first command word x."""
    if not args:
        return args
    flags = node.flags()
    pos = 0
    while pos < len(args):
        s = args[pos]
        if s == "--":
            break
        if (s.startswith("--") and "=" not in s and not _has_no_opt(s[2:], flags, node)) or (
            s.startswith("-")
            and "=" not in s
            and len(s) == 2
            and not _short_has_no_opt(s[1:], node)
        ):
            pos += 2
            continue
        if not s.startswith("-") and s == x:
            return args[:pos] + args[pos + 1 :]
        pos += 1
    return args


def find(root: Node, args: list[str]) -> tuple[Node, list[str]]:
    """cobra's Find: the deepest command the words of args name, and the args
    left for it to parse."""
    node, rest = root, list(args)
    while True:
        words = strip_flags(rest, node)
        if not words:
            return node, rest
        nxt = node.child(words[0])
        if nxt is None:
            return node, rest
        rest = args_minus_first(rest, node, words[0])
        node = nxt
