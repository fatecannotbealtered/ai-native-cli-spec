# Vendored from ai-native-cli-spec template/python/aicli.
# Do not edit in tool repos; run scripts/sync-spec.js.
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sean Guo

"""The registry (CLI-020): Meta per command, output schemas, the reference
(full, --index, --command), --help, the Skill command map, deprecation (CLI-081)
and the tree checks."""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import click

from ._tree import Flag, Node, positionals_of, usage_tokens
from ._util import Ordered, go_json, go_quote, go_value_text, parse_go_bool, parse_go_int
from .contract_gen import (
    CORE_CODES,
    DANGEROUS_EFFECTS,
    EFFECT_VALUES,
    EXIT_CODE_MEANINGS,
    FORMAT_VALUES,
    GLOBAL_FLAGS,
    INDEX_BUDGET_CHARS,
    MATURITY_VALUES,
    MINIMUM_RISK_TIER,
    PARAM_TYPES,
    RISK_TIER_VALUES,
    SCHEMA_SHAPES,
    SCHEMA_VERSION,
    SPEC_VERSION,
)
from .errors import Error, usage

if TYPE_CHECKING:
    from .app import App

# The contract's effects (CLI-025). Any effect other than OBSERVE makes a command a write.
OBSERVE = "observe"
CHANGE = "change"
DELIVER = "deliver"
CONFIGURE = "configure"
ARTIFACT = "artifact"
PERSIST = "persist"

# Maturity levels, lowest first (CLI-027, VER-005).
DRAFT = "draft"
EXPERIMENTAL = "experimental"
VERIFIED = "verified"
STABLE = "stable"


@dataclass
class ParamDoc:
    """Extra reference metadata for one flag (see Meta.params): type overrides
    the derived type (path, json, enum), enum lists allowed values (checked
    before the command runs) and unit names the unit of a quantity."""

    type: str = ""
    enum: list[str] = field(default_factory=list)
    unit: str = ""


@dataclass
class Meta:
    """A command's registry entry (CLI-020). Register it for every leaf."""

    summary: str = ""
    effects: list[str] = field(default_factory=list)
    dangerous: bool = False
    maturity: str = ""
    evidence: list[str] = field(default_factory=list)
    output: str = ""  # name of a schema registered with App.schema
    error_codes: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    positional: str = ""  # flag filled by a single positional argument
    # file flag -> bound value name ("body-file" -> "body"); value "-" reads stdin
    file_inputs: dict[str, str] = field(default_factory=dict)
    secrets: list[str] = field(
        default_factory=list
    )  # flags (or bound names) whose values are secret
    aliases: list[str] = field(default_factory=list)
    batch_flag: str = ""  # plural target flag for write_batch
    default_fields: list[str] = field(default_factory=list)  # replaces the schema's defaults
    raw_output: bool = False  # the command supports --format raw
    params: dict[str, ParamDoc] = field(default_factory=dict)
    # Flag groups (click has none; cobra's MarkFlagsOneRequired and friends): each
    # entry is a list of flag names. one_required: at least one of them is given;
    # mutually_exclusive: at most one; required_together: all or none.
    one_required: list[list[str]] = field(default_factory=list)
    mutually_exclusive: list[list[str]] = field(default_factory=list)
    required_together: list[list[str]] = field(default_factory=list)
    # Deprecation (CLI-081): deprecated names the replacement ("use `mr update
    # --label`"); deprecated_flags maps a flag name to its replacement text. Help and
    # reference say so, every use adds a deprecated notice, and the Skill command map
    # leaves a deprecated command out.
    deprecated: str = ""
    deprecated_flags: dict[str, str] = field(default_factory=dict)
    # renamed_flags keeps old flag names working (old -> new, Q10): --old is read as
    # --new (and binds as --new), using it adds the notice "`--old` is deprecated:
    # use `--new`.", giving both is E_USAGE, and help leaves it out while reference
    # lists it as a deprecated parameter. The command defines only --new.
    renamed_flags: dict[str, str] = field(default_factory=dict)
    # flag_aliases accepts other long flag names on this command only (alias ->
    # canonical, Q11), after App.flag_aliases: an alias App.flag_aliases maps on this
    # command wins. The canonical flag binds; reference does not list the alias.
    flag_aliases: dict[str, str] = field(default_factory=dict)
    # stream_item names the schema of the items an NDJSON stream writes (App.stream,
    # Q23) when they are not the output schema's objects (a log's lines): items are
    # projected with it and take its untrusted fields.
    stream_item: str = ""
    # allow_unknown_profile lets the command run under a profile (--profile or
    # <PREFIX>_PROFILE) that the settings file does not have (CLI-101, P6, Q4): `auth
    # login --profile lab` creates it, `auth logout --profile gone` cleans up after a
    # deleted one. The unknown-profile refusal is skipped, App.setting reads the flag,
    # the environment and the default only, App.profile returns the requested name,
    # and that name is still bound into the confirm token. apply creates the profile
    # with App.set_profile_settings.
    allow_unknown_profile: bool = False

    def is_write(self) -> bool:
        """Whether any declared effect is not observe."""
        return any(e != OBSERVE for e in self.effects)

    def is_dangerous(self) -> bool:
        """Whether the command needs --dangerous (declared, or deliver/configure)."""
        return self.dangerous or any(e in DANGEROUS_EFFECTS for e in self.effects)

    def has_effect(self, *names: str) -> bool:
        return any(e in names for e in self.effects)

    def effect_strings(self) -> list[str]:
        return list(self.effects)

    def is_secret(self, name: str) -> bool:
        return name in self.secrets


@dataclass
class SchemaDef:
    """An output schema in the reference catalog (CLI-020). shape is object,
    array or page; fields lists the fields of the object (or of each item);
    default_fields are returned when --fields is not given; untrusted lists the
    fields that carry external content."""

    shape: str = ""
    fields: list[str] = field(default_factory=list)
    default_fields: list[str] = field(default_factory=list)
    untrusted: list[str] = field(default_factory=list)


class Registry:
    def __init__(self) -> None:
        self.meta: dict[click.Command, Meta] = {}
        self.groups: dict[click.Command, str] = {}
        self.group_aliases: dict[click.Command, list[str]] = {}
        self.schemas: dict[str, SchemaDef] = {}
        self.order: list[click.Command] = []  # leaves in the order they were registered
        # registration defects (a schema registered twice, Q18; a shortcut word
        # mapped to two paths, Q6), which the tree check reports
        self.problems: list[str] = []


@dataclass
class ParamEntry:
    name: str
    kind: str
    type: str
    required: bool
    multiple: bool = False
    enum: list[str] = field(default_factory=list)
    default: Any = None
    unit: str = ""
    secret: bool = False
    file_input: bool = False
    description: str = ""

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "type": self.type,
            "required": self.required,
        }
        if self.multiple:
            out["multiple"] = True
        if self.enum:
            out["enum"] = list(self.enum)
        if self.default is not None:
            out["default"] = self.default
        if self.unit:
            out["unit"] = self.unit
        if self.secret:
            out["secret"] = True
        if self.file_input:
            out["file_input"] = True
        if self.description:
            out["description"] = self.description
        return out


def is_global_flag(name: str) -> bool:
    """The contract's global flags, help and the root version flag."""
    return name in GLOBAL_FLAGS or name in ("help", "version")


def is_global(f: Flag) -> bool:
    """A global flag: the contract's, help, version, or the --profile root flag
    aicli registers for App.settings (CLI-101), which reference lists with the
    global flags and not in each command's params."""
    return is_global_flag(f.name) or (f.name == "profile" and f.param is None)


def flag_type(f: Flag) -> tuple[str, bool]:
    """A flag's reference param type."""
    t = f.type_name()
    if t == "bool":
        return "boolean", False
    if t in ("int", "int8", "int16", "int32", "int64", "uint", "count"):
        return "integer", False
    if t in ("float32", "float64"):
        return "number", False
    if t in ("stringSlice", "stringArray"):
        return "string", True
    if t in ("intSlice", "int32Slice", "int64Slice", "uintSlice"):
        return "integer", True
    if t in ("float32Slice", "float64Slice"):
        return "number", True
    if t == "boolSlice":
        return "boolean", True
    return "string", False


def typed_default(f: Flag, typ: str) -> Any:
    """A flag's default as a JSON value, or None when it has none."""
    d = f.def_value
    if d in ("", "[]"):
        return None
    if typ == "boolean":
        try:
            return True if parse_go_bool(d) else None
        except ValueError:
            return None
    if typ == "integer":
        try:
            return parse_go_int(d)
        except ValueError:
            return d
    if typ == "number":
        try:
            return float(d)
        except ValueError:
            return d
    return d


def _choices(f: Flag) -> list[str]:
    if f.param is not None and isinstance(f.param.type, click.Choice):
        return [str(c) for c in f.param.type.choices]
    return []


def tier_rank(t: str) -> int:
    return RISK_TIER_VALUES.index(t) if t in RISK_TIER_VALUES else -1


def deprecated_text(text: str, original: str) -> str:
    """A summary or flag description of something deprecated (CLI-081):
    "Deprecated: <text>. <original>" (text trimmed, a trailing "." dropped)."""
    return ("Deprecated: " + replacement_text(text) + ". " + original).strip()


def replacement_text(text: str) -> str:
    """A deprecation's replacement text as shown: spaces trimmed, then a trailing
    "." dropped (D10)."""
    return text.strip().removesuffix(".").strip()


def schema_entry_of(s: SchemaDef) -> dict[str, Any]:
    """A schema as the reference catalog shows it."""
    return {
        "shape": s.shape,
        "fields": list(s.fields),
        "default_fields": list(s.default_fields),
        "untrusted_fields": list(s.untrusted),
    }


def with_twins(secrets: list[str], file_inputs: dict[str, str]) -> list[str]:
    """Meta.secrets with each secret's twin added (B2): the literal flag a secret
    file flag stands in for, and the file flag of a secret literal. One notion of a
    secret flag serves binding, previews, records, reference, suggestions,
    conformance and redaction."""
    out = list(secrets)
    changed = True
    while changed:
        changed = False
        for file in sorted(file_inputs):
            bound = file_inputs[file]
            if (file in out) != (bound in out):
                out.append(bound if file in out else file)
                changed = True
    return out


class RegistryMixin:
    """App methods for the registry, reference, help and tree checks."""

    # ---------------------------------------------------------------- registration

    def register(self: App, cmd: click.Command, m: Meta) -> None:
        """Attach metadata to a leaf command. Call it for every leaf."""
        cp = dataclasses.replace(
            m,
            effects=list(m.effects),
            evidence=list(m.evidence),
            error_codes=list(m.error_codes),
            examples=list(m.examples),
            file_inputs=dict(m.file_inputs),
            secrets=list(m.secrets),
            aliases=list(m.aliases),
            default_fields=list(m.default_fields),
            params=dict(m.params),
            deprecated_flags=dict(m.deprecated_flags),
            renamed_flags=dict(m.renamed_flags),
            flag_aliases=dict(m.flag_aliases),
        )
        if not cp.maturity:
            cp.maturity = DRAFT
        if not cp.summary:
            cp.summary = _short_of(cmd)
        cp.secrets = with_twins(cp.secrets, cp.file_inputs)
        if cmd not in self._reg.meta:
            self._reg.order.append(cmd)
        self._reg.meta[cmd] = cp

    def group(
        self: App, cmd: click.Command, summary: str = "", aliases: list[str] | None = None
    ) -> None:
        """Register a command group (a noun), its summary and its aliases."""
        if not summary:
            summary = _short_of(cmd)
        self._reg.groups[cmd] = summary
        if aliases:
            self._reg.group_aliases[cmd] = list(aliases)

    def schema(self: App, name: str, s: SchemaDef) -> None:
        """Register an output schema under name. A name is registered once: a second
        call is a defect the tree check reports (Q18), and the first definition
        stays."""
        if name in self._reg.schemas:
            self._reg.problems.append(
                f"App.schema({go_quote(name)}) was called twice; register each schema name once "
                "(the first definition is kept)"
            )
            return
        self._reg.schemas[name] = s

    def add_shortcut(self: App, word: str, *path: str) -> None:
        """Map a word typed at the root to a command path (CLI-022, Q6), as
        App.shortcuts does: add_shortcut("branch", "repo", "branch"). A word already
        mapped to a different path is a defect the tree check reports, and the first
        mapping stays; the same mapping again is harmless."""
        if self.shortcuts is None:
            self.shortcuts = {}
        have = self.shortcuts.get(word)
        if have is not None:
            if list(have) != list(path):
                self._reg.problems.append(
                    f"App.add_shortcut({go_quote(word)}) maps it to {go_quote(' '.join(path))}, "
                    f"but it already maps to {go_quote(' '.join(have))}; a shortcut word names "
                    "one path (the first is kept)"
                )
            return
        self.shortcuts[word] = list(path)

    def meta_for(self: App, node: Node | None) -> Meta | None:
        if node is None or node.cmd is None:
            return None
        return self._reg.meta.get(node.cmd)

    def schema_of(self: App, m: Meta | None) -> SchemaDef:
        if m is None:
            return SchemaDef()
        return self._reg.schemas.get(m.output, SchemaDef())

    # ---------------------------------------------------------------- tree queries

    def commands(self: App) -> list[Node]:
        """Every visible leaf command in path order."""
        out = []
        for c in self._root.walk():
            if c is self._root or c.internal or c.hidden or c.has_subcommands():
                continue
            if any(p.hidden for p in c.ancestors()):
                continue
            out.append(c)
        out.sort(key=lambda n: n.path)
        return out

    def command_by_path(self: App, path: str) -> Node | None:
        """A leaf or group by its path without the tool name."""
        c = self._root
        for w in path.split():
            nxt = c.child(w)
            if nxt is None or nxt.internal:
                return None
            c = nxt
        return c

    def node_for(self: App, cmd: Any) -> Node | None:
        """The node of a click command (or context)."""
        if isinstance(cmd, click.Context):
            cmd = cmd.command
        for n in self._root.walk():
            if n.cmd is cmd:
                return n
        return None

    def top_level_names(self: App) -> list[str]:
        return sorted(c.name for c in self._root.children if not c.internal and not c.hidden)

    # ---------------------------------------------------------------- params

    def param_for(self: App, m: Meta | None, f: Flag) -> ParamEntry:
        typ, multiple = flag_type(f)
        p = ParamEntry(
            name=f.name,
            kind="flag",
            type=typ,
            multiple=multiple,
            required=f.required,
            description=f.usage,
        )
        choices = _choices(f)
        doc = m.params.get(f.name) if m is not None else None
        if m is not None and f.name in m.file_inputs:
            p.type = "path"
            p.file_input = True
        if doc is not None:
            if doc.type:
                p.type = doc.type
            if doc.enum:
                p.enum = list(doc.enum)
                if not doc.type and not multiple:
                    p.type = "enum"
            p.unit = doc.unit
        elif choices:
            p.enum = choices
            if not multiple:
                p.type = "enum"
        if m is not None and (
            m.is_secret(f.name) or (p.file_input and m.is_secret(m.file_inputs[f.name]))
        ):
            p.secret = True
        # The batch flag must name at least one target (write_batch refuses an empty
        # batch), so it is required even though click does not enforce it.
        if m is not None and m.batch_flag and f.name == m.batch_flag:
            p.required = True
        if not p.required and not p.secret and not p.file_input:
            p.default = typed_default(f, p.type)
        if m is not None and replacement_text(m.deprecated_flags.get(f.name) or ""):
            p.description = deprecated_text(m.deprecated_flags[f.name], p.description)
        return p

    def params(self: App, c: Node, with_renamed: bool = False) -> list[ParamEntry]:
        """A command's params: positionals first, then flags by name. with_renamed
        adds the old names of renamed flags as deprecated parameters, which reference
        lists and help does not (Q10)."""
        m = self.meta_for(c)
        out: list[ParamEntry] = []
        pos_flag = m.positional if m is not None else ""
        used = False
        for p in positionals_of(c, m):
            entry = ParamEntry(
                name=p.name,
                kind="positional",
                type="string",
                required=p.required,
                multiple=p.variadic,
            )
            if pos_flag and not used:
                f = c.flag(pos_flag)
                if f is not None:
                    fe = self.param_for(m, f)
                    entry.name = pos_flag
                    entry.type, entry.enum, entry.unit, entry.secret = (
                        fe.type,
                        fe.enum,
                        fe.unit,
                        fe.secret,
                    )
                    entry.description = (fe.description + " (or --" + pos_flag + ")").strip()
                    entry.required = p.required or fe.required
                    used = True
            out.append(entry)
        flags = []
        for f in c.sorted_flags():
            if f.hidden or is_global(f) or (used and f.name == pos_flag):
                continue
            flags.append(self.param_for(m, f))
        if with_renamed and m is not None:
            for old in sorted(m.renamed_flags):
                f = c.flag(m.renamed_flags[old])
                if f is None or c.flag(old) is not None:
                    continue  # the tree check reports it
                p = self.param_for(m, f)
                p.name, p.required, p.default = old, False, None
                p.description = deprecated_text("use `--" + f.name + "`", f.usage)
                flags.append(p)
        flags.sort(key=lambda e: e.name)
        return out + flags

    def global_params(self: App) -> list[ParamEntry]:
        """The global control flags for the reference, and --profile when the tool
        declares settings (CLI-101)."""
        out = []
        for name in GLOBAL_FLAGS:
            f = self._root.persistent.get(name)
            if f is None:
                continue
            p = self.param_for(None, f)
            if name == "format":
                p.type = "enum"
                p.enum = list(FORMAT_VALUES)
            out.append(p)
        f = self._root.persistent.get("profile")
        if self.settings is not None and f is not None and f.param is None:
            out.append(self.param_for(None, f))
        return out

    # ---------------------------------------------------------------- reference

    def command_type(self: App, m: Meta | None) -> str:
        return "write" if m is not None and m.is_write() else "read"

    def error_codes_of(self: App, m: Meta | None, builtin: bool = False) -> list[str]:
        """The codes a command can return: its declared codes plus the codes the
        library itself returns for it. With settings declared, a tool command
        (not a built-in) can stop at E_CONFIG for an unknown profile (D4, Q25)."""
        codes = {"E_USAGE", "E_UNKNOWN"}
        if self.settings is not None and not builtin:
            codes.add("E_CONFIG")
        if m is not None:
            codes.update(m.error_codes)
            if m.is_write():
                codes.update(
                    {
                        "E_CONFIRMATION_REQUIRED",
                        "E_CONFLICT",
                        "E_FORBIDDEN",
                        "E_IO",
                        "E_OUTCOME_UNKNOWN",
                    }
                )
                if m.batch_flag:
                    codes.add("E_PARTIAL")
            if m.file_inputs:
                codes.add("E_IO")
        return sorted(codes)

    def entry_for(self: App, c: Node) -> dict[str, Any]:
        m = self.meta_for(c)
        e: dict[str, Any] = {
            "path": c.path,
            "aliases": sorted(c.aliases),
            "summary": c.short,
            "type": self.command_type(m),
            "effects": [],
            "dangerous": False,
            "maturity": DRAFT,
            "evidence": [],
            "params": [p.to_json() for p in self.params(c, True)],
            "output": "",
            "error_codes": self.error_codes_of(m, c.builtin),
            "examples": [],
        }
        if m is not None:
            e["summary"] = self.summary_of(c)
            e["effects"] = m.effect_strings()
            e["dangerous"] = m.is_dangerous()
            e["maturity"] = m.maturity
            e["evidence"] = list(m.evidence)
            e["output"] = self.output_of(c, m)[0]
            e["examples"] = list(m.examples)
        return e

    def summary_of(self: App, c: Node) -> str:
        """A command's summary as help and reference show it: Meta.summary (the
        click summary without Meta), prefixed for a deprecated command (CLI-081)."""
        m = self.meta_for(c)
        summary = m.summary if m is not None and m.summary else c.short
        if m is not None and replacement_text(m.deprecated):
            summary = deprecated_text(m.deprecated, summary)
        return summary

    def index_for(self: App, c: Node) -> dict[str, Any]:
        e = self.entry_for(c)
        return {k: e[k] for k in ("path", "type", "effects", "dangerous", "maturity", "summary")}

    def schema_entries(self: App) -> dict[str, Any]:
        """The reference's schema catalog: every registered schema, and the
        command-specific copies output_of names (Q5)."""
        out = {name: schema_entry_of(s) for name, s in self._reg.schemas.items()}
        for c in self.commands():
            name, own = self.output_of(c, self.meta_for(c))
            if own is not None:
                out[name] = own
        return out

    def output_of(self: App, c: Node, m: Meta | None) -> tuple[str, dict[str, Any] | None]:
        """The schema name a command's reference entry gives as its output (Q5): its
        output schema, or, when Meta.default_fields differ from the schema's default
        fields, a copy of the schema with the command's default fields under the name
        <output>_for_<path> (path words joined by _, - read as _), returned as own,
        so reference shows the defaults the command really uses, as help does."""
        if m is None:
            return "", None
        s = self._reg.schemas.get(m.output)
        if s is None or not m.default_fields or list(m.default_fields) == list(s.default_fields):
            return m.output, None
        base = m.output + "_for_" + "_".join(c.path.split()).replace("-", "_")
        name = base
        n = 2
        while name in self._reg.schemas:
            name = f"{base}_{n}"
            n += 1
        own = schema_entry_of(s)
        own["default_fields"] = list(m.default_fields)
        return name, own

    def code_table(self: App, codes: list[str]) -> dict[str, Any]:
        out = {}
        for code in codes:
            spec = self.lookup_code(code)
            if spec is not None:
                out[code] = {
                    "exit": spec.exit,
                    "retryable": spec.retryable,
                    "meaning": spec.meaning,
                }
        return out

    def reference_full(self: App) -> dict[str, Any]:
        """The full reference (CLI-033): for tooling, not for agents."""
        codes = sorted(CORE_CODES) + sorted(self.ext_codes or {})
        return {
            "tool": self.tool_name(),
            "version": self._version(),
            "spec_version": SPEC_VERSION,
            "schema_version": SCHEMA_VERSION,
            "risk_tier": self.risk_tier,
            "global_flags": [p.to_json() for p in self.global_params()],
            "commands": [self.entry_for(c) for c in self.commands()],
            "schemas": self.schema_entries(),
            "error_codes": self.code_table(codes),
            "exit_codes": {str(k): v for k, v in EXIT_CODE_MEANINGS.items()},
        }

    def reference_index(self: App, group: str) -> dict[str, Any]:
        """The compact index (CLI-031): one entry per command, or groups when the
        list would exceed the index budget. group narrows it to one subtree."""
        base = self._root
        if group:
            found = self.command_by_path(group)
            if found is None:
                e = usage(
                    "unknown_command",
                    group,
                    "No command group has that path; details.available lists the groups.",
                )
                e.details["available"] = self.top_level_names()
                e.details["next_action"] = (
                    f"Run `{self.tool_name()} reference --index` to see the groups."
                )
                raise e
            base = found
        cmds = [c for c in self.commands() if base is self._root or c is base or c.is_under(base)]
        data: dict[str, Any] = {
            "tool": self.tool_name(),
            "version": self._version(),
            "commands": [self.index_for(c) for c in cmds],
        }
        if fits_index_budget(data):
            return data
        groups = []
        seen: list[Node] = []
        for c in cmds:
            top = c
            while top.parent is not None and top.parent is not base:
                top = top.parent
            if any(s is top for s in seen):
                continue
            seen.append(top)
            count = sum(1 for x in cmds if x is top or x.is_under(top))
            summary = top.short
            if top.cmd is not None and self._reg.groups.get(top.cmd):
                summary = self._reg.groups[top.cmd]
            groups.append({"path": top.path, "summary": summary, "command_count": count})
        return {"tool": self.tool_name(), "version": self._version(), "groups": groups}

    def reference_command(self: App, path: str) -> dict[str, Any]:
        """One command's entry with its output schema and error codes (CLI-032)."""
        c = self.command_by_path(path)
        if c is None or c is self._root or c.has_subcommands():
            raise self.unknown_reference_path(path, c)
        entry = self.entry_for(c)
        schemas = {}
        all_schemas = self.schema_entries()
        if entry["output"] in all_schemas:
            schemas[entry["output"]] = all_schemas[entry["output"]]
        codes = {
            code: {"exit": v["exit"], "retryable": v["retryable"]}
            for code, v in self.code_table(entry["error_codes"]).items()
        }
        return {
            "tool": self.tool_name(),
            "version": self._version(),
            "command": entry,
            "schemas": schemas,
            "error_codes": codes,
        }

    def unknown_reference_path(self: App, path: str, c: Node | None) -> Error:
        """reference --command with a path that names no command (CLI-032, CLI-012):
        E_USAGE unknown_command. A group lists its commands; otherwise the closest
        commands in the deepest group the path reaches are suggested (verb synonyms
        and short typos included) and next_action is the exact reference call for
        the best one."""
        from .ux import shell_word

        tool = self.tool_name()
        if c is not None and (c is self._root or c.has_subcommands()):
            cmds = [x.path for x in self.commands() if c is self._root or x.is_under(c)][:20]
            e = usage(
                "unknown_command",
                path,
                "That path is a group, not a command; details.available lists its commands.",
            )
            e.details["available"] = cmds
            e.details["next_action"] = (
                f"Run `{tool} reference --index {shell_word(path)}` to see its commands, or "
                f'`{tool} reference --command "<one of details.available>"`.'
            )
            return e
        words = path.split()
        group = self._root
        word = words[-1] if words else ""
        for w in words:
            nxt = group.child(w)
            if nxt is None or nxt.internal:
                word = w
                break
            if not nxt.has_subcommands():
                word = nxt.name  # a command followed by extra words
                break
            group = nxt
        # available and did_you_mean are --command values: registry paths without
        # the tool name.
        available = sorted(
            sub.path for sub in group.children if not sub.internal and not sub.hidden
        )

        def without_tool(s_: str) -> str:
            return s_.removeprefix(self._root.name).removeprefix(" ")

        suggestions = []
        for s_ in self.sibling_matches(group, word):
            x = self.command_by_path(without_tool(s_))
            if x is not None and not x.has_subcommands():
                suggestions.append(without_tool(s_))
        if not suggestions:
            suggestions = [without_tool(s_) for s_ in self.tree_matches(word, True)]
        suggestions = suggestions[:3]
        e = usage(
            "unknown_command",
            path,
            "No command has that path; details.did_you_mean lists the closest commands.",
        )
        e.details["available"] = available
        nxt_action = f"Run `{tool} reference --index` to list the commands."
        if suggestions:
            e.details["did_you_mean"] = suggestions
            nxt_action = f'Run `{tool} reference --command "{suggestions[0]}"`.'
        else:
            e.message = (
                "No command has that path; details.available lists the commands of the "
                "nearest group."
            )
        e.details["next_action"] = nxt_action
        return e

    # ---------------------------------------------------------------- command map

    def command_map(self: App) -> str:
        """The Skill command map from the registry (SKL-002, decision 4): one bullet
        per top-level noun, "- `<noun>`: <verb>, <verb>; `<noun> <sub>` <verb>|<verb>",
        with the verbs in registration order (register reads first), " (dangerous)"
        after each verb that needs --dangerous, and the built-ins and deprecated
        commands (CLI-081) left out. A command at the root is a bullet of its own
        ("- `<command>`"). SKILL.md and README carry this text between the
        aicli:command-map markers, kept current by a test; for the example tool it is

            - `note`: list, get, create, update, delete (dangerous), share (dangerous), bulk-tag
        """
        self._ensure_tree()
        rank = {cmd: i for i, cmd in enumerate(self._reg.order)}
        cmds = self.commands()
        big = len(rank)
        cmds.sort(key=lambda n: rank.get(n.cmd, big))  # stable: unregistered ones keep path order
        order: list[str] = []
        nouns: dict[str, dict[str, Any]] = {}
        for c in cmds:
            if c.builtin:
                continue
            m = self.meta_for(c)
            if m is not None and replacement_text(m.deprecated):
                continue
            mark = " (dangerous)" if m is not None and m.is_dangerous() else ""
            words = c.path.split()
            n = nouns.get(words[0])
            if n is None:
                n = {"root": None, "verbs": [], "groups": []}
                nouns[words[0]] = n
                order.append(words[0])
            if len(words) == 1:
                n["root"] = mark
                continue
            verb = words[-1] + mark
            group = " ".join(words[:-1])
            if group == words[0]:
                n["verbs"].append(verb)
                continue
            for g in n["groups"]:
                if g[0] == group:
                    g[1].append(verb)
                    break
            else:
                n["groups"].append((group, [verb]))
        out = []
        for name in order:
            n = nouns[name]
            if n["root"] is not None:
                out.append(f"- `{name}`{n['root']}\n")
                continue
            parts = []
            if n["verbs"]:
                parts.append(", ".join(n["verbs"]))
            for group, verbs in n["groups"]:
                parts.append(f"`{group}` " + "|".join(verbs))
            out.append(f"- `{name}`: " + "; ".join(parts) + "\n")
        return "".join(out)

    # ---------------------------------------------------------------- help

    def help_func(self: App, c: Node) -> None:
        """--help from the registry (CLI-030)."""
        st = self._run
        if st is not None:
            st.help_shown = True
        text = (
            self.group_help(c) if c is self._root or c.has_subcommands() else self.command_help(c)
        )
        self._write(self._stdout(), text.encode("utf-8"))

    def group_help(self: App, c: Node) -> str:
        b = []
        summary = c.short
        if c.cmd is not None and self._reg.groups.get(c.cmd):
            summary = self._reg.groups[c.cmd]
        if summary:
            b.append(summary + "\n\n")
        b.append(f"Usage: {c.command_path} <command> [flags]\n\n")
        rows = []
        width = 0
        for x in self.commands():
            if c is not self._root and not x.is_under(c):
                continue
            use = x.path
            tokens = usage_tokens(positionals_of(x, self.meta_for(x)))
            if tokens:
                use += " " + tokens
            tag = ""
            m = self.meta_for(x)
            if m is not None and m.is_write():
                tag = " (write, dangerous)" if m.is_dangerous() else " (write)"
            summary = x.short
            if m is not None and replacement_text(m.deprecated):
                summary = deprecated_text(m.deprecated, summary)
            rows.append((use, summary + tag))
            width = max(width, len(use))
        scope = "Commands" if c is self._root else "Commands under " + c.path
        b.append(f"{scope}:\n")
        for use, text in rows:
            b.append(f"  {use:<{width}}  {text}\n")
        b.append(
            "\nOutput is one JSON envelope on stdout (check ok first). Writes: run the command "
            "(add --dry-run to be explicit) to get a preview and a confirm token; nothing changes. "
            "Then run it again without --dry-run, adding --confirm <token>. Commands marked "
            "dangerous need --dangerous in both steps.\n"
        )
        b.append(f"Run `{self._root.name} <command> --help` for a command's flags.\n")
        return "".join(b)

    def command_help(self: App, c: Node) -> str:
        b = []
        m = self.meta_for(c)
        b.append(self.summary_of(c) + "\n")
        if c.long:
            b.append("\n" + c.long.strip() + "\n")
        from .ux import usage_line

        b.append(f"\nUsage: {usage_line(self, c)}\n")
        if c.aliases:
            b.append(f"Aliases: {', '.join(c.aliases)}\n")
        write = dangerous = False
        if m is not None:
            write, dangerous = m.is_write(), m.is_dangerous()
            b.append(f"Effects: {', '.join(m.effect_strings())}\n")
            if dangerous:
                b.append(
                    "Write (dangerous): run it with --dangerous (add --dry-run to be explicit) to "
                    "get a preview and a confirm token; nothing changes. Then run it again without "
                    "--dry-run, with --dangerous --confirm <token>.\n"
                )
                b.append("Dangerous: --dangerous is required in both steps.\n")
            elif write:
                b.append(
                    "Write: run it (add --dry-run to be explicit) to get a preview and a confirm "
                    "token; nothing changes. Then run it again without --dry-run, adding --confirm "
                    "<token>.\n"
                )
            line = self.fields_line(m)
            if line:
                b.append(line + "\n")
        params = self.params(c)
        if params:
            b.append("\nParameters:\n")
            for p in params:
                name = "--" + p.name if p.kind == "flag" else "<" + p.name + ">"
                notes = [p.type]
                if p.required:
                    notes.append("required")
                if p.multiple:
                    notes.append("repeatable or comma-separated")
                if p.enum:
                    notes.append("one of " + "|".join(p.enum))
                if p.default is not None:
                    notes.append("default " + go_value_text(p.default))
                if p.secret:
                    notes.append("secret")
                b.append(f"  {name} ({', '.join(notes)})  {p.description}\n")
        glob = "--format json|text|raw, --fields, --pretty, --quiet"
        if self.settings is not None:
            glob += ", --profile <name>"
        if write:
            glob += ", --dry-run, --confirm <token>"
        if dangerous:
            glob += ", --dangerous"
        b.append("\nGlobal flags: " + glob + "\n")
        if m is not None and m.examples:
            b.append("\nExamples:\n")
            for ex in m.examples:
                b.append("  " + ex + "\n")
        return "".join(b)

    def fields_line(self: App, m: Meta) -> str:
        """The fields --fields accepts (CLI-040), so help answers it without a
        reference call: the default fields first, then the others."""
        s = self.schema_of(m)
        if not s.fields:
            return ""
        defaults = list(m.default_fields) if m.default_fields else list(s.default_fields)
        if not defaults:
            return f"Fields: {', '.join(s.fields)}. --fields a,b returns only those."
        others = [f for f in s.fields if f not in defaults]
        line = "Fields: " + ", ".join(defaults) + " (default)"
        if others:
            line += "; also " + ", ".join(others) + ". --fields +" + others[0] + " adds one"
        else:
            line += "."
        return line + "; --fields all returns every field."

    # ---------------------------------------------------------------- tree checks

    def check_tree(self: App) -> list[str]:
        """Registry problems (CLI-021, CLI-022, CLI-025, CLI-027, R02). The
        conformance suite fails on any of them."""
        self._ensure_tree()
        problems: list[str] = []
        add = problems.append
        tool = self.tool_name()
        if self.risk_tier not in RISK_TIER_VALUES:
            add(f'App.risk_tier "{self.risk_tier}" is not one of {", ".join(RISK_TIER_VALUES)}')
        # An empty or "dev" App.version is a development build, which reports a
        # version derived from the changelog (P14); it is not a tree problem.
        problems.extend(self.check_ext_codes())
        reserved = ["reference", "context", "doctor", "changelog", "operation", "help", "version"]
        if self.update is not None:
            if not _REPO.fullmatch(self.update.repo or ""):
                add(f'App.update.repo {go_quote(self.update.repo)} is not a GitHub "owner/name"')
            if not _WORKFLOW.fullmatch(self.update.workflow or ""):
                add(
                    f"App.update.workflow {go_quote(self.update.workflow)} is not a workflow file "
                    "name like release.yml"
                )
        if self.settings is not None:
            from ._tree import flag_name_of

            seen_settings: set[str] = set()
            for i, s in enumerate(self.settings):
                if not _SNAKE.fullmatch(s.name or ""):
                    add(f"App.settings[{i}]: {go_quote(s.name)} is not a snake_case name")
                elif s.name in seen_settings:
                    add(f"App.settings: {go_quote(s.name)} is declared twice")
                seen_settings.add(s.name)
            for c in self._root.walk():
                if c.cmd is None or c.builtin:
                    continue
                for p in c.cmd.params:
                    if isinstance(p, click.Option) and flag_name_of(p) == "profile":
                        add(
                            f'"{c.path or tool}" defines --profile, which aicli reserves for the '
                            "active profile when App.settings is set (CLI-101)"
                        )
        max_tier = "T0"
        for c in self._root.walk():
            if c.internal:
                continue
            path = c.path
            if c.parent is self._root and c.name == "update" and not c.builtin:
                add("`update` is aicli's built-in; set App.update (CLI-070)")
            seen: dict[str, str] = {}
            for sub in c.children:
                if sub.internal:
                    continue
                for n in [sub.name, *sub.aliases]:
                    if n in seen and seen[n] != sub.name:
                        add(
                            f'"{path}": the name or alias "{n}" is used by both "{seen[n]}" and '
                            f'"{sub.name}"'
                        )
                    seen[n] = sub.name
            if c is not self._root and c.has_subcommands():
                if c.cmd is not None and c.cmd in self._reg.meta:
                    add(
                        f'"{path}" is a group and also registered as a command; a path is one or '
                        "the other"
                    )
                continue
            if c is self._root:
                continue
            if c.parent is self._root and c.name in reserved and not c.builtin:
                add(f'"{path}" uses a reserved built-in name')
            if c.hidden:
                continue
            m = self.meta_for(c)
            if m is None:
                add(f'"{path}" has no registered Meta (call App.register)')
                continue
            for f in c.local_flags().values():
                if f.param is None:
                    continue
                if is_global_flag(f.name):
                    add(f'"{path}" defines --{f.name}, which shadows a global flag')
                if f.unsupported == "nargs":
                    add(
                        f'"{path}": --{f.name} takes several values per use (nargs), which aicli '
                        "does not support"
                    )
                if f.unsupported == "prompt":
                    add(
                        f'"{path}": --{f.name} prompts for its value; agents cannot answer prompts '
                        "(CLI-001)"
                    )
            if not m.summary.strip():
                add(f'"{path}" has no summary')
            if not m.effects:
                add(f'"{path}" declares no effects')
            for e in m.effects:
                if e not in EFFECT_VALUES:
                    add(f'"{path}" declares unknown effect "{e}"')
                    continue
                if tier_rank(MINIMUM_RISK_TIER[e]) > tier_rank(max_tier):
                    max_tier = MINIMUM_RISK_TIER[e]
            if m.dangerous and tier_rank(max_tier) < tier_rank("T2") and m.is_write():
                max_tier = "T2"
            if m.maturity not in MATURITY_VALUES:
                add(f'"{path}" has unknown maturity "{m.maturity}"')
            if not m.output:
                add(f'"{path}" names no output schema')
            elif m.output not in self._reg.schemas:
                add(f'"{path}" names output schema "{m.output}", which is not registered')
            else:
                s = self._reg.schemas[m.output]
                if s.shape not in SCHEMA_SHAPES:
                    add(f'schema "{m.output}" has unknown shape "{s.shape}"')
                for f in [*s.default_fields, *m.default_fields]:
                    if s.fields and f not in s.fields:
                        add(f'"{path}": default field "{f}" is not a field of schema "{m.output}"')
            if not m.examples:
                add(f'"{path}" has no example')
            for ex in m.examples:
                if not ex.startswith(tool + " "):
                    add(
                        f"{go_quote(path)}: example {go_quote(ex)} does not start with "
                        f"{go_quote(tool)}"
                    )
                elif self.example_command(ex)[0] is not c:
                    # Resolved as the parser resolves it (shortcuts, aliases), as R01
                    # does (Q21).
                    add(
                        f"{go_quote(path)}: example {go_quote(ex)} does not run this command; "
                        f"start it with {go_quote(tool)} and the command's path, a shortcut or an "
                        "alias"
                    )
            if m.stream_item and m.stream_item not in self._reg.schemas:
                add(
                    f"{go_quote(path)} names stream item schema {go_quote(m.stream_item)}, which "
                    "is not registered"
                )
            for old in sorted(m.renamed_flags):
                renamed = m.renamed_flags[old]
                if c.flag(renamed) is None:
                    add(
                        f'"{path}": Meta.renamed_flags maps --{old} to --{renamed}, which the '
                        "command does not define"
                    )
                elif c.flag(old) is not None:
                    add(
                        f'"{path}": Meta.renamed_flags names --{old}, which the command still '
                        f"defines; keep only --{renamed}"
                    )
            for alias in sorted(m.flag_aliases):
                canonical = m.flag_aliases[alias]
                if c.flag(canonical) is None:
                    add(
                        f'"{path}": Meta.flag_aliases maps --{alias} to --{canonical}, which the '
                        "command does not define"
                    )
                elif c.flag(alias) is not None:
                    add(
                        f'"{path}": Meta.flag_aliases names --{alias}, which the command already '
                        "defines"
                    )
            if m.is_write():
                if not any("--dry-run" in ex for ex in m.examples) or not any(
                    "--confirm" in ex for ex in m.examples
                ):
                    add(
                        f'"{path}" is a write; its examples need a --dry-run and a --confirm '
                        "example"
                    )
                if m.is_dangerous():
                    for ex in m.examples:
                        if ("--dry-run" in ex or "--confirm" in ex) and "--dangerous" not in ex:
                            add(f'"{path}" is dangerous; example "{ex}" needs --dangerous')
            for code in m.error_codes:
                if self.lookup_code(code) is None:
                    add(
                        f'"{path}" lists error code "{code}", which is neither core nor declared '
                        "in App.ext_codes"
                    )
            if m.positional and c.flag(m.positional) is None:
                add(
                    f'"{path}": Meta.positional names --{m.positional}, which the command does not '
                    "define"
                )
            for file, bound in m.file_inputs.items():
                if c.flag(file) is None:
                    add(
                        f'"{path}": Meta.file_inputs names --{file}, which the command does not '
                        "define"
                    )
                if not bound.strip():
                    add(f'"{path}": Meta.file_inputs maps --{file} to an empty name')
            for s in m.secrets:
                if c.flag(s) is None and s not in m.file_inputs.values():
                    add(
                        f'"{path}": Meta.secrets names "{s}", which is neither a flag nor a bound '
                        "file input"
                    )
            # B4: a bound name is one input; a file stands in only for a plain value flag
            first_file: dict[str, str] = {}
            for file in sorted(m.file_inputs):
                bound = m.file_inputs[file]
                if bound in first_file:
                    add(
                        f'"{path}": Meta.file_inputs maps --{first_file[bound]} and --{file} to '
                        f'the same name "{bound}"'
                    )
                else:
                    first_file[bound] = file
                twin = c.flag(bound)
                if twin is not None and twin is not c.flag(file) and not _plain_value(twin, m):
                    add(
                        f'"{path}": Meta.file_inputs maps --{file} onto --{bound}, which is not a '
                        "plain value flag the file can stand in for"
                    )
            # B3: a write's file content is bound only when aicli reads it
            if m.is_write():
                for f in c.local_flags().values():
                    kind = _opened_by_tool(f, m)
                    if kind:
                        add(
                            f'"{path}": --{f.name} is a {kind} the tool opens itself, so the write '
                            "gate cannot bind its content; declare it in Meta.file_inputs and read "
                            "it with App.read_file_input"
                        )
            if m.batch_flag and c.flag(m.batch_flag) is None:
                add(
                    f'"{path}": Meta.batch_flag names --{m.batch_flag}, which the command does not '
                    "define"
                )
            for name, doc in m.params.items():
                if c.flag(name) is None:
                    add(f'"{path}": Meta.params names --{name}, which the command does not define')
                if doc.type and doc.type not in PARAM_TYPES:
                    add(f'"{path}": Meta.params --{name} has unknown type "{doc.type}"')
            for name in m.deprecated_flags:
                if c.flag(name) is None:
                    add(
                        f'"{path}": Meta.deprecated_flags names --{name}, which the command does '
                        "not define"
                    )
        if self.risk_tier in RISK_TIER_VALUES and tier_rank(self.risk_tier) < tier_rank(max_tier):
            add(
                f"App.risk_tier {self.risk_tier} is below {max_tier}, which the declared effects "
                "require (SEC-001)"
            )
        problems.extend(self._reg.problems)
        for key in sorted(self.shortcuts or {}):
            to = " ".join(self.shortcuts[key])
            target = self.command_by_path(to)
            if target is None or target is self._root:
                add(f'App.shortcuts["{key}"] points to "{to}", which is not a command or group')
            words = key.split()
            if words and self._root.child(words[0]) is not None:
                add(f'App.shortcuts["{key}"] never applies: "{words[0]}" is already a root command')
        # A nested group needs a root shortcut (CLI-022) when its noun occurs under one
        # parent only; a noun under several parents (comment under issue and mr) needs
        # none, since a root word could name only one of them (Q6).
        nested: dict[str, int] = {}
        for c in self._root.walk():
            if (
                c is not self._root
                and not c.internal
                and not c.hidden
                and c.has_subcommands()
                and c.parent is not self._root
            ):
                nested[c.name] = nested.get(c.name, 0) + 1
        for c in self._root.walk():
            if (
                c is self._root
                or c.internal
                or c.hidden
                or not c.has_subcommands()
                or c.parent is self._root
                or nested.get(c.name, 0) > 1
            ):
                continue
            words = c.path.split()
            reachable = False
            for key, to in (self.shortcuts or {}).items():
                kw = key.split()
                effective = bool(kw) and self._root.child(kw[0]) is None
                if effective and " ".join(to) == " ".join(words):
                    reachable = True
            if not reachable:
                add(
                    f'nested group "{c.path}" is not reachable from the root; add '
                    f'App.shortcuts["{words[-1]}"] (CLI-022)'
                )
        return problems


_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_WORKFLOW = re.compile(r"[A-Za-z0-9_.-]+\.ya?ml")
_SNAKE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")


def _plain_value(f: Flag, m: Meta) -> bool:
    """A flag a file input can stand in for (its literal twin): one string value
    of the command's own, not a file flag, a global flag, a list or a switch."""
    return (
        f.param is not None
        and f.name not in m.file_inputs
        and not is_global(f)
        and not f.multiple
        and f.kind in ("string", "value")
    )


def _opened_by_tool(f: Flag, m: Meta) -> str:
    """The kind of a parameter whose file the tool would open itself (B3): a
    click.File, or a click.Path of an existing file outside Meta.file_inputs; ""
    otherwise."""
    t = getattr(f.param, "type", None) if f.param is not None else None
    if isinstance(t, click.File):
        return "click.File"
    if (
        isinstance(t, click.Path)
        and getattr(t, "exists", False)
        and not getattr(t, "dir_okay", True)
        and f.name not in m.file_inputs
    ):
        return "click.Path of an existing file"
    return ""


def _short_of(cmd: click.Command) -> str:
    """A click command's one-line summary: short_help, else the first line of help."""
    if getattr(cmd, "short_help", None):
        return str(cmd.short_help).strip()
    text = (getattr(cmd, "help", None) or "").strip()
    return text.split("\n", 1)[0].strip() if text else ""


def fits_index_budget(data: dict[str, Any]) -> bool:
    env = Ordered(
        ok=True, schema_version=SCHEMA_VERSION, data=data, meta=Ordered(duration_ms=100000)
    )
    try:
        text = go_json(env) + "\n"
    except (TypeError, ValueError):
        return False
    return len(text) <= INDEX_BUDGET_CHARS


__all__ = [
    "ARTIFACT",
    "CHANGE",
    "CONFIGURE",
    "DELIVER",
    "DRAFT",
    "EXPERIMENTAL",
    "OBSERVE",
    "PERSIST",
    "STABLE",
    "VERIFIED",
    "Meta",
    "ParamDoc",
    "SchemaDef",
]
