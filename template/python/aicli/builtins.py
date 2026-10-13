# Vendored from ai-native-cli-spec template/python/aicli.
# Do not edit in tool repos; run scripts/sync-spec.js.
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sean Guo

"""The built-in commands: reference, context, doctor, changelog, operation
show/list/resolve (CLI-031 .. CLI-037, CLI-061) and, when App.update is set, update
(CLI-070, update.py)."""

from __future__ import annotations

import datetime as _dt
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import click

from ._tree import Node
from ._util import Ordered
from .contract_gen import (
    CHANGELOG_CATEGORIES,
    CONTEXT_REQUIRED_KEYS,
    DOCTOR_STATUS_VALUES,
    OPERATION_OPTIONAL_KEYS,
    OPERATION_RECORD_KEYS,
    OPERATION_STATUS_VALUES,
    RESOLUTION_OUTCOMES,
    SPEC_VERSION,
    SUCCESS_BUDGET_CHARS,
    UPDATE_ERROR_CODES,
    UPDATE_OPTIONAL_KEYS,
    UPDATE_REQUIRED_KEYS,
)
from .envelope import fit_operation_budget, success_size
from .errors import Error, io_error, not_found, usage, validation
from .journal import (
    CorruptRecord,
    Journal,
    Record,
    Resolution,
    TargetRef,
    effective_status,
    unresolved,
)
from .keystore import protection
from .query import Page
from .registry import EXPERIMENTAL, OBSERVE, Meta, ParamDoc, SchemaDef
from .scope import ScopeNeed, ScopeState
from .token import valid_operation_id

if TYPE_CHECKING:
    from .app import App


@dataclass
class Check:
    """One doctor check (CLI-036). status is pass, warn or fail."""

    check: str
    status: str
    message: str
    fix: str = ""


def redact_host(h: str) -> str:
    """A host URL without user info, query and fragment; user info goes also
    without a scheme (user:token@host)."""
    h = (h or "").strip()
    i = h.find("://")
    scheme, rest = (h[: i + 3], h[i + 3 :]) if i >= 0 else ("", h)
    authority = rest.split("/", 1)[0]
    at = authority.rfind("@")
    if at >= 0:
        rest = rest[at + 1 :]
    h = scheme + rest
    for j, ch in enumerate(h):
        if ch in "?#":
            h = h[:j]
            break
    return h


# ---------------------------------------------------------------- changelog

_VERSION_LINE = re.compile(
    r"##[ \t\n\f\r]+\[?v?([0-9][0-9A-Za-z.+-]*)\]?(?:[ \t\n\f\r]*[-–—][ "
    r"\t\n\f\r]*([0-9]{4}-[0-9]{2}-[0-9]{2}))?"
)
_CATEGORY_LINE = re.compile(r"###[ \t\n\f\r]+([0-9A-Za-z_]+)")
_BULLET_LINE = re.compile(r"[ \t\n\f\r]*[-*+][ \t\n\f\r]+(.*[^ \t\n\f\r])")
_LINK_REF = re.compile(r"[ \t]*\[[^\]]+\]:[ \t]*\S")


@dataclass
class ChangelogEntry:
    version: str
    date: str
    changes: dict[str, list[str]]


def parse_changelog(text: str) -> list[ChangelogEntry]:
    """A Keep a Changelog document; [Unreleased] is skipped, unknown categories
    count as "changed", HTML comments are skipped, and a continuation line ends at
    a blank line or a link-reference definition (decision 26)."""
    out: list[ChangelogEntry] = []
    cur: ChangelogEntry | None = None
    category = ""
    continuing = False
    in_comment = False
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        had_text = line.strip() != ""
        if in_comment:
            end = line.find("-->")
            if end < 0:
                continue
            line = line[end + 3 :]
            in_comment = False
        while "<!--" in line:
            start = line.find("<!--")
            end = line.find("-->", start + 4)
            if end < 0:
                line = line[:start]
                in_comment = True
                break
            line = line[:start] + line[end + 3 :]
        if had_text and not line.strip():
            continue  # a comment-only line is not a blank line
        if line.startswith("## "):
            if cur is not None:
                out.append(cur)
                cur = None
            m = _VERSION_LINE.match(line)
            if m:
                cur = ChangelogEntry(m.group(1), m.group(2) or "", {})
            category = ""
            continuing = False
            continue
        if cur is None:
            continue
        m = _CATEGORY_LINE.match(line)
        if m:
            category = m.group(1).lower()
            if category not in CHANGELOG_CATEGORIES:
                category = "changed"
            continuing = False
            continue
        if not category:
            continue
        m = _BULLET_LINE.match(line)
        if m and not line.startswith("    "):
            cur.changes.setdefault(category, []).append(m.group(1))
            continuing = True
            continue
        t = line.strip()
        if not t:
            continuing = False
            continue
        if _LINK_REF.match(line):
            continuing = False
            continue
        if continuing and cur.changes.get(category):
            items = cur.changes[category]
            items[-1] += " " + t.lstrip("-*+ ")
    if cur is not None:
        out.append(cur)
    return out


def dev_version(changelog: str) -> str:
    """A development build's version (P14): the next patch after the highest version
    heading of the changelog, with the prerelease dev ("1.2.17" gives "1.2.18-dev";
    no released version gives "0.0.0-dev")."""
    from .selfupdate import compare_versions, parse_version

    newest = None
    for e in parse_changelog(changelog or ""):
        try:
            v = parse_version(e.version)
        except ValueError:
            continue
        if newest is None or compare_versions(v, newest) > 0:
            newest = v
    if newest is None:
        return "0.0.0-dev"
    return f"{newest.major}.{newest.minor}.{newest.patch + 1}-dev"


# The truncated_reason of a changelog cut to the budget (P3).
CHANGELOG_TRUNCATED = (
    "Only the newest entries fit the size budget; run changelog --since <version> for the "
    "changes after a version."
)


def fit_changelog(data: dict[str, Any], entries: list[Any]) -> dict[str, Any]:
    """Keep changelog data without --since within the success budget (CLI-042, P3):
    the newest entries that fit (at least one), with truncated, truncated_reason and
    omitted (the number left out) when some are left out. The envelope is measured
    compact with empty meta, keeping 500 characters for meta."""

    def cut(n: int) -> dict[str, Any]:
        out = dict(data)
        out["entries"], out["count"] = entries[:n], n
        if n < len(entries):
            out["truncated"] = True
            out["truncated_reason"] = CHANGELOG_TRUNCATED
            out["omitted"] = len(entries) - n
        return out

    def fits(n: int) -> bool:
        return success_size(cut(n), Ordered(duration_ms=0)) <= SUCCESS_BUDGET_CHARS - 500

    if len(entries) <= 1 or fits(len(entries)):
        return data
    keep, over = 1, len(entries)
    while keep + 1 < over:
        mid = (keep + over) // 2
        if fits(mid):
            keep = mid
        else:
            over = mid
    return cut(keep)


# ---------------------------------------------------------------- Skill frontmatter

_FRONTMATTER = re.compile(r"^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)")
_META_VERSION = re.compile(r"^[ \t]+version:[ \t]*\"?([^\"\s#]+)\"?", re.MULTILINE)
_META_MIN_VERSION = re.compile(r"^[ \t]+min_version:[ \t]*\"?([^\"\s#]+)\"?", re.MULTILINE)


def skill_versions(text: str) -> tuple[str, str]:
    """metadata.version and metadata.min_version of a SKILL.md frontmatter."""
    m = _FRONTMATTER.match(text)
    if not m:
        return "", ""
    body = m.group(1)
    v = _META_VERSION.search(body)
    mv = _META_MIN_VERSION.search(body)
    return (v.group(1) if v else ""), (mv.group(1) if mv else "")


def deref(s: str | None) -> str:
    return "unknown" if s is None else s


# The keys of an operation list row (a batch row adds item_count).
OPERATION_ROW_KEYS = (
    "operation_id",
    "command",
    "status",
    "target",
    "effects",
    "created_at",
    "finished_at",
    "batch_id",
)


class BuiltinsMixin:
    """App methods behind the built-in commands."""

    def install_builtins(self: App) -> None:
        """Create the built-in commands: reference, context, doctor, changelog,
        operation show/list/resolve and, with App.update, update. execute calls it
        when the tool has not. A tool command already using a built-in name is left
        in place and reported by the tree check."""
        if self._builtins is not None:
            return
        tool = self.tool_name()
        self.schema(
            "aicli_reference",
            SchemaDef(
                shape="object",
                fields=[
                    "tool",
                    "version",
                    "spec_version",
                    "schema_version",
                    "risk_tier",
                    "global_flags",
                    "commands",
                    "schemas",
                    "error_codes",
                    "exit_codes",
                    "groups",
                    "command",
                ],
            ),
        )
        self.schema("aicli_context", SchemaDef(shape="object", fields=list(CONTEXT_REQUIRED_KEYS)))
        self.schema("aicli_doctor", SchemaDef(shape="object", fields=["checks"]))
        self.schema(
            "aicli_changelog",
            SchemaDef(shape="object", fields=["current_version", "since", "count", "entries"]),
        )
        self.schema(
            "aicli_operation",
            SchemaDef(
                shape="object",
                fields=[*OPERATION_RECORD_KEYS, *OPERATION_OPTIONAL_KEYS],
                untrusted=[
                    "preview.changes",
                    "preview.target.label",
                    "receipt.observed",
                    "resolution.evidence",
                ],
            ),
        )
        self.schema(
            "aicli_operations",
            SchemaDef(shape="page", fields=[*OPERATION_ROW_KEYS, "item_count"]),
        )
        # The built-ins are tested by the library's own tests in the spec repository,
        # which run against a temporary state directory, not a recorded real upstream,
        # so they are experimental (VER-005); the evidence names those tests at the
        # spec tag, a reference that resolves from any tool repository.
        evidence = [
            "simulated:https://github.com/fatecannotbealtered/ai-native-cli-spec/blob/v"
            + SPEC_VERSION
            + "/template/python/aicli/tests/test_builtins.py"
        ]

        def meta(**kw: Any) -> Meta:
            return Meta(effects=[OBSERVE], maturity=EXPERIMENTAL, evidence=list(evidence), **kw)

        def cb(fn: Any) -> Any:
            def callback(**_kw: Any) -> None:
                fn()

            return callback

        ref = click.Command(
            "reference",
            callback=cb(self._run_reference),
            params=[
                click.Argument(["path"], nargs=-1, required=False),
                click.Option(
                    ["--index"],
                    is_flag=True,
                    help="Return one compact entry per command (or the groups when the list is "
                    "long)",
                ),
                click.Option(
                    ["--command"],
                    help="Return one command's entry with its output schema and error codes",
                ),
            ],
            short_help="Describe the commands: --index for a compact list, --command <path> for "
            "one command",
            help=(
                "Agents use --index (one line per command) or --command <path> (one command with "
                "its flags, output fields and errors). Without flags it returns the full registry "
                "for tooling such as validators and generators; it is large, so agents do not need "
                "it."
            ),
        )
        ctx_cmd = click.Command(
            "context",
            callback=cb(self._run_context),
            short_help="Show the tool, account, host, credentials, state directory and granted "
            "scope (local only)",
        )
        doc = click.Command(
            "doctor",
            callback=cb(self._run_doctor),
            short_help="Check credentials, connectivity, the state store, the scope and the "
            "version",
        )
        cl = click.Command(
            "changelog",
            callback=cb(self._run_changelog),
            params=[click.Option(["--since"], help="Only versions newer than this one")],
            short_help="Show what changed between versions",
        )
        op = click.Group("operation", short_help="Look up and resolve recorded write operations")
        show = click.Command(
            "show",
            callback=cb(self._run_operation_show),
            params=[
                click.Option(
                    ["--id"], help="Operation id (op_... or opb_...), or the write's confirm token"
                )
            ],
            short_help="Show one operation: its plan, status and receipt",
        )
        lst = click.Command(
            "list",
            callback=cb(self._run_operation_list),
            params=[
                click.Option(
                    ["--state"],
                    help="Only operations in this state (all: every record, planned and expired "
                    "too)",
                ),
                click.Option(
                    ["--limit"], type=int, default=10, help="Maximum operations to return (1-200)"
                ),
                click.Option(
                    ["--offset"],
                    type=int,
                    default=0,
                    help="Skip this many operations (from next_offset)",
                ),
                click.Option(["--items"], is_flag=True, help="Also list the items of batches"),
                click.Option(
                    ["--batch"], help="Only the items of this batch (opb_...), in input order"
                ),
            ],
            short_help="List dispatched operations, newest first (a batch is one row)",
        )
        res = click.Command(
            "resolve",
            callback=cb(self._run_operation_resolve),
            params=[
                click.Option(["--id"], help="Operation id (op_...), or the write's confirm token"),
                click.Option(
                    ["--outcome"],
                    required=True,
                    help="What the upstream shows: applied, not_applied or partial",
                ),
                click.Option(
                    ["--evidence"],
                    required=True,
                    help="How the outcome was checked (what was looked at)",
                ),
            ],
            short_help="Record what happened to an operation whose outcome is unknown",
        )
        self.register(
            ref,
            meta(
                summary=ref.short_help,
                output="aicli_reference",
                examples=[tool + " reference --index", tool + ' reference --command "<path>"'],
            ),
        )
        self.register(
            ctx_cmd,
            meta(summary=ctx_cmd.short_help, output="aicli_context", examples=[tool + " context"]),
        )
        self.register(
            doc, meta(summary=doc.short_help, output="aicli_doctor", examples=[tool + " doctor"])
        )
        self.register(
            cl,
            meta(
                summary=cl.short_help,
                output="aicli_changelog",
                error_codes=["E_USAGE"],
                examples=[tool + " changelog --since 1.0.0"],
            ),
        )
        self.group(op, op.short_help or "")
        self.register(
            show,
            meta(
                summary=show.short_help,
                output="aicli_operation",
                positional="id",
                error_codes=["E_NOT_FOUND", "E_IO"],
                examples=[tool + " operation show --id <operation_id>"],
            ),
        )
        self.register(
            lst,
            meta(
                summary=lst.short_help,
                output="aicli_operations",
                error_codes=["E_NOT_FOUND", "E_IO"],
                params={"state": ParamDoc(enum=[*OPERATION_STATUS_VALUES, "all"])},
                examples=[tool + " operation list --state unknown"],
            ),
        )
        self.register(
            res,
            meta(
                summary=res.short_help,
                output="aicli_operation",
                positional="id",
                error_codes=["E_NOT_FOUND", "E_CONFLICT", "E_FORBIDDEN", "E_VALIDATION", "E_IO"],
                params={"outcome": ParamDoc(enum=list(RESOLUTION_OUTCOMES))},
                examples=[
                    tool
                    + ' operation resolve --id <operation_id> --outcome applied --evidence "<what '
                    'you checked>"'
                ],
            ),
        )
        root = [ref, ctx_cmd, doc, cl]
        if self.update is not None:
            root.append(self._update_builtin())
        self._builtins = {
            "root": root,
            "operation": op,
            "operation_children": [show, lst, res],
        }

    def _update_builtin(self: App) -> click.Command:
        """The update built-in (CLI-070): registered when App.update is set. It
        changes the tool itself, outside the confirm gate, so its effects are
        observe (CLI-025's documented exception)."""
        tool = self.tool_name()
        self.schema(
            "aicli_update",
            SchemaDef(shape="object", fields=[*UPDATE_REQUIRED_KEYS, *UPDATE_OPTIONAL_KEYS]),
        )
        summary = "Update the tool and its Skills to the latest release, or to --target-version"

        def callback(**_kw: Any) -> None:
            self._run_update()

        cmd = click.Command(
            "update",
            callback=callback,
            params=[
                click.Option(
                    ["--check"],
                    is_flag=True,
                    help="Only look: report whether a newer release exists and change nothing "
                    "(same as --dry-run)",
                ),
                click.Option(
                    ["--target-version"],
                    help="Install this version (strict semver, e.g. 2.1.0) instead of the latest "
                    "release",
                ),
                click.Option(
                    ["--allow-downgrade"],
                    is_flag=True,
                    help="Allow --target-version to be older than the installed version",
                ),
            ],
            short_help=summary,
        )
        self.register(
            cmd,
            Meta(
                summary=summary,
                effects=[OBSERVE],
                maturity=EXPERIMENTAL,
                evidence=[
                    "simulated:https://github.com/fatecannotbealtered/ai-native-cli-spec/blob/v"
                    + SPEC_VERSION
                    + "/template/python/aicli/tests/test_update.py"
                ],
                output="aicli_update",
                error_codes=list(UPDATE_ERROR_CODES),
                examples=[
                    tool + " update --check",
                    tool + " update",
                    tool + " update --target-version 2.1.0",
                ],
            ),
        )
        return cmd

    def _run_update(self: App) -> None:
        from .update import run_update

        node = self._running()
        self.ok(node.cmd, run_update(self, node))

    def _running(self: App) -> Node:
        st = self._run
        assert st is not None and st.cmd is not None
        return st.cmd

    # ---------------------------------------------------------------- reference

    def _run_reference(self: App) -> None:
        node = self._running()
        index = bool(node.flag("index").value)
        command_flag = node.flag("command")
        command = command_flag.string()
        words = " ".join(self._run.positionals).strip()
        if index and command_flag.changed:
            e = usage("conflicting_flags", "--command", "Use --index or --command, not both.")
            t = self.tool_name()
            e.details["next_action"] = (
                f'Run `{t} reference --index`, then `{t} reference --command "<path>"`.'
            )
            raise e
        if command_flag.changed or (not index and words):
            self.ok(node.cmd, self.reference_command((command + " " + words).strip()))
            return
        if index:
            self.ok(node.cmd, self.reference_index(words))
            return
        self.ok(node.cmd, self.reference_full())

    # ---------------------------------------------------------------- context

    def _run_context(self: App) -> None:
        self.ok(self._running().cmd, self.context_data())

    def context_fields(self: App) -> list[str]:
        """Every top-level key context returns, which --fields accepts (P13): the
        required keys, profile when settings are declared, and the keys of
        App.context_extra."""
        fields = list(CONTEXT_REQUIRED_KEYS)
        if self.settings is not None:
            fields.append("profile")
        if self.context_extra is not None:
            for k in sorted(self.context_extra() or {}):
                if k not in fields:
                    fields.append(k)
        return fields

    def context_data(self: App) -> dict[str, Any]:
        """The context built-in's data (CLI-035): local reads only."""
        d, available, problem = self.inspect_state_dir()
        account: str | None = None
        host: str | None = None
        creds: dict[str, Any] = {"configured": False, "source": "none"}
        if self.identity is not None:
            creds["source"] = "unknown"
            try:
                ident = self.identity()
            except Exception:
                ident = None
            if ident is not None:
                if ident.account:
                    account = ident.account
                h = redact_host(ident.host)
                if h:
                    host = h
                creds["configured"] = bool(ident.account)
                src = (ident.extra or {}).get("credentials_source", "")
                if src:
                    creds["source"] = src
        auth: dict[str, Any] = {
            "status": "missing",
            "scope_id": None,
            "path": None,
            "effects": [],
            "dangerous": False,
            "expires_at": None,
        }
        if d:
            sc = self.load_scope(d)
            auth = self.authorization_summary(sc)
            if self._budget_spent(sc, d):
                auth["status"] = "budget_spent"
        # dir is null when no state directory can be resolved, and protection is
        # null whenever the store is unavailable (decision 15); reason says why.
        state: dict[str, Any] = {"dir": d or None, "available": available, "protection": None}
        if available:
            state["protection"] = protection(d)
        elif problem.strip():
            state["reason"] = problem
        data: dict[str, Any] = {
            "tool": self.tool_name(),
            "version": self._version(),
            "spec_version": SPEC_VERSION,
            "account": account,
            "host": host,
            "credentials": creds,
            "state": state,
            "authorization": auth,
        }
        if self.settings is not None:
            data["profile"] = self.profile_context()
        if self.context_extra is not None:
            for k, v in (self.context_extra() or {}).items():
                if k not in data or k == "credentials":
                    data[k] = v
        return data

    def _budget_spent(self: App, sc: ScopeState, state_dir: str) -> bool:
        """Whether a granted scope has used up limits.max_operations, by the durable
        counter (read-only: it never creates the journal)."""
        if sc.status != "granted" or sc.file is None or sc.file.max_operations is None:
            return False
        try:
            j = self.open_journal(state_dir, False)
            used = self.scope_used(j, sc.file.scope_id)
        except Error:
            return False
        return used >= sc.file.max_operations

    # ---------------------------------------------------------------- doctor

    def _run_doctor(self: App) -> None:
        self.ok(self._running().cmd, {"checks": self.doctor_data()})

    def _skill_dirs(self: App) -> list[str]:
        if self.skill_dirs is not None:
            return list(self.skill_dirs)
        home = os.path.expanduser("~")
        return [os.path.join(home, ".agents", "skills"), os.path.join(home, ".claude", "skills")]

    def _version_check(self: App) -> Check:
        """doctor's version check (decision 22): the installed Skill against this
        tool's version."""
        from .selfupdate import compare_versions, parse_version

        tool = self.tool_name()
        found = []
        for d in self._skill_dirs():
            p = os.path.join(d, tool, "SKILL.md")
            try:
                with open(p, encoding="utf-8") as fh:
                    text = fh.read(256 << 10)
            except OSError:
                continue
            v, mv = skill_versions(text)
            found.append((p, v, mv))
        try:
            cur = parse_version(self._version())
        except ValueError:
            cur = None
        # A development build (P14) reports its derived version but is not compared:
        # update, the usual fix, refuses such a build.
        if self._dev_build():
            cur = None
        if not found:
            if self.min_skill_version and cur is not None:
                try:
                    need = parse_version(self.min_skill_version)
                except ValueError:
                    need = None
                if need is not None and compare_versions(cur, need) < 0:
                    return Check(
                        "version",
                        "fail",
                        f"The Skill needs {tool} {self.min_skill_version} or newer; "
                        f"{self._version()} is installed.",
                        f"Run `{tool} update`.",
                    )
            return Check(
                "version",
                "warn",
                f"The {tool} Skill is not installed, so agents work without it.",
                "Install the Skill with the install command in the tool's README; update does "
                "not install a missing Skill.",
            )
        worst: Check | None = None
        rank = {"pass": 0, "warn": 1, "fail": 2}
        for p, v, mv in found:
            c = self._skill_check(tool, cur, p, v, mv)
            if worst is None or rank[c.status] > rank[worst.status]:
                worst = c
        assert worst is not None
        return worst

    def _skill_check(self: App, tool: str, cur: Any, path: str, v: str, mv: str) -> Check:
        from .selfupdate import compare_versions, parse_version

        if cur is None:
            return Check(
                "version",
                "warn",
                f"{tool} {self._version()} is a development build; the Skill at {path} cannot be "
                "compared.",
            )
        sync = f"Run `{tool} update` to sync the Skill from the installed package."
        min_v = skill_v = None
        for key, text in (("min_version", mv), ("version", v)):
            if not text:
                continue
            try:
                parsed = parse_version(text)
            except ValueError:
                return Check(
                    "version",
                    "warn",
                    f"The Skill at {path} has a metadata.{key} that is not a semantic version.",
                    sync,
                )
            if key == "min_version":
                min_v = parsed
            else:
                skill_v = parsed
        if min_v is not None and compare_versions(cur, min_v) < 0:
            return Check(
                "version",
                "fail",
                f"The Skill at {path} needs {tool} {mv} or newer; {self._version()} is installed.",
                f"Run `{tool} update`.",
            )
        if skill_v is None or compare_versions(skill_v, cur) < 0:
            return Check(
                "version",
                "warn",
                f"The Skill at {path} is older than {tool} {self._version()}.",
                f"Run `{tool} update` to sync the Skill from the installed package.",
            )
        return Check(
            "version",
            "pass",
            f"{tool} {self._version()} matches the Skill at {path} (min_version {mv or 'unset'}).",
        )

    def doctor_data(self: App) -> list[dict[str, Any]]:
        """The required checks (CLI-036); the tool's doctor_checks replace library
        checks of the same name and add their own."""
        checks: list[Check] = []
        fix_creds = "Configure the credentials as the tool's README describes."
        if self.identity is None:
            checks.append(Check("credentials", "warn", "This tool reports no account."))
        else:
            try:
                ident = self.identity()
                err = None
            except Exception as exc:
                ident, err = None, exc
            if err is not None:
                from ._util import truncate_runes
                from .errors import as_error

                checks.append(
                    Check(
                        "credentials",
                        "fail",
                        "The account cannot be resolved: "
                        + truncate_runes(as_error(err).message, 160),
                        fix_creds,
                    )
                )
            elif ident is None or not ident.account:
                checks.append(Check("credentials", "warn", "No account is configured.", fix_creds))
            else:
                checks.append(Check("credentials", "pass", "Credentials are configured."))
        checks.append(Check("connectivity", "warn", "This tool has no connectivity check."))
        d, available, problem = self.inspect_state_dir()
        prefix = self._prefix()
        # the same protection value context reports (decision 30)
        if not available:
            fix = f"Set {prefix}_STATE_DIR to a private directory."
            if d:
                fix = f"Fix {d} or set {prefix}_STATE_DIR to a private directory."
            checks.append(
                Check(
                    "state_store",
                    "fail",
                    "The state store is unusable: "
                    + problem.removesuffix(".")
                    + ". Writes are refused.",
                    fix,
                )
            )
        elif protection(d) == "windows-unverified":
            checks.append(
                Check(
                    "state_store",
                    "warn",
                    "The state directory is outside your user profile, so its protection depends "
                    "on that directory's ACL (windows-unverified).",
                    f"Keep {prefix}_STATE_DIR inside your profile, or give the directory an "
                    "owner-only ACL.",
                )
            )
        else:
            checks.append(
                Check("state_store", "pass", "The state store is usable (" + protection(d) + ").")
            )
        if d:
            sc = self.load_scope(d)
            if sc.status == "granted":
                assert sc.file is not None
                exp = "no expiry" if sc.file.expires_at is None else "expires " + sc.file.expires_at
                checks.append(
                    Check(
                        "authorization",
                        "pass",
                        f"Scope {sc.file.scope_id} grants {', '.join(sc.file.effects)} ({exp}).",
                    )
                )
            elif sc.status == "missing":
                checks.append(
                    Check(
                        "authorization",
                        "warn",
                        "No scope is granted, so every write is refused.",
                        f"A person grants writes by creating {sc.path}; see the tool's "
                        "SECURITY.md.",
                    )
                )
            elif sc.status == "invalid":
                checks.append(
                    Check(
                        "authorization",
                        "fail",
                        "The scope file is invalid: " + sc.reason + ".",
                        f"Ask the user to fix {sc.path}.",
                    )
                )
            else:
                checks.append(
                    Check(
                        "authorization",
                        "warn",
                        "The scope is " + sc.status + ", so every write is refused.",
                        f"Ask the user to renew {sc.path}.",
                    )
                )
        else:
            checks.append(
                Check(
                    "authorization",
                    "fail",
                    "The state directory is unknown, so no scope can be read.",
                    f"Set {prefix}_STATE_DIR to a private directory.",
                )
            )
        if self.settings is not None:
            checks.append(self.settings_check())
        if self.update is not None and not self._offline:
            # refresh the update notice (latest release, 3 s, failures ignored)
            from .update import refresh_update_notice

            refresh_update_notice(self)
        checks.append(self._version_check())
        if self.doctor_checks is not None:
            for tc in self.doctor_checks() or []:
                replaced = False
                for i, c in enumerate(checks):
                    if c.check == tc.check:
                        checks[i] = tc
                        replaced = True
                if not replaced:
                    checks.append(tc)
        out = []
        for c in checks:
            status = c.status if c.status in DOCTOR_STATUS_VALUES else "fail"
            # A passing check has no fix; a failing one always has one (decision 15).
            fix: str | None = None
            if status == "pass":
                pass
            elif (c.fix or "").strip():
                fix = c.fix
            elif status == "fail":
                fix = "Fix what the message names, then run doctor again."
            message = c.message
            if not (message or "").strip():
                message = f"The {c.check} check reported {status}."
            out.append({"check": c.check, "status": status, "message": message, "fix": fix})
        return out

    # ---------------------------------------------------------------- changelog

    def _run_changelog(self: App) -> None:
        from .selfupdate import compare_versions, parse_version

        node = self._running()
        since_flag = node.flag("since")
        since = since_flag.string()
        since_v = None
        if since_flag.changed:
            try:
                since_v = parse_version(since)
            except ValueError:
                e = usage("invalid_value", "--since", "--since needs a version like 1.2.3.")
                e.details["expected"] = "semantic version, e.g. 1.2.3"
                raise e from None
        entries = []
        for e in parse_changelog(self.changelog):
            if since_v is not None:
                try:
                    v = parse_version(e.version)
                except ValueError:
                    continue
                if compare_versions(v, since_v) <= 0:
                    continue
            changes = {
                cat: list(e.changes[cat]) for cat in CHANGELOG_CATEGORIES if e.changes.get(cat)
            }
            entries.append({"version": e.version, "date": e.date or None, "changes": changes})
        data: dict[str, Any] = {
            "current_version": self._version(),
            "entries": entries,
            "count": len(entries),
        }
        if since_v is not None:
            data["since"] = since
        else:
            data = fit_changelog(data, entries)
        self.ok(node.cmd, data)

    # ---------------------------------------------------------------- operation

    def _operation_id_arg(self: App, node: Node) -> list[str]:
        """The ids operation show and resolve look up from --id (or the positional).
        Besides an operation id it accepts the same id in upper case and the write's
        confirm token, whose id part names the operation: the agent often holds the
        token it just sent. This only finds a record; show is read-only and resolve
        still checks the scope, so it grants nothing."""
        from .token import parse_token
        from .ux import usage_line

        op_id = node.flag("id").string().strip().strip("<>\"'`").lower()
        find = f"Run `{self.tool_name()} operation list --limit 5` to find it."
        if not op_id:
            e = usage("missing_parameter", "--id", "Give the operation id.")
            e.details["usage"] = usage_line(self, node)
            e.details["next_action"] = "Give the operation_id from the write's output. " + find
            raise e
        if valid_operation_id(op_id):
            return [op_id]
        parsed = parse_token(op_id)
        if parsed is not None:
            return ["op_" + parsed[0], "opb_" + parsed[0]]
        e = usage("invalid_value", "--id", "The operation id has the wrong form.")
        e.details["expected"] = (
            "op_ or opb_ followed by 26 characters a-z and 2-7, or the write's confirm token"
        )
        e.details["next_action"] = "Copy operation_id from the write's output. " + find
        raise e

    def _read_journal(self: App) -> tuple[Journal, str]:
        """The journal without creating anything (read-only built-ins)."""
        d = self.state_dir_path()
        return self.open_journal(d, False), d

    @staticmethod
    def _read_operation(j: Journal, ids: list[str]) -> tuple[str, Record | None, bool, bool]:
        """The first of ids in the journal: (id, record, exists, corrupt). Raises E_IO
        when a record file cannot be read."""
        for op_id in ids:
            try:
                r, exists = j.read(op_id)
            except CorruptRecord:
                return op_id, None, True, True
            except OSError as exc:
                raise io_error(
                    "The operation record cannot be read.", f"Check {j.dir}.", exc
                ) from None
            if exists:
                return op_id, r, True, False
        return ids[0], None, False, False

    def _run_operation_show(self: App) -> None:
        node = self._running()
        ids = self._operation_id_arg(node)
        j, _ = self._read_journal()
        op_id, r, exists, corrupt = self._read_operation(j, ids)
        if not exists:
            raise not_found(
                "No operation with that id is recorded by this tool.",
                f"Run `{self.tool_name()} operation list --limit 5` to see recorded operations.",
            )
        if corrupt or r is None:
            self.ok(node.cmd, self.corrupt_view(op_id, j.mod_time(op_id)))
            return
        view, notice = fit_operation_budget(self.operation_view(j, r, True), self.tool_name())
        if notice is not None:
            self.notice(notice)
        self.ok(node.cmd, view)

    def corrupt_view(self: App, op_id: str, mod: _dt.datetime) -> dict[str, Any]:
        """The view of a record file that cannot be trusted (CLI-058): its outcome is
        unknown and it blocks every target until it is resolved. Its own fields are
        unreadable, so its times are the file's last write, its effects are every
        write effect of the tool (it could be any of its writes) and the rest is null."""
        from ._util import time_string

        v: dict[str, Any] = {k: None for k in OPERATION_RECORD_KEYS}
        at = time_string(mod)
        v["operation_id"] = op_id
        v["command"] = "unknown"
        v["status"] = "unknown"
        v["effects"] = self.any_write_effects()
        v["created_at"] = at
        v["dispatched_at"] = at
        return v

    def any_write_effects(self: App) -> list[str]:
        """Every write effect the tool declares (change when it declares none), for
        records whose own effects are unreadable."""
        return self._tool_write_need().effects or ["change"]

    def operation_view(self: App, j: Journal, r: Record, full: bool) -> dict[str, Any]:
        """The operation show view (BLUEPRINT 4.7)."""
        from .gate import target_object

        now = self._now()
        status = "resolved" if r.resolution is not None else effective_status(r, now)
        v: dict[str, Any] = {
            "operation_id": r.operation_id,
            "command": r.command,
            "status": status,
            "target": target_object(r.target) if r.target is not None else None,
            "effects": list(r.effects or []),
            "scope_id": r.scope_id,
            "created_at": r.created_at,
            "expires_at": r.expires_at or None,
            "dispatched_at": r.dispatched_at,
            "finished_at": r.finished_at,
            "preview": None,
            "receipt": None,
            # a map in Go: its keys are written in sorted order
            "resolution": dict(r.resolution.to_json()) if r.resolution is not None else None,
            "batch_id": r.batch_id,
            "items": None,
        }
        if full:
            if r.preview is not None:
                v["preview"] = r.preview if isinstance(r.preview, dict) else {}
            if r.receipt is not None:
                v["receipt"] = r.receipt if isinstance(r.receipt, dict) else {}
            # A batch record without item ids replaced one that could not be read
            # (operation resolve): its items are still unknown, so items stays null.
            if r.kind == "batch" and r.items:
                items = []
                for item_id in r.items or []:
                    entry: dict[str, Any] = {
                        "operation_id": item_id,
                        "target": None,
                        "status": "unknown",
                    }
                    try:
                        it, exists = j.read(item_id)
                    except (CorruptRecord, OSError):
                        it, exists = None, False
                    if exists and it is not None:
                        entry["status"] = (
                            "resolved" if it.resolution is not None else effective_status(it, now)
                        )
                        if it.target is not None:
                            entry["target"] = target_object(it.target)
                    items.append(entry)
                v["items"] = items
        return v

    def _run_operation_list(self: App) -> None:
        """Recorded operations, newest first (CLI-061). By default what an agent
        looks for after a write: dispatched operations, a batch as one row with its
        item count. --state <state> (or all) also shows planned and expired records
        and the items of batches; --items lists batch items without a filter; --batch
        lists the items of one batch (_list_batch_items)."""
        node = self._running()
        status = node.flag("state").string()
        limit = node.flag("limit").value
        offset = node.flag("offset").value
        with_items = bool(node.flag("items").value)
        if limit is None or limit < 1 or limit > 200:
            e = usage("invalid_value", "--limit", "--limit must be between 1 and 200.")
            e.details["expected"] = "1-200"
            e.details["next_action"] = (
                "Give --limit a value from 1 to 200 and run the command again."
            )
            raise e
        if offset is None or offset < 0:
            e = usage("invalid_value", "--offset", "--offset must not be negative.")
            e.details["expected"] = "0 or more"
            e.details["next_action"] = (
                "Give --offset the next_offset of the previous page, or leave it out."
            )
            raise e
        batch = node.flag("batch")
        if batch.changed:
            self._list_batch_items(node, batch.string(), status, limit, offset)
            return
        j, d = self._read_journal()
        if status:
            with_items = True
        rows = []
        for entry in j.list():
            item_count = -1
            if entry.corrupt or entry.rec is None:
                v = self.corrupt_view(
                    entry.id, _dt.datetime.fromtimestamp(entry.mod_time, _dt.timezone.utc)
                )
                created = v["created_at"]
            else:
                if entry.rec.kind == "item" and not with_items:
                    continue
                v = self.operation_view(j, entry.rec, False)
                created = entry.rec.created_at
                # A batch whose item list was lost with its unreadable record has no count.
                if entry.rec.kind == "batch" and entry.rec.items:
                    item_count = len(entry.rec.items)
            st = v.get("status")
            if status == "all":
                pass
            elif status:
                if st != status:
                    continue
            elif st in ("planned", "expired"):
                continue
            summary = {k: v.get(k) for k in OPERATION_ROW_KEYS}
            if item_count >= 0:
                summary["item_count"] = item_count
            rows.append((created, summary))
        rows.sort(key=lambda r: r[1]["operation_id"])
        rows.sort(key=lambda r: r[0], reverse=True)
        scope: dict[str, Any] = {
            "journal": os.path.join(d, "operations"),
            "state": "dispatched operations (planned and expired are left out; --state all shows "
            "them)",
            "batch_items": "counted in their batch's row (--items lists them)",
        }
        if status:
            scope["state"] = status
        if with_items:
            scope["batch_items"] = "listed"
        self._operation_page(node, [r[1] for r in rows], offset, limit, scope, "created_at_desc")

    def _operation_page(
        self: App,
        node: Node,
        rows: list[dict[str, Any]],
        offset: int,
        limit: int,
        scope: dict[str, Any],
        order: str,
    ) -> None:
        """rows[offset:offset+limit] as a page of operation list (CLI-041) within the
        success budget (CLI-042): when the rows --limit asks for do not fit one
        response, the page keeps the first rows that fit (at least one), is truncated
        with the reason, and next_offset continues after them. total counts every
        row, everything scope covers."""
        total = len(rows)
        start = min(offset, total)
        end = min(start + limit, total)
        reason = (
            f"The page holds fewer rows than --limit {limit}, as many as fit one response "
            f"({SUCCESS_BUDGET_CHARS} characters, CLI-042); continue from next_offset."
        )

        def fits(n: int) -> bool:
            # meta is added later: keep room for it.
            data = {
                "items": rows[start : start + n],
                "count": n,
                "has_more": True,
                "next_offset": start + n,
                "truncated": True,
                "truncated_reason": reason,
                "scope": scope,
                "sort": order,
                "total": total,
            }
            return success_size(data, Ordered(duration_ms=0)) <= SUCCESS_BUDGET_CHARS - 500

        truncated = False
        if end - start > 1 and not fits(end - start):
            keep, over = 1, end - start
            while keep + 1 < over:
                mid = (keep + over) // 2
                if fits(mid):
                    keep = mid
                else:
                    over = mid
            end, truncated = start + keep, True
        items = rows[start:end]
        p = Page(
            items=items,
            count=len(items),
            has_more=end < total,
            scope=scope,
            sort=order,
            total=total,
        )
        if p.has_more:
            p.next_offset = end
        if truncated:
            p.truncated, p.truncated_reason = True, reason
        self.list(node.cmd, p)

    def _list_batch_items(
        self: App, node: Node, raw: str, status: str, limit: int, offset: int
    ) -> None:
        """operation list --batch (operation.shortening): one row per item of the
        batch record, in input order, with the keys of a list row. An item whose
        record is missing or cannot be read is a row with status unknown and a null
        target, as in operation show's items; its other keys are the batch's.
        --state filters the rows; without it (or with all) every item is listed,
        planned and expired too. A batch record that could not be read, or the record
        operation resolve put in its place, names no items: that is E_IO, not an empty
        list."""
        batch_id = raw.strip().strip("<>\"'`").lower()
        if not (batch_id.startswith("opb_") and valid_operation_id(batch_id)):
            e = usage("invalid_value", "--batch", "The batch id has the wrong form.")
            e.details["expected"] = "a batch id: opb_ followed by 26 characters a-z and 2-7"
            e.details["next_action"] = (
                "Copy the batch's operation_id (opb_...) from the write's output. Run "
                f"`{self.tool_name()} operation list --limit 5` to find it."
            )
            raise e
        j, d = self._read_journal()
        _, r, exists, corrupt = self._read_operation(j, [batch_id])
        if not exists:
            raise not_found(
                "No batch with that id is recorded by this tool.",
                f"Run `{self.tool_name()} operation list --limit 5` to see recorded operations.",
            )
        if corrupt or r is None or r.kind != "batch" or not r.items:
            raise io_error(
                "The batch's items are unknown: its record could not be read.",
                f"Run `{self.tool_name()} operation show --id {batch_id}` to see what is recorded "
                "about it.",
            )
        rows: list[dict[str, Any]] = []
        for item_id in r.items:
            row: dict[str, Any] = {
                "operation_id": item_id,
                "command": r.command,
                "status": "unknown",
                "target": None,
                "effects": list(r.effects or []),
                "created_at": r.created_at,
                "finished_at": None,
                "batch_id": batch_id,
            }
            try:
                it, exists = j.read(item_id)
            except (CorruptRecord, OSError):
                it, exists = None, False
            if exists and it is not None:
                v = self.operation_view(j, it, False)
                row.update((k, v.get(k)) for k in OPERATION_ROW_KEYS)
            if status and status != "all" and row["status"] != status:
                continue
            rows.append(row)
        scope: dict[str, Any] = {
            "journal": os.path.join(d, "operations"),
            "batch": batch_id,
            "state": "every item of the batch",
        }
        if status:
            scope["state"] = status
        self._operation_page(node, rows, offset, limit, scope, "input_order")

    def _run_operation_resolve(self: App) -> None:
        node = self._running()
        ids = self._operation_id_arg(node)
        outcome = node.flag("outcome").string()
        evidence = node.flag("evidence").string().strip()
        if not evidence:
            raise usage("missing_parameter", "--evidence", "Say how the outcome was checked.")
        if len(evidence) > 2000:
            raise usage(
                "input_too_large", "--evidence", "--evidence is longer than 2000 characters."
            )
        state = self.ensure_state_dir()
        j = self.open_journal(state, False)
        op_id, r, exists, corrupt = self._read_operation(j, ids)
        if not exists:
            raise not_found(
                "No operation with that id is recorded by this tool.",
                f"Run `{self.tool_name()} operation list --state unknown`.",
            )
        mod = j.mod_time(op_id)
        now = self._now()
        if not corrupt:
            assert r is not None
            if r.kind == "batch":
                e = validation(
                    "--id",
                    "op_...",
                    "A batch is resolved through its items; resolve each item whose outcome is "
                    "unknown.",
                )
                e.details["next_action"] = (
                    f"Run `{self.tool_name()} operation list --batch {op_id} --state unknown` to "
                    "see the items to resolve."
                )
                raise e
            st = effective_status(r, now)
            if st == "dispatching":
                from .errors import conflict_error

                ce = conflict_error(
                    "lock_held",
                    "The operation is still running under its lease; it cannot be resolved yet.",
                    f"Wait until its lease ends ({deref(r.lease_until)}), then run the command "
                    "again.",
                )
                ce.details["active_operation"] = op_id
                raise ce
            if not unresolved(st) or r.resolution is not None:
                e = validation(
                    "--id",
                    "an operation whose status is unknown",
                    "Only operations whose outcome is unknown can be resolved.",
                )
                e.details["next_action"] = (
                    f"Run `{self.tool_name()} operation show --id {op_id}` to see its status."
                )
                raise e
        # Resolving needs a scope covering the operation's effects (CLI-061). An
        # unreadable record could be any of this tool's writes, so it needs them all.
        need = self._tool_write_need()
        if not corrupt:
            assert r is not None
            need = ScopeNeed(effects=list(r.effects or []), dangerous=r.dangerous)
            if r.target is not None:
                need.targets = [r.target.key]
        self.check_scope(self.load_scope(state), need, None)
        unlock = j.lock(op_id)
        try:
            try:
                cur, _ = j.read(op_id)
            except (CorruptRecord, OSError):
                cur = None
            if cur is not None:
                if not unresolved(effective_status(cur, self._now())) or cur.resolution is not None:
                    raise validation(
                        "--id",
                        "an operation whose status is unknown",
                        "The operation finished or was resolved meanwhile; nothing was changed.",
                    )
                r = cur
            else:
                # The unreadable record is replaced by one that carries the resolution;
                # its own fields stay unknown (see corrupt_view).
                from ._util import time_string

                at = time_string(mod)
                r = Record(
                    operation_id=op_id,
                    kind="single",
                    command="unknown",
                    status="resolved",
                    target=TargetRef("unknown"),
                    effects=self.any_write_effects(),
                    created_at=at,
                    dispatched_at=at,
                )
                if op_id.startswith("opb_"):
                    r.kind = "batch"
                    r.target = None
            from ._util import time_string as _ts

            r.status = "resolved"
            r.lease_until = None
            r.resolution = Resolution(outcome, evidence, _ts(now))
            j.write(r)
        finally:
            unlock()
        self.ok(node.cmd, self.operation_view(j, r, True))

    def _time(self: App) -> str:
        from ._util import time_string

        return time_string(self._now())

    def _tool_write_need(self: App) -> ScopeNeed:
        """The union of every registered command's write effects, and dangerous when
        any command is."""
        effects: set[str] = set()
        dangerous = False
        for m in self._reg.meta.values():
            if not m.is_write():
                continue
            effects.update(e for e in m.effects if e != OBSERVE)
            dangerous = dangerous or m.is_dangerous()
        return ScopeNeed(effects=sorted(effects), dangerous=dangerous)
