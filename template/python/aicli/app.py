# Vendored from ai-native-cli-spec template/python/aicli.
# Do not edit in tool repos; run scripts/sync-spec.js.
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sean Guo

"""The App: one tool, its click tree and the hooks the library calls. Build a new
App for every execute; an App runs once."""

from __future__ import annotations

import contextlib
import contextvars
import datetime as _dt
import os
import ssl
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import click

from ._tree import (
    CLICK_UNSET,
    Flag,
    FlagParseError,
    Node,
    find,
    flag_from_option,
    parse_flags,
)
from ._util import Ordered, go_json, truncate_runes, utc
from .binding import BindingMixin
from .builtins import BuiltinsMixin, Check, dev_version
from .contract_gen import CORE_CODES, DEFAULT_FORMAT, FORMAT_VALUES, SPEC_VERSION
from .envelope import EnvelopeMixin, Result_, RunState
from .errors import CodeSpec, Error, ErrorsMixin, internal_error, new_error, usage
from .gate import GateMixin
from .journal import JournalMixin
from .keystore import KeystoreMixin
from .query import QueryMixin
from .registry import Registry, RegistryMixin, _short_of, with_twins
from .scope import ScopeMixin
from .selfupdate import SelfUpdateMixin
from .settings import Setting, SettingsMixin
from .stream import StreamMixin
from .update import UpdateConfig
from .ux import UXMixin

_CURRENT: contextvars.ContextVar[App | None] = contextvars.ContextVar("aicli_app", default=None)

# Secret values shorter than this are not redacted: replacing them would garble
# the text around them, and they protect nothing.
MIN_SECRET_LEN = 4


@dataclass
class Identity:
    """The account and host a command acts as."""

    account: str = ""
    host: str = ""
    extra: dict[str, str] = field(default_factory=dict)


# The contract's global control flags: name, kind, default text, usage.
_GLOBALS = (
    (
        "format",
        "string",
        DEFAULT_FORMAT,
        "Output format: json (default, for agents), text (for people), raw (commands that declare "
        "it)",
    ),
    ("json", "bool", "false", "Same as --format json"),
    ("pretty", "bool", "false", "Indent JSON for people (JSON is compact by default)"),
    ("compact", "bool", "false", "Accepted for compatibility; JSON is compact by default"),
    (
        "fields",
        "string",
        "",
        "Fields to return: a,b replaces the defaults, +a adds to them, all returns every field",
    ),
    ("quiet", "bool", "false", "Suppress non-error output on stderr"),
    ("dry-run", "bool", "false", "Preview a write and get a confirm token; changes nothing"),
    ("confirm", "string", "", "Run a previewed write with the confirm token from its preview"),
    ("dangerous", "bool", "false", "Required in both steps of a dangerous write"),
)


def panic_error(exc: BaseException) -> Error:
    """A crash inside the library or the tool as E_UNKNOWN, its text in
    details.cause (untrusted; secrets are redacted with the rest of the error)."""
    e = internal_error(
        "The command failed with an internal error; details.cause has the panic text."
    )
    e.details["cause"] = truncate_runes(f"{type(exc).__name__}: {exc}", 300)
    e.untrusted = ["cause"]
    return e


def current_app() -> App:
    """The App running in this context (for module-level click or typer commands)."""
    app = _CURRENT.get()
    if app is None:
        raise RuntimeError("no aicli App is running")
    return app


def set_resolved(cmd: Any, flag: str, *values: str) -> None:
    """Replace a flag's value with its resolved form (see App.set_resolved) on the
    App that is running."""
    current_app().set_resolved(cmd, flag, *values)


def split_flag(arg: str) -> tuple[str, str, bool]:
    """ "--name=value" / "-n=value" / "--name" into its parts."""
    if arg.startswith("--"):
        name = arg[2:]
    elif arg.startswith("-") and len(arg) > 1:
        name = arg[1:]
    else:
        return "", "", False
    if "=" in name:
        i = name.index("=")
        return name[:i], name[i + 1 :], True
    return name, "", False


def flag_name(arg: str) -> str:
    return split_flag(arg)[0]


def takes_value(f: Flag | None, arg: str) -> bool:
    """Whether a flag token consumes the next argument as its value."""
    if f is None or "=" in arg:
        return False
    return f.no_opt_def_val == ""


def scan_output_flags(st: RunState, args: list[str]) -> None:
    """The output flags read from the raw arguments, so errors raised before flag
    parsing (unknown commands, bad flags) use the requested format."""
    json_flag = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            break
        if arg == "--format" and i + 1 < len(args):
            st.format = args[i + 1].strip().lower()
            i += 1
        elif arg.startswith("--format="):
            st.format = arg[len("--format=") :].strip().lower()
        elif arg in ("--json", "--json=true"):
            json_flag = True
        elif arg in ("--pretty", "--pretty=true"):
            st.pretty = True
        elif arg in ("--quiet", "--quiet=true"):
            st.quiet = True
        i += 1
    if json_flag or st.format not in FORMAT_VALUES:
        st.format = DEFAULT_FORMAT


class App(
    ErrorsMixin,
    EnvelopeMixin,
    QueryMixin,
    RegistryMixin,
    UXMixin,
    BindingMixin,
    KeystoreMixin,
    JournalMixin,
    ScopeMixin,
    GateMixin,
    BuiltinsMixin,
    SelfUpdateMixin,
    SettingsMixin,
    StreamMixin,
):
    """One tool: its identity, its click tree and the hooks the library calls."""

    def __init__(
        self,
        *,
        name: str = "",
        version: str = "",
        risk_tier: str = "",
        env_prefix: str = "",
        root: click.Group | None = None,
        changelog: str = "",
        min_skill_version: str = "",
        skill_dirs: list[str] | None = None,
        identity: Callable[[], Identity] | None = None,
        doctor_checks: Callable[[], list[Check]] | None = None,
        context_extra: Callable[[], dict[str, Any]] | None = None,
        shortcuts: dict[str, list[str]] | None = None,
        short_flags: dict[str, str] | None = None,
        flag_aliases: dict[str, str] | None = None,
        flag_synonyms: dict[str, list[str]] | None = None,
        verb_synonyms: dict[str, list[str]] | None = None,
        ext_codes: dict[str, CodeSpec] | None = None,
        update: UpdateConfig | None = None,
        settings: list[Setting] | None = None,
        stdin: Any = None,
        stdout: Any = None,
        stderr: Any = None,
        now: Callable[[], _dt.datetime] | None = None,
        env: Callable[[str], str] | None = None,
        state_dir: str = "",
    ) -> None:
        self.name = name
        self.version = version
        self.risk_tier = risk_tier
        self.env_prefix = env_prefix  # e.g. "GITLAB_CLI"
        self.root = root
        self.changelog = changelog  # the embedded CHANGELOG.md (Keep a Changelog)
        # fallback for doctor's version check when no Skill is installed (decision 22)
        self.min_skill_version = min_skill_version
        # where doctor looks for <dir>/<name>/SKILL.md; None means the standard agent
        # skill directories (~/.agents/skills, ~/.claude/skills)
        self.skill_dirs = skill_dirs
        # identity resolves the account and host a write would act as, from local
        # configuration only (no network). None means the tool has no account.
        self.identity = identity
        self.doctor_checks = doctor_checks
        self.context_extra = context_extra
        # words typed at the root that reach a nested command ("branch" -> ["repo", "branch"])
        self.shortcuts = shortcuts or {}
        # single-letter flags for long flags ("p" -> "project")
        self.short_flags = short_flags or {}
        # other long flag names ("repo" -> "project")
        self.flag_aliases = flag_aliases or {}
        # suggested when an unknown flag is used ("all" -> ["limit"])
        self.flag_synonyms = flag_synonyms or {}
        # suggested when an unknown subcommand is used ("view" -> ["get"])
        self.verb_synonyms = verb_synonyms or {}
        self.ext_codes = ext_codes or {}  # the tool's extension codes (contract-ext.json)
        # the built-in update (CLI-070): set it to register `update`; None means no update
        self.update = update
        # the tool's settings (CLI-100, CLI-101); not None registers --profile, the
        # context profile key and doctor's settings check
        self.settings = settings
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.now = now
        self.env = env  # default os.environ
        self.state_dir = state_dir  # override; default <PREFIX>_STATE_DIR or ~/.<name>
        self._reg = Registry()
        self._builtins: dict[str, Any] | None = None
        self._root_node: Node | None = None
        self._prepared = False
        self._executed = False
        self._run: RunState | None = None
        self._ctx_cache: dict[int, click.Context] = {}
        # tool bugs the library corrected at run time (run_conformance reports them)
        self.defects: list[str] = []
        # set by run_conformance: no network use (doctor skips the update refresh)
        self._offline = False
        # secrets the tool registered with add_secret (redacted from errors and debug)
        self._added_secrets: list[str] = []
        # debug, notice and add_secret may run on several threads (Q22)
        self._mu = threading.Lock()

    # ---------------------------------------------------------------- basics

    def _now(self) -> _dt.datetime:
        if self.now is not None:
            return utc(self.now())
        return _dt.datetime.now(_dt.timezone.utc)

    def _env(self, key: str) -> str:
        if self.env is not None:
            return self.env(key) or ""
        return os.environ.get(key, "")

    def defect(self, msg: str) -> None:
        """Record a tool bug the library corrected so the output stays within the
        contract; the conformance suite fails on it, production output is unaffected."""
        if msg not in self.defects:
            self.defects.append(msg)

    def _version(self) -> str:
        """The tool version the whole run reports and uses (CLI-080, P14): App.version
        when it is a semantic version; otherwise, for a development build ("dev",
        empty), the next patch after the newest version in App.changelog with the
        prerelease dev ("1.2.17" gives "1.2.18-dev"; no released version gives
        "0.0.0-dev"). version, reference, changelog, context, doctor, the update
        notice, update and the confirm token all see it."""
        if self._dev_build():
            return dev_version(self.changelog)
        return self.version

    def _dev_build(self) -> bool:
        """A build whose App.version is not a semantic version (P14)."""
        from .selfupdate import parse_version

        try:
            parse_version(self.version)
        except ValueError:
            return True
        return False

    def ssl_context(self) -> ssl.SSLContext:
        """The TLS settings of CLI-110 (P10), for the tool's upstream client and
        aicli's own requests: TLS 1.2 or newer, always verified, against the system
        roots plus the PEM file <PREFIX>_CA_FILE names. An unusable CA file is
        E_CONFIG. Build the upstream client on it with the proxy from HTTPS_PROXY,
        HTTP_PROXY and NO_PROXY as App.env gives them (Q2; update.app_proxies reads
        them as update does), a per-request timeout of at most 30 s and user_agent(),
        and never re-send a write (CLI-110)."""
        ctx = ssl.create_default_context()  # system roots, verification on
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        prefix = self._prefix()
        path = (self._env(prefix + "_CA_FILE") or "").strip()
        if path:
            try:
                ctx.load_verify_locations(cafile=path)
            except (OSError, ssl.SSLError, ValueError):
                raise new_error(
                    "E_CONFIG",
                    f"{prefix}_CA_FILE names {path}, which is not a readable PEM certificate file.",
                    {
                        "dispatch_state": "not_started",
                        "next_action": f"Point {prefix}_CA_FILE at a PEM file of CA certificates, "
                        "or unset it.",
                    },
                ) from None
        return ctx

    def user_agent(self) -> str:
        """The User-Agent of every request (CLI-110): <tool>/<version>
        aicli/<spec version>."""
        return f"{self.tool_name()}/{self._version()} aicli/{SPEC_VERSION}"

    def debug(self, message: str) -> None:
        """A diagnostic line (CLI-102): when <PREFIX>_DEBUG is set to anything but
        empty or 0 (spaces trimmed), "debug: <message>" goes to stderr, never to
        stdout; output and exit codes do not change. Secret values (redaction_values)
        are replaced by ***, and line breaks are escaped so every line starts with
        "debug:". It is safe to call from several threads (Q22): each line is written
        whole."""
        flag = self._env(self._prefix() + "_DEBUG").strip()
        if flag in ("", "0"):
            return
        text = self.redact(str(message)).replace("\r", "\\r").replace("\n", "\\n")
        line = ("debug: " + text + "\n").encode("utf-8", "replace")
        with self._mu, contextlib.suppress(Exception):
            self._write(self._stderr(), line)

    def _debug_once(self, message: str) -> None:
        """A library diagnostic, written at most once per run."""
        st = self._run
        if st is not None:
            with self._mu:
                if message in st.debugged:
                    return
                st.debugged.add(message)
        self.debug(message)

    def add_secret(self, value: str) -> None:
        """Register a secret the tool obtained at run time (a token read from the
        keyring), so errors and debug lines show it as *** like a secret flag
        (CLI-102, SEC-021). Values shorter than 4 characters are ignored."""
        if isinstance(value, str) and len(value) >= MIN_SECRET_LEN:
            with self._mu:
                if value not in self._added_secrets:
                    self._added_secrets.append(value)

    def redaction_values(self) -> list[str]:
        """What debug lines replace with ***, longest first: the running command's
        secret flags (Meta.secrets, their file twins and the flags of Secret
        settings) and the content of its secret file inputs, the values of Secret
        settings (from their flag, the environment or the default) and the values
        registered with add_secret; each also URL query-escaped, path-escaped, and
        path-escaped keeping "/" (P17). Errors use error_redaction_values."""
        with self._mu:
            base: set[str] = set(self._added_secrets)
        st = self._run
        node = (st.cmd or st.parsed) if st is not None else None
        m = self.meta_for(node) if node is not None and self._prepared else None
        if st is not None and node is not None and m is not None:
            for name in m.secrets:
                f = node.flag(name)
                if f is None or not f.changed:
                    continue
                if f.multiple:
                    base.update(f.slice_strings())
                base.add(f.string())
            for file, bound in m.file_inputs.items():
                fi = st.files.get(file)
                if fi is not None and (m.is_secret(file) or m.is_secret(bound)):
                    text = bytes(fi.content).decode("utf-8", "replace")
                    base.update((text, text.strip()))
        with contextlib.suppress(Exception):
            base.update(self.secret_setting_values())
        out: set[str] = set()
        for v in base:
            if not isinstance(v, str) or not v:
                continue
            out.add(v)
            with contextlib.suppress(UnicodeError):
                out.add(urllib.parse.quote_plus(v, safe=""))  # Go's url.QueryEscape
                out.add(urllib.parse.quote(v, safe="$&+:=@"))  # Go's url.PathEscape
                # the path-escaped form that keeps "/" (P17): Go's
                # strings.ReplaceAll(url.PathEscape(v), "%2F", "/")
                out.add(urllib.parse.quote(v, safe="$&+:=@/"))
        out.discard("")
        return sorted(out, key=lambda s: (-len(s.encode("utf-8", "surrogatepass")), s))

    def error_redaction_values(self) -> list[str]:
        """redaction_values without values shorter than 4 characters, which would
        mangle the text of an error without protecting anything (D1, D2)."""
        return [v for v in self.redaction_values() if len(v) >= MIN_SECRET_LEN]

    def redact(self, text: str, values: list[str] | None = None) -> str:
        """text with every secret value (redaction_values) replaced by ***."""
        for v in self.redaction_values() if values is None else values:
            if v in text:
                text = text.replace(v, "***")
        return text

    def tool_name(self) -> str:
        if self.name:
            return self.name
        if self.root is not None and self.root.name:
            return self.root.name
        return "tool"

    def _prefix(self) -> str:
        """The environment variable prefix (the name in upper case by default)."""
        if self.env_prefix:
            return self.env_prefix
        return self.tool_name().replace("-", "_").replace(".", "_").upper()

    @property
    def _root(self) -> Node:
        self._ensure_tree()
        assert self._root_node is not None
        return self._root_node

    # ---------------------------------------------------------------- execute

    def execute(self, args: list[str] | None = None) -> int:
        """Run the tool with args (without the program name), write exactly one
        response and return the process exit code. Installs the built-ins,
        normalises the arguments agents commonly guess, and turns crashes into an
        E_UNKNOWN envelope."""
        if args is None:
            args = sys.argv[1:]
        args = [str(a) for a in args]
        st = RunState(start=self._now(), start_real=time.monotonic(), format=DEFAULT_FORMAT)
        prev = self._run
        self._run = st
        token = _CURRENT.set(self)
        try:
            scan_output_flags(st, args)
            if self._executed:
                st.panic_err = internal_error(
                    "App.execute was called twice on one App; build a new App for each run."
                )
                return self._finish_guarded(st)
            self._executed = True
            try:
                if self.root is None or not isinstance(self.root, click.Group):
                    st.panic_err = internal_error(
                        "App.root must be a click group (typer: typer.main.get_command(app) "
                        "of an app with commands)."
                    )
                else:
                    self._ensure_tree()
                    norm = self.normalize_args(args)
                    st.args = norm
                    self._dispatch(st, norm)
            except Error as e:
                st.cmd_err = e
            except KeyboardInterrupt:
                st.cmd_err = new_error("E_INTERRUPTED", "The command was interrupted.")
            except Exception as exc:  # a library crash: report it, never a traceback
                st.panic_err = panic_error(exc)
            return self._finish_guarded(st)
        finally:
            self._run = prev
            _CURRENT.reset(token)

    def _finish_guarded(self, st: RunState) -> int:
        """_finish, with a crash while writing the response reported as E_UNKNOWN
        instead of a traceback; if even that cannot be written, the exit code of
        E_UNKNOWN is returned (D9)."""
        try:
            return self._finish(st)
        except Exception as exc:  # the response itself failed: report that instead
            st.panic_err = panic_error(exc)
            st.gate = Result_()
            st.stream = None
            st.notices = []
        try:
            return self._finish(st)
        except Exception:
            return CORE_CODES["E_UNKNOWN"].exit

    def main(self, args: list[str] | None = None) -> None:
        """execute and exit the process with its exit code."""
        sys.exit(self.execute(args))

    # ---------------------------------------------------------------- the tree

    def _ensure_tree(self) -> None:
        if self._prepared:
            return
        self.install_builtins()
        self._prepare_tree()

    def _prepare_tree(self) -> None:
        """Build the library's nodes over the click tree and the built-ins: global
        flags, help flags, aliases and summaries. Runs once per App."""
        self._prepared = True
        root_cmd = (
            self.root if isinstance(self.root, click.Group) else click.Group(self.tool_name())
        )
        root = Node(self.tool_name(), root_cmd, None)
        root.short = self._reg.groups.get(root_cmd) or _short_of(root_cmd)
        for name, kind, default, text in _GLOBALS:
            root.persistent[name] = Flag(
                name,
                kind=kind,
                usage=text,
                def_value=default,
                default=(False if kind == "bool" else default),
                owner=root,
            )
        if self.settings is not None:
            # CLI-101: a root flag, bound into confirm tokens like any other flag
            root.persistent["profile"] = Flag(
                "profile",
                kind="string",
                usage=f"Profile from the settings file to use (default {self._prefix()}_PROFILE; "
                "`context` lists the profiles)",
                def_value="",
                default="",
                owner=root,
            )
        root.local["version"] = Flag(
            "version",
            kind="bool",
            usage="Print the tool, its version and the spec version as JSON",
            def_value="false",
            default=False,
            owner=root,
        )
        self._root_node = root
        self._attach_params(root)
        for sub in sorted(root_cmd.commands):
            self._build(root_cmd.commands[sub], sub, root, builtin=False)
        b = self._builtins or {}
        for cmd in b.get("root", []):
            if root.child(cmd.name) is None:
                self._build(cmd, cmd.name, root, builtin=True)
        op = root.child("operation")
        if op is None:
            op = self._build(b["operation"], "operation", root, builtin=True)
        if op.cmd is not None and self._reg.groups.get(op.cmd):
            for cmd in b.get("operation_children", []):
                if op.child(cmd.name) is None:
                    self._build(cmd, cmd.name, op, builtin=True)
        if root.child("help") is None:
            help_node = Node("help", None, root, internal=True)
            help_node.short = "Help about any command"
            root.children.append(help_node)
        for n in root.walk():
            n.children.sort(key=lambda c: c.name)
        for n in root.walk():
            if "help" not in n.local and "help" not in n.persistent:
                short = "h" if n.shorthand_flag("h") is None else ""
                n.add_local(
                    Flag(
                        "help",
                        kind="bool",
                        shorthand=short,
                        usage="help for " + n.name,
                        def_value="false",
                        default=False,
                        owner=n,
                    )
                )
            n._merged = None
        # The flag of a Secret setting is a secret flag of every command that has it
        # (CLI-100, B2), as if Meta.secrets named it.
        flags = sorted({s.flag for s in self.settings or [] if s.secret and s.flag})
        for n in root.walk():
            m = self._reg.meta.get(n.cmd) if n.cmd is not None else None
            if m is None or not flags:
                continue
            add = [f for f in flags if n.flag(f) is not None and f not in m.secrets]
            if add:
                m.secrets = with_twins([*m.secrets, *add], m.file_inputs)

    def _build(self, cmd: click.Command, name: str, parent: Node, *, builtin: bool) -> Node:
        node = Node(name, cmd, parent, builtin=builtin)
        m = self._reg.meta.get(cmd)
        node.short = _short_of(cmd) or (m.summary if m is not None else "")
        if cmd in self._reg.groups:
            node.short = self._reg.groups[cmd] or node.short
            node.aliases = [a for a in self._reg.group_aliases.get(cmd, []) if a != name]
        if m is not None:
            node.aliases = [
                a for a in m.aliases if a != name and a not in node.aliases
            ] + node.aliases
            if not node.short:
                node.short = m.summary
        text = (getattr(cmd, "help", None) or "").strip()
        if text and text != node.short and text.split("\n", 1)[0].strip() != text:
            node.long = text
        parent.children.append(node)
        self._attach_params(node)
        if isinstance(cmd, click.Group):
            for sub in sorted(cmd.commands):
                self._build(cmd.commands[sub], sub, node, builtin=builtin)
        if m is not None and m.batch_flag and node.flag("continue-on-error") is None:
            node.add_local(
                Flag(
                    "continue-on-error",
                    kind="bool",
                    usage="Keep going after an item fails (default true; false for dangerous "
                    "commands)",
                    def_value="false" if m.is_dangerous() else "true",
                    default=not m.is_dangerous(),
                    owner=node,
                )
            )
        return node

    def _attach_params(self, node: Node) -> None:
        cmd = node.cmd
        if cmd is None:
            return
        m = self._reg.meta.get(cmd)
        is_group = isinstance(cmd, click.Group)
        for p in cmd.params:
            if isinstance(p, click.Argument):
                if not is_group:
                    node.arguments.append(p)
                continue
            if not isinstance(p, click.Option):
                continue
            from ._tree import flag_name_of

            fname = flag_name_of(p)
            meta_type = m.params[fname].type if m is not None and fname in m.params else ""
            f = flag_from_option(p, node, meta_type)
            if is_group:
                node.persistent.setdefault(f.name, f)
            else:
                node.local.setdefault(f.name, f)
        node._merged = None

    # ---------------------------------------------------------------- normalisation

    def normalize_args(self, args: list[str]) -> list[str]:
        """Rewrite the command forms agents guess first (CLI-022) before parsing:
        root shortcuts for nested nouns, single-letter flags, long flag aliases
        (App-wide and the command's own, Q11) and renamed flags (Q10). Arguments
        that already parse are returned unchanged. A renamed flag's use is
        remembered for its notice, and its use beside the new name for the refusal
        (prepare_run)."""
        out = self._apply_shortcut(list(args))
        target, kinds = self.classify(out)
        if target is self._root:
            return out
        given = {flag_name(arg) for i, arg in enumerate(out) if kinds[i] == "flag"}
        m = self.meta_for(target)
        renamed: list[tuple[str, str]] = []
        for i, arg in enumerate(out):
            if kinds[i] != "flag":
                continue
            out[i], old = self._rewrite_flag(target, arg)
            if old and m is not None:
                renamed.append((old, m.renamed_flags[old]))
        st = self._run
        if st is not None:
            for old, new in renamed:
                if old not in st.renamed_used:
                    st.renamed_used.append(old)
                f = target.flag(new)
                if new in given or (f is not None and f.shorthand and f.shorthand in given):
                    st.renamed_both = (old, new)
        return out

    def _rewrite_flag(self, target: Node, arg: str) -> tuple[str, str]:
        """One flag token mapped through Meta.renamed_flags, App.flag_aliases,
        Meta.flag_aliases and App.short_flags. The second value is the renamed
        flag's old name when the token used it."""
        name, value, has_value = split_flag(arg)
        if not name:
            return arg, ""

        def applies(to: str | None) -> bool:
            return bool(to) and target.flag(name) is None and target.flag(to or "") is not None

        m = self.meta_for(target)
        long = old = ""
        if arg.startswith("--"):
            if m is not None and applies(m.renamed_flags.get(name)):
                long, old = m.renamed_flags[name], name
            elif applies(self.flag_aliases.get(name)):
                long = self.flag_aliases[name]
            elif m is not None and applies(m.flag_aliases.get(name)):
                long = m.flag_aliases[name]
        elif len(name) == 1:
            to = self.short_flags.get(name)
            if to and target.shorthand_flag(name) is None and target.flag(to) is not None:
                long = to
        if not long:
            return arg, ""
        return (f"--{long}={value}" if has_value else f"--{long}"), old

    def example_command(self, ex: str) -> tuple[Node | None, str]:
        """An example resolved the way the parser resolves a command line (Q21): the
        tool name, then root shortcuts and aliases. Returns the command the example
        runs (None when it names none) and the example rewritten with the command's
        path in place of the words that named it."""
        tokens = ex.split()
        if not tokens or tokens[0] != self.tool_name():
            return None, ex
        args = self._apply_shortcut(tokens[1:])
        target, kinds = self.classify(args)
        if target is self._root or target.has_subcommands():
            return None, ex
        out = [tokens[0], target.path]
        out.extend(a for a, k in zip(args, kinds, strict=True) if k != "word")
        return target, " ".join(out)

    def _apply_shortcut(self, args: list[str]) -> list[str]:
        """A root-level word that is not a root command replaced with the path
        App.shortcuts maps it to (the longest multi-word key wins)."""
        if not self.shortcuts:
            return args
        i = self._first_word(args)
        if i < 0 or self._root.child(args[i]) is not None:
            return args
        max_words = max([1, *(len(k.split()) for k in self.shortcuts)])
        for n in range(max_words, 0, -1):
            if i + n > len(args):
                continue
            words = args[i : i + n]
            if any(w.startswith("-") for w in words):
                continue
            to = self.shortcuts.get(" ".join(words))
            if not to:
                continue
            return args[:i] + list(to) + args[i + n :]
        return args

    def _first_word(self, args: list[str]) -> int:
        """The index of the first command word, skipping root flags."""
        i = 0
        while i < len(args):
            arg = args[i]
            if arg == "--":
                return -1
            if not arg.startswith("-") or arg == "-":
                return i
            if takes_value(self._root.flag(flag_name(arg)), arg):
                i += 1
            i += 1
        return -1

    def classify(self, args: list[str]) -> tuple[Node, list[str]]:
        """The deepest command the arguments name, and each token labelled as a
        command word, a flag, a flag's value or a positional argument."""
        kinds = ["word"] * len(args)
        c = self._root
        in_positionals = False
        i = 0
        while i < len(args):
            arg = args[i]
            if arg == "--":
                kinds[i] = "flag"
                for j in range(i + 1, len(args)):
                    kinds[j] = "positional"
                return c, kinds
            if arg.startswith("-") and arg != "-":
                kinds[i] = "flag"
                if takes_value(c.flag(flag_name(arg)), arg) and i + 1 < len(args):
                    kinds[i + 1] = "value"
                    i += 1
            elif not in_positionals:
                nxt = c.child(arg)
                if nxt is not None and not nxt.internal:
                    kinds[i] = "word"
                    c = nxt
                    i += 1
                    continue
                in_positionals = True
                kinds[i] = "positional"
            else:
                kinds[i] = "positional"
            i += 1
        return c, kinds

    # ---------------------------------------------------------------- dispatch

    def _dispatch(self, st: RunState, args: list[str]) -> None:
        """Find the command, parse its flags and run it (cobra's ExecuteC)."""
        node, rest = find(self._root, args)
        st.parsed = node  # errors redact its secret flags even before it runs
        try:
            positionals = parse_flags(node, rest, self._click_ctx(node))
        except FlagParseError as err:
            raise self.flag_error(node, err) from None
        st.positionals = positionals
        h = node.flags().get("help")
        if h is not None and h.changed and h.value:
            self.help_func(node)
            return
        if node.internal:  # cobra's help command
            target, _ = find(self._root, positionals)
            self.help_func(self._root if target.internal else target)
            return
        if node is self._root or node.has_subcommands():
            self.group_run(node, positionals)
            return
        self.validate_args(node, positionals)
        self.prepare_run(node, positionals)
        if (
            self.settings is not None
            and not node.builtin
            and not self._allows_unknown_profile(node)
        ):
            # An unknown profile stops every tool command before it runs; the
            # built-ins still run, and context reports the problem (CLI-101), and so
            # does a command declared with Meta.allow_unknown_profile (P6, Q4).
            ps = self._profile_state()
            if ps.unknown and not ps.reason:
                raise self._profile_unknown(ps)
        self._invoke(st, node, positionals)

    def _click_ctx(self, node: Node) -> click.Context | None:
        """The click context of node (its parents' contexts above it)."""
        if node.cmd is None:
            return None
        key = id(node)
        if key in self._ctx_cache:
            return self._ctx_cache[key]
        parent = self._click_ctx(node.parent) if node.parent is not None else None
        ctx = click.Context(node.cmd, parent=parent, info_name=node.name)
        if parent is None:
            ctx.meta["aicli.app"] = self
        self._ctx_cache[key] = ctx
        return ctx

    def _invoke(self, st: RunState, node: Node, positionals: list[str]) -> None:
        """Call the click callbacks: each group's on the path (as click does), then
        the leaf's, with the parsed values."""
        chain: list[Node] = []
        n: Node | None = node
        while n is not None:
            chain.append(n)
            n = n.parent
        chain.reverse()
        contexts = []
        for n in chain:
            ctx = self._click_ctx(n)
            if ctx is None:
                continue
            self._fill_params(n, ctx, positionals if n is node else [])
            contexts.append((n, ctx))
        try:
            with contextlib.ExitStack() as stack:
                for n, ctx in contexts:
                    stack.enter_context(ctx)
                    cb = n.cmd.callback if n.cmd is not None else None
                    if cb is None:
                        continue
                    if n is node:
                        ctx.invoke(cb, **ctx.params)
                    elif n.cmd is not None and not n.builtin:
                        ctx.invoke(cb, **ctx.params)
        except Error as e:
            st.cmd_err = e
        except click.exceptions.Exit as e:
            if getattr(e, "exit_code", 0):
                st.cmd_err = internal_error("The command exited without reporting a result.")
        except click.Abort:
            st.cmd_err = new_error("E_INTERRUPTED", "The command was interrupted.")
        except click.UsageError as e:
            param = getattr(e, "param", None)
            name = ""
            if isinstance(param, click.Option):
                from ._tree import flag_name_of

                name = "--" + flag_name_of(param)
            elif param is not None:
                name = param.name or ""
            st.cmd_err = usage("invalid_value", name, truncate_runes(e.format_message(), 300))
        except KeyboardInterrupt:
            st.cmd_err = new_error("E_INTERRUPTED", "The command was interrupted.")
        except SystemExit:
            st.cmd_err = internal_error("The command exited without reporting a result.")
        except Exception as exc:  # the tool's own error: E_UNKNOWN with the text as cause
            st.cmd_err = exc

    def _fill_params(self, n: Node, ctx: click.Context, positionals: list[str]) -> None:
        from click.core import ParameterSource

        from ._tree import flag_name_of

        st = self._run
        assert st is not None
        cmd = n.cmd
        assert cmd is not None
        args = [] if st.positional_consumed else list(positionals)
        values: dict[str, Any] = {}
        for p in cmd.params:
            source = ParameterSource.DEFAULT
            if isinstance(p, click.Option):
                f = n.local.get(flag_name_of(p)) or n.persistent.get(flag_name_of(p))
                if f is not None and f.param is p and f.changed:
                    raw = f.click_value()
                    source = ParameterSource.COMMANDLINE
                else:
                    raw = p.get_default(ctx)
            elif isinstance(p, click.Argument):
                if p.nargs == -1:
                    raw = tuple(args) if args else CLICK_UNSET
                    args = []
                elif p.nargs == 1:
                    raw = args.pop(0) if args else CLICK_UNSET
                else:
                    raw = tuple(args[: p.nargs]) if len(args) >= p.nargs else CLICK_UNSET
                    args = args[p.nargs :]
                if raw is not CLICK_UNSET:
                    source = ParameterSource.COMMANDLINE
                else:
                    raw = p.get_default(ctx)
            else:
                raw = p.get_default(ctx)
            if (
                raw is None
                and CLICK_UNSET is not None
                and not isinstance(CLICK_UNSET, type(object()))
            ):
                pass
            try:
                value = p.process_value(ctx, raw)
            except click.BadParameter as exc:
                name = p.name or ""
                if isinstance(p, click.Option):
                    name = "--" + flag_name_of(p)
                e = usage(
                    "invalid_value",
                    name,
                    truncate_runes(f"{name} is not valid: {exc.format_message()}", 300),
                )
                raise e from None
            except click.MissingParameter:
                e = usage(
                    "missing_parameter",
                    p.name or "",
                    f"`{n.command_path}` needs the <{p.name}> argument.",
                )
                raise e from None
            if value is CLICK_UNSET:
                value = None
            if p.expose_value and p.name:
                values[p.name] = value
            if p.name:
                with contextlib.suppress(Exception):
                    ctx.set_parameter_source(p.name, source)
        ctx.params.update(values)

    # ---------------------------------------------------------------- run state

    def _resolve_node(self, cmd: Any) -> Node | None:
        st = self._run
        if cmd is None:
            return st.cmd if st is not None else None
        if isinstance(cmd, Node):
            return cmd
        if isinstance(cmd, click.Context):
            cmd = cmd.command
        if isinstance(cmd, click.Command):
            if st is not None and st.cmd is not None and st.cmd.cmd is cmd:
                return st.cmd
            return self.node_for(cmd)
        return None

    def _run_for(self, cmd: Any) -> tuple[RunState, Node]:
        """The active run state for cmd, or a programming error when an App output
        method is called outside the command that execute is running."""
        st = self._run
        if st is None:
            raise internal_error(
                "App output methods must be called from a command run by App.execute."
            )
        node = self._resolve_node(cmd)
        if node is None or (st.cmd is not None and node is not st.cmd):
            raise internal_error("App output methods must receive the running command.")
        if st.cmd is None:
            st.cmd = node
        return st, node

    def _record_coverage(self, st: RunState, code: int) -> None:
        """Append the executed command to <PREFIX>_COVERAGE_FILE when set (the
        recorder hook for VER-003 / R06)."""
        path = self._env(self._prefix() + "_COVERAGE_FILE").strip()
        if not path or st.cmd is None:
            return
        line = go_json(Ordered(command=st.cmd.path, exit=code), escape_html=True) + "\n"
        line = go_json({"command": st.cmd.path, "exit": code}, escape_html=True) + "\n"
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(path, flags, 0o600)
        except OSError:
            return
        try:
            os.write(fd, line.encode("utf-8"))
        except OSError:
            pass
        finally:
            os.close(fd)
