# Vendored from ai-native-cli-spec template/python/aicli.
# Do not edit in tool repos; run scripts/sync-spec.js.
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sean Guo

"""Agent UX: every usage mistake is a structured E_USAGE error that names the
problem and the fix (CLI-012), suggestions search the whole tree (CLI-022), and
unexpected positional arguments are errors, never ignored (CLI-023). When the
library can build the corrected command line, next_action is that command."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import click

from ._tree import Flag, FlagParseError, FlagValueError, Node, UsePos, positionals_of
from .contract_gen import SCHEMA_VERSION, SPEC_VERSION
from .envelope import FieldSelection
from .errors import Error, internal_error, usage
from .registry import Meta, flag_type, is_global

if TYPE_CHECKING:
    from .app import App

# The verbs agents guess first for the contract's standard verbs (CLI-021, CLI-022,
# the fleet guess list). They are used for a word that App.verb_synonyms does not
# list, and only to suggest: to accept a guess, list it in Meta.aliases.
DEFAULT_VERB_SYNONYMS: dict[str, list[str]] = {
    "ls": ["list"],
    "dir": ["list"],
    "find": ["search", "list"],
    "search": ["list"],
    "list": ["search"],
    "rm": ["delete"],
    "del": ["delete"],
    "remove": ["delete"],
    "delete": ["remove", "rm"],
    "show": ["get"],
    "view": ["get"],
    "cat": ["get"],
    "read": ["get"],
    "get": ["show", "view"],
    "add": ["create"],
    "new": ["create"],
    "make": ["create"],
    "create": ["add", "new"],
    "edit": ["update"],
    "set": ["update"],
    "modify": ["update"],
    "update": ["edit", "set"],
}

# The flag names agents guess first for the shared vocabulary (CLI-028). They are
# suggested through did_you_mean, only for a flag App.flag_synonyms does not list and
# only when the command has the target flag; to accept a guess, use App.flag_aliases.
DEFAULT_FLAG_SYNONYMS: dict[str, list[str]] = {
    "per-page": ["limit"],
    "page-size": ["limit"],
    "max-results": ["limit"],
    "max": ["limit"],
    "page-token": ["cursor"],
    "next-token": ["cursor"],
    "next": ["cursor"],
    "search": ["query"],
    "keyword": ["query"],
    "keywords": ["query"],
    "term": ["query"],
    "order-by": ["sort"],
    "sort-by": ["sort"],
}

# A synopsis (details.usage) is capped at about this many characters.
USAGE_MAX = 200


def usage_line(app: App, c: Node) -> str:
    """The command's synopsis for details.usage (CLI-012): the path, its
    positionals and its flags, required ones bare and optional ones in brackets, a
    file flag beside the literal flag it replaces, and the gate flags of a write.
    Optional flags that do not fit in about 200 characters become "[...]"."""
    if c is app._root or c.has_subcommands():
        return c.command_path + " <command> [flags]"
    m = app.meta_for(c) or Meta()
    parts = [c.command_path]
    pos = positionals_of(c, m)
    for p in pos:
        s = p.name + ("..." if p.variadic else "")
        parts.append(f"<{s}>" if p.required else f"[{s}]")
    pos_flag = m.positional
    if pos_flag and not pos:
        f = c.flag(pos_flag)
        if f is not None:
            parts.append(f"<{pos_flag}>" if f.required else f"[{pos_flag}]")
    file_for = {bound: file for file, bound in m.file_inputs.items() if c.flag(bound) is not None}
    required: list[str] = []
    optional: list[str] = []
    for f in c.sorted_flags():
        name = f.name
        if f.hidden or is_global(f) or name == pos_flag or name in file_for.values():
            continue
        text = "--" + name + value_hint(m, f)
        if name in file_for and c.flag(file_for[name]) is not None:
            text += " | --" + file_for[name] + " <path>"
        if f.required or name == m.batch_flag:
            required.append(text)
        else:
            optional.append("[" + text + "]")
    parts += required
    if m.is_write():
        if m.is_dangerous():
            parts.append("--dangerous")
        parts.append("[--dry-run | --confirm <token>]")
    line = " ".join(parts)
    for o in optional:
        if len(line) + 1 + len(o) > USAGE_MAX:
            line += " [...]"
            break
        line += " " + o
    return line


def value_hint(m: Meta, f: Flag) -> str:
    """The value placeholder of a flag in a synopsis: nothing for a boolean, the
    values of a short enum, <n> for integers, <path> for files, else the flag's own
    name; a repeatable flag ends in "..."."""
    if f.no_opt_def_val:
        return ""
    typ, multiple = flag_type(f)
    hint = "<" + f.name + ">"
    doc = m.params.get(f.name)
    enum = list(doc.enum) if doc is not None and doc.enum else _choices_of(f)
    if enum and len("|".join(enum)) <= 40:
        hint = "|".join(enum)
    elif m.file_inputs.get(f.name) or (doc is not None and doc.type == "path"):
        hint = "<path>"
    elif typ == "integer":
        hint = "<n>"
    elif typ == "number":
        hint = "<number>"
    if multiple:
        hint += "..."
    return " " + hint


def max_typo(word: str) -> int:
    """How many edits still count as a typo of word: one for short words, two otherwise."""
    return 1 if len(word) <= 4 else 2


def edit_distance(a: str, b: str) -> int:
    """The optimal-string-alignment distance between a and b: insertions,
    deletions, substitutions and adjacent transpositions ("veiw" -> "view") cost one."""
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[len(a)][len(b)]


def closest_words(word: str, candidates: list[str]) -> list[str]:
    """Up to three candidates close to word."""
    cs = []
    for c in candidates:
        dist = edit_distance(word, c)
        if dist <= max_typo(word) + 1 or c.startswith(word) or word.startswith(c):
            cs.append((dist, c))
    cs.sort(key=lambda x: x[0])
    return [c for _, c in cs[:3]]


def parse_fields(raw: str) -> FieldSelection:
    """--fields: "a,b" replaces, "+a,+b" adds, "all" returns everything."""
    sel = FieldSelection(set=True)
    plus = plain = 0
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if part == "all":
            sel.all = True
            continue
        if part.startswith("+"):
            plus += 1
            part = part[1:].strip()
        else:
            plain += 1
        if part:
            sel.names.append(part)
    if plus > 0 and plain > 0:
        e = usage(
            "invalid_value", "--fields", "--fields mixes replacing (a,b) and adding (+a) fields."
        )
        e.details["expected"] = "a,b | +a,+b | all"
        raise e
    sel.add = plus > 0
    if not sel.all and not sel.names:
        e = usage("invalid_value", "--fields", "--fields is empty.")
        e.details["expected"] = "a,b | +a,+b | all"
        raise e
    return sel


def near_secret(name: str, secret: set[str]) -> bool:
    """Whether a flag name is, or is a likely misspelling of, a secret flag: its
    value may be a secret."""
    if name in secret:
        return True
    return any(
        edit_distance(name, s) <= 2
        or (len(name) >= 3 and (s.startswith(name) or name.startswith(s)))
        for s in secret
    )


def known_flag(node: Node | None, tok: str) -> bool:
    """Whether a flag token names a flag of node (long, or each of its shorthands)."""
    if node is None:
        return False
    if tok.startswith("--"):
        return node.flag(tok[2:].split("=", 1)[0]) is not None
    return bool(tok[1:2]) and node.shorthand_flag(tok[1]) is not None


def mask_shorthands(node: Node | None, tok: str, secret: set[str]) -> tuple[str, bool] | None:
    """A shorthand token (-k v, -k=v, -kv, -abk v) with the value of a secret flag
    masked: the token to show and whether the next token is the secret's value.
    None when no secret flag takes a value in it."""
    if node is None:
        return None
    s = tok[1:]
    for i, c in enumerate(s):
        f = node.shorthand_flag(c)
        if f is None:
            return None
        if f.no_opt_def_val:
            continue  # a boolean: the next letter is another shorthand
        if not near_secret(f.name, secret):
            return None
        if i + 1 < len(s):
            return "-" + s[: i + 1] + "=***", False
        return tok, True
    return None


_SHELL_SPECIAL = set("\"'`$\\|&;<>()*?[]{}!#~")


def shell_word(s: str) -> str:
    """A token quoted for a copy-paste command line when it holds spaces or shell
    metacharacters."""
    if s == "":
        return '""'
    if not any(ch <= " " or ch in _SHELL_SPECIAL for ch in s):
        return s
    return (
        '"'
        + s.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
        + '"'
    )


class UXMixin:
    """App methods for agent-friendly parsing errors and pre-run checks."""

    def verb_synonyms_for(self: App, word: str) -> list[str]:
        """The tool's synonyms for word, else the library's defaults."""
        syn = self.verb_synonyms or {}
        if word in syn:
            return list(syn[word])
        return list(DEFAULT_VERB_SYNONYMS.get(word, []))

    # ---------------------------------------------------------------- groups

    def group_run(self: App, c: Node, args: list[str]) -> None:
        """A group (or the root) called without a known subcommand: help when
        nothing follows, E_USAGE unknown_command otherwise."""
        if c is self._root:
            v = c.flag("version")
            if v is not None and v.changed and v.value:
                if args:
                    raise self.unexpected_arg(c, args, 0)
                self.ok(
                    c.cmd,
                    {
                        "tool": self.tool_name(),
                        "version": self._version(),
                        "spec_version": SPEC_VERSION,
                        "schema_version": SCHEMA_VERSION,
                    },
                )
                return
        if not args:
            self.help_func(c)
            return
        raise self.unknown_command(c, args[0])

    def sibling_matches(self: App, group: Node, word: str) -> list[str]:
        """The children of group whose name or alias is a synonym of word or a close
        spelling, as runnable paths (with the tool name), closest first."""
        syn = self.verb_synonyms_for(word)
        cands: list[tuple[int, str]] = []
        for sub in group.children:
            if sub.internal or sub.hidden:
                continue
            best = -1
            for name in [sub.name, *sub.aliases]:
                dist = edit_distance(word, name)
                if name in syn:
                    dist = 0
                elif dist <= max_typo(word):
                    pass
                elif len(word) >= 3 and (name.startswith(word) or word.startswith(name)):
                    dist = max_typo(word)
                else:
                    continue
                if best < 0 or dist < best:
                    best = dist
            if best >= 0:
                cands.append((best, sub.command_path))
        cands.sort()
        return [p for _, p in cands]

    def unknown_command(self: App, group: Node, word: str) -> Error:
        """E_USAGE unknown_command with suggestions from the group's children, the
        verb synonyms and the whole tree, and the corrected command line when there
        is a suggestion."""
        available = sorted(
            sub.name for sub in group.children if not sub.internal and not sub.hidden
        )
        suggestions = self.sibling_matches(group, word) or self.tree_matches(word, False)
        suggestions = suggestions[:3]
        msg = (
            f"`{group.command_path}` has no such subcommand; details.available lists its commands."
        )
        if suggestions:
            msg = (
                f"`{group.command_path}` has no such subcommand; details.did_you_mean lists the "
                "closest commands."
            )
        e = usage("unknown_command", word, msg)
        if suggestions:
            e.details["did_you_mean"] = suggestions
        e.details["available"] = available
        e.details["usage"] = group.command_path + " <command> [flags]"
        nxt = f"Run `{group.command_path} --help` to list every command under it."
        if suggestions:
            best = suggestions[0]
            best_words = best.removeprefix(self._root.name).removeprefix(" ").split()
            dropped = [False]
            kept: list[str] = []

            def edit(_i: int, tok: str, kind: str) -> list[str]:
                if kind == "positional" and not dropped[0] and tok == word:
                    dropped[0] = True
                    return []
                kept.append(tok)
                return [tok]

            line, ok = self.command_line(best, edit)
            # The remaining arguments are read as the suggested command reads them
            # (P11): a value of one of its flags is not a positional argument.
            _, kinds = self.classify([*best_words, *kept])
            positionals = sum(1 for k in kinds[len(best_words) :] if k == "positional")
            target = self.command_by_path(" ".join(best_words))
            is_group = target is not None and (target is self._root or target.has_subcommands())
            takes, variadic = 0, False
            if target is not None:
                m = self.meta_for(target)
                for p in positionals_of(target, m):
                    takes += p.count
                    variadic = variadic or p.variadic
                if m is not None and m.positional and takes == 0:
                    takes = 1
            if ok and not is_group and not variadic and positionals > takes:
                # The rewritten line would be refused (unexpected_argument): point to
                # the command's own help instead of a command that cannot run.
                nxt = (
                    f"Did you mean `{best}`? It does not take those positional arguments; "
                    f"run `{best} --help` for its flags."
                )
            elif ok and (not is_group or line != best):
                nxt = f"Did you mean `{best}`? Run `{line}`."
            else:
                nxt = f"Did you mean `{best}`? Run `{best} --help` for its commands and flags."
        e.details["next_action"] = nxt
        return e

    def tree_matches(self: App, word: str, leaves_only: bool) -> list[str]:
        """Commands anywhere in the tree whose name or alias matches word (exact,
        singular/plural, a verb synonym, or a close spelling), shallowest first.
        Shortcut keys count as names of their target. Paths carry the tool name."""
        if not word:
            return []
        hits: list[tuple[str, int]] = []
        singular = word.removesuffix("s")
        syn = self.verb_synonyms_for(word)

        def match(n: str) -> bool:
            return (
                n == word
                or n.removesuffix("s") == singular
                or n in syn
                or (len(word) >= 4 and edit_distance(word, n) <= 1)
            )

        seen: set[str] = set()
        for c in self._root.walk():
            if c is self._root or c.internal or c.hidden or (leaves_only and c.has_subcommands()):
                continue
            for n in [c.name, *c.aliases]:
                if match(n) and c.command_path not in seen:
                    seen.add(c.command_path)
                    hits.append((c.command_path, c.command_path.count(" ")))
                    break
        for key, to in (self.shortcuts or {}).items():
            if match(key):
                c = self.command_by_path(" ".join(to))
                if c is not None and c.command_path not in seen:
                    seen.add(c.command_path)
                    hits.append((c.command_path, 0))
        hits.sort(key=lambda h: (h[1], h[0]))
        return [p for p, _ in hits[:3]]

    def flag_names(self: App, c: Node) -> list[str]:
        """The flags a command accepts besides the global ones (details.available of
        an unknown flag that matched nothing)."""
        out = sorted("--" + f.name for f in c.flags().values() if not f.hidden and not is_global(f))
        return out[:25]

    # ---------------------------------------------------------------- flag errors

    def flag_error(self: App, c: Node, err: FlagParseError) -> Error:
        """Parse errors as E_USAGE (CLI-012, CLI-023)."""
        # A group given an unknown word followed by flags fails on the flags first;
        # the unknown word is the real mistake.
        if c is self._root or c.has_subcommands():
            word = self.first_unknown_word(c)
            if word:
                return self.unknown_command(c, word)
        use = usage_line(self, c)
        help_hint = f"Run `{c.command_path} --help` to see its arguments and flags."
        if err.kind == "not_exist":
            return self.unknown_flag(c, err.name, bool(err.shorthands), use)
        if err.kind == "value_required":
            f = err.flag
            assert f is not None
            e = usage("missing_parameter", "--" + f.name, f"--{f.name} needs a value.")
            e.details["usage"] = use
            e.details["next_action"] = (
                f"Give --{f.name} a value, as details.usage shows, and run the command again."
            )
            return e
        if err.kind == "invalid_value":
            f = err.flag
            assert f is not None
            choices = _choices_of(f)
            if choices:  # a click.Choice: the values are the type
                return enum_error(f.name, choices, err.value)
            typ, _ = flag_type(f)
            e = usage("invalid_value", "--" + f.name, f"--{f.name} needs a value of type {typ}.")
            e.details["expected"] = typ
            e.details["usage"] = use
            e.details["next_action"] = (
                f"Give --{f.name} a value of type {typ} and run the command again."
            )
            return e
        if err.kind == "invalid_syntax":
            e = usage(
                "unknown_flag",
                err.specified,
                "A flag is malformed; flags look like --name value or --name=value.",
            )
            e.details["usage"] = use
            e.details["next_action"] = help_hint
            return e
        e = usage("invalid_value", "", "The command line could not be parsed.")
        e.details["usage"] = use
        e.details["next_action"] = help_hint
        return e

    def unknown_flag(self: App, c: Node, name: str, short: bool, use: str) -> Error:
        """E_USAGE unknown_flag: the closest flags (synonyms first) and, when the top
        suggestion can take the typed flag's place, the corrected command; with no
        close flag, details.available lists the command's flags."""
        param = ("-" if short else "--") + name
        sugg = self.similar_flags(c, name)
        msg = f"`{c.command_path}` has no such flag; details.usage shows its flags."
        if sugg:
            msg = (
                f"`{c.command_path}` has no such flag; details.did_you_mean lists the closest "
                "flags."
            )
        e = usage("unknown_flag", param, msg)
        e.details["usage"] = use
        if not sugg:
            e.details["available"] = self.flag_names(c)
            e.details["next_action"] = (
                "Use one of the flags in details.available (details.usage shows how), or drop the "
                "flag."
            )
            return e
        e.details["did_you_mean"] = sugg
        nxt = f"Use {sugg[0]} instead, as details.usage shows."
        f = c.flag(sugg[0].removeprefix("--"))
        # A mistyped secret flag carries a secret: rewrite only to that secret flag
        # (whose value is masked), never to a neighbour that would echo it.
        secrets = self.secret_flag_names()
        risky = any(s.removeprefix("--") in secrets and s != sugg[0] for s in sugg)
        if f is not None and not risky:
            line, ok = self.replace_flag(c, name, short, f)
            if ok:
                nxt = f"Run `{line}`."
            elif not f.no_opt_def_val:
                hint = value_hint(self.meta_for(c) or Meta(), f).strip()
                if f.def_value not in ("", "[]"):
                    nxt = (
                        f"Use {sugg[0]} {hint} instead (default {f.def_value}), as details.usage "
                        "shows."
                    )
                else:
                    nxt = f"Use {sugg[0]} {hint} instead, as details.usage shows."
        e.details["next_action"] = nxt
        return e

    def replace_flag(self: App, c: Node, name: str, short: bool, f: Flag) -> tuple[str, bool]:
        """The run's command line with the unknown flag token replaced by f, keeping
        its value. It fails when f needs a value the token did not carry."""
        from .app import split_flag

        st = self._run
        if st is None:
            return "", False
        _, kinds = self.classify(st.args)
        bad = -1
        for i, tok in enumerate(st.args):
            if kinds[i] != "flag":
                continue
            if short and tok.startswith("-") and not tok.startswith("--") and name in tok:
                bad = i
                break
            if not short and tok.startswith("--") and split_flag(tok)[0] == name:
                bad = i
                break
        if bad < 0:
            return "", False
        _, value, inline = split_flag(st.args[bad])
        takes = not f.no_opt_def_val
        if short and len(st.args[bad].removeprefix("-")) != 1 and not inline:
            return "", False  # combined shorthands: leave them to the agent
        if takes and not inline:
            nxt = bad + 1
            if nxt >= len(st.args) or kinds[nxt] in ("flag", "word"):
                return "", False

        def edit(i: int, tok: str, _kind: str) -> list[str]:
            if i != bad:
                return [tok]
            if inline:
                return ["--" + f.name + "=" + value]
            return ["--" + f.name]

        return self.command_line(c.command_path, edit)

    def first_unknown_word(self: App, c: Node) -> str:
        """The first non-flag word after group c in the run's arguments that is
        not one of its subcommands."""
        st = self._run
        if st is None:
            return ""
        target, kinds = self.classify(st.args)
        if target is not c:
            return ""
        for i, k in enumerate(kinds):
            if k == "positional":
                return st.args[i]
        return ""

    def similar_flags(self: App, c: Node, name: str) -> list[str]:
        """Flags of c close to name: synonyms first (the tool's, else the library's
        defaults), then spelling mistakes by edit distance, then flags that only
        share a prefix."""
        cands: list[tuple[int, str]] = []
        tool_synonyms = self.flag_synonyms or {}
        if name in tool_synonyms:
            synonyms = list(tool_synonyms[name])
        else:
            synonyms = list(DEFAULT_FLAG_SYNONYMS.get(name, []))
        for f in c.sorted_flags():
            if f.hidden or f.name == "help":
                continue
            dist = edit_distance(name, f.name)
            if f.name in synonyms:
                dist = -1
            elif dist <= max_typo(name):
                dist *= 2
            elif len(name) >= 3 and (f.name.startswith(name) or name.startswith(f.name)):
                dist = 2 * max_typo(name) + 1
            else:
                continue
            cands.append((dist, "--" + f.name))
        cands.sort()
        return [n for _, n in cands[:3]]

    # ---------------------------------------------------------------- positionals

    def validate_args(self: App, c: Node, args: list[str]) -> None:
        """Positional arguments (CLI-023): a command accepts only the positionals it
        declares (its click arguments, or one for Meta.positional); more are E_USAGE
        unexpected_argument, never silently ignored. A required click argument that
        is missing is E_USAGE missing_parameter."""
        m = self.meta_for(c)
        pos = positionals_of(c, m)
        max_args = sum(p.count for p in pos if not p.variadic)
        min_args = sum(p.count for p in pos if p.required and not p.variadic)
        variadic = any(p.variadic for p in pos)
        if m is not None and m.positional:
            if max_args == 0:
                max_args = 1
            min_args = 0  # the flag can be given instead
        if len(args) > max_args and not variadic:
            raise self.unexpected_arg(c, args, max_args)
        if c.arguments and len(args) < min_args:
            raise self.missing_arg(c, pos, len(args))
        if c.arguments and any(p.required and p.variadic for p in pos) and len(args) <= max_args:
            raise self.missing_arg(c, pos, len(args))

    def unexpected_arg(self: App, c: Node, args: list[str], allowed: int) -> Error:
        """Positional arguments a command does not take. parameter names the first
        one by position (args[i]), so a value typed in the wrong place is not
        echoed. When the stray value clearly belongs to one flag (the only required
        flag still unset, or the batch flag), next_action is the corrected command."""
        idx = max(min(allowed, len(args) - 1), 0)
        takes = "no positional arguments"
        if allowed == 1:
            takes = "one positional argument"
        elif allowed > 1:
            takes = f"{allowed} positional arguments"
        e = usage(
            "unexpected_argument",
            f"args[{idx}]",
            f"`{c.command_path}` takes {takes}; pass the other values with their flags.",
        )
        e.details["usage"] = usage_line(self, c)
        nxt = "Pass each value with its flag, as details.usage shows."
        if len(args) > idx:
            flag, value = self.stray_flag(c, args[idx:], allowed)
            if flag:
                seen = [0]

                def edit(_i: int, tok: str, kind: str) -> list[str]:
                    if kind != "positional":
                        return [tok]
                    seen[0] += 1
                    return [] if seen[0] > allowed else [tok]

                line, ok = self.command_line(c.command_path, edit, "--" + flag, value)
                if ok:
                    nxt = f"Pass it as --{flag}: `{line}`."
        e.details["next_action"] = nxt
        return e

    def stray_flag(self: App, c: Node, stray: list[str], allowed: int) -> tuple[str, str]:
        """The flag stray positional values belong to, when that is clear: the batch
        flag (values joined with commas), or the only required flag still unset."""
        m = self.meta_for(c)
        if m is None or not stray:
            return "", ""
        if m.batch_flag:
            f = c.flag(m.batch_flag)
            if f is not None and not f.changed:
                return m.batch_flag, ",".join(stray)
        if len(stray) != 1:
            return "", ""
        unset = [
            f.name
            for f in c.sorted_flags()
            if f.required and not f.changed and not (f.name == m.positional and allowed > 0)
        ]
        if len(unset) == 1:
            return unset[0], stray[0]
        return "", ""

    def missing_arg(self: App, c: Node, pos: list[UsePos], got: int) -> Error:
        name = ""
        seen = 0
        for p in pos:
            if seen >= got and p.required:
                name = p.name
                break
            seen += p.count if not p.variadic else max(0, got - seen)
        e = usage("missing_parameter", name, f"`{c.command_path}` needs the <{name}> argument.")
        e.details["usage"] = usage_line(self, c)
        e.details["next_action"] = (
            f"Give the <{name}> argument, as details.usage shows, and run the command again."
        )
        return e

    # ---------------------------------------------------------------- command lines

    def command_line(
        self: App, path: str, edit: Callable[[int, str, str], list[str]] | None = None, *extra: str
    ) -> tuple[str, bool]:
        """A copy-paste command line: path (with the tool name) and the run's
        arguments without their command words, each token passed through edit
        (which may drop or replace it), then extra. Values of secret flags are
        shown as ***. ok is False outside a run."""
        st = self._run
        if st is None:
            return "", False
        node, kinds = self.classify(st.args)
        toks: list[tuple[str, str]] = [(w, "word") for w in path.split()]
        for i, tok in enumerate(st.args):
            if kinds[i] == "word":
                continue
            for t in edit(i, tok, kinds[i]) if edit is not None else [tok]:
                toks.append((t, kinds[i]))
        toks += [(t, "flag" if t.startswith("--") else "value") for t in extra]
        return self.render_command([t for t, _ in toks], [k for _, k in toks], node), True

    def secret_flag_names(self: App) -> set[str]:
        """Every flag name a command declares secret (Meta.secrets with its twins)
        and the flags of Secret settings (B2)."""
        names = {s for m in self._reg.meta.values() for s in m.secrets}
        names.update(s.flag for s in self.settings or [] if s.secret and s.flag)
        return names

    def render_command(
        self: App, tokens: list[str], kinds: list[str], node: Node | None = None
    ) -> str:
        """Tokens joined into one shell-ready line, quoting where needed and masking
        the values of flags declared secret and of flags whose names are close to a
        secret flag's (D6). kinds are the classify labels of the tokens, so a
        secret's value is masked however it was given: --long v, --long=v, -s v,
        -s=v, -sv, or a value that starts with "-"."""
        from .app import split_flag

        secret = self.secret_flag_names()
        out: list[str] = []
        mask_next = False
        for tok, kind in zip(tokens, kinds, strict=False):
            if mask_next and (kind != "flag" or not known_flag(node, tok)):
                out.append("***")
                mask_next = False
                continue
            mask_next = False
            if kind == "flag" and tok.startswith("--") and len(tok) > 2:
                name, _, inline = split_flag(tok)
                if near_secret(name, secret):
                    if inline:
                        out.append("--" + name + "=***")
                    else:
                        out.append(tok)
                        mask_next = True
                    continue
            elif kind == "flag" and tok.startswith("-") and len(tok) > 1:
                masked = mask_shorthands(node, tok, secret)
                if masked is not None:
                    out.append(masked[0])
                    mask_next = masked[1]
                    continue
            out.append(shell_word(tok))
        return " ".join(out)

    # ---------------------------------------------------------------- pre-run checks

    def prepare_run(self: App, c: Node, args: list[str]) -> None:
        """Before a leaf's own code: fix the output mode, bind a lone positional to
        Meta.positional, and check required flags, flag groups, file inputs, enums
        and --fields, so the command never runs on a bad command line."""
        st = self._run
        if st is None:
            return
        st.cmd = c
        m = self.meta_for(c)
        self.apply_output_flags(c)
        if m is not None and m.positional and len(args) == 1:
            f = c.flag(m.positional)
            if f is None:
                raise internal_error("Meta.positional names a flag the command does not define.")
            if f.changed:
                if f.string() != args[0]:
                    e = usage(
                        "conflicting_flags",
                        "--" + f.name,
                        f"The argument and --{f.name} give different values.",
                    )
                    e.details["next_action"] = (
                        f"Give the value once, either as the argument or as --{f.name}."
                    )
                    raise e
            else:
                try:
                    f.set(args[0], self._click_ctx(c))
                except FlagValueError:
                    typ, _ = flag_type(f)
                    e = usage(
                        "invalid_value",
                        "--" + f.name,
                        f"The argument must be a value of type {typ}.",
                    )
                    e.details["expected"] = typ
                    raise e from None
            st.positional_consumed = True
        if m is not None:
            # after Meta.positional is bound, so a deprecated flag given as the
            # positional is noticed too (Go's order, D10)
            self.deprecation_notices(c, m)
        self.check_required(c)
        if m is not None:
            self.check_file_inputs(c, m)
            self.check_enums(c, m)
        self.check_fields(c, m)

    def deprecation_notices(self: App, c: Node, m: Meta) -> None:
        """A deprecated notice for each use of a deprecated command and of each
        deprecated flag given on the command line (CLI-081)."""
        from .envelope import Notice
        from .registry import replacement_text
        from .selfupdate import parse_version

        if replacement_text(m.deprecated):
            tool = self.tool_name()
            msg = f"`{c.path}` is deprecated: {replacement_text(m.deprecated)}."
            try:
                msg += (
                    f" It will be removed in {tool} {parse_version(self._version()).major + 1}.0.0."
                )
            except ValueError:
                msg += f" It will be removed in the next major version of {tool}."
            self.notice(Notice(type="deprecated", severity="warning", message=msg))
        for name in sorted(m.deprecated_flags):
            f = c.flag(name)
            text = replacement_text(m.deprecated_flags[name])
            if f is not None and f.changed and text:
                self.notice(
                    Notice(
                        type="deprecated",
                        severity="warning",
                        message=f"`--{name}` is deprecated: {text}.",
                    )
                )

    def apply_output_flags(self: App, c: Node) -> None:
        """--format/--json/--pretty/--quiet/--fields for the run."""
        st = self._run
        assert st is not None
        fmt_flag = c.flag("format")
        fmt = (fmt_flag.string() if fmt_flag is not None else "").strip().lower()
        json_alias = bool(c.flag("json") and c.flag("json").value)
        st.pretty = bool(c.flag("pretty") and c.flag("pretty").value)
        st.quiet = bool(c.flag("quiet") and c.flag("quiet").value)
        format_set = fmt_flag is not None and fmt_flag.changed
        if json_alias and format_set and fmt != "json":
            st.format = "json"
            e = usage("conflicting_flags", "--json", "--json and --format name different formats.")
            e.details["next_action"] = "Drop --json (JSON is the default) or drop --format."
            raise e
        if json_alias or fmt == "":
            fmt = "json"
        m = self.meta_for(c)
        supported = ["json", "text"]
        if m is not None and m.raw_output:
            supported.append("raw")
        if fmt not in supported:
            st.format = "json"
            e = usage(
                "unsupported_format",
                "--format",
                f"`{c.command_path}` does not support that format.",
            )
            e.details["expected"] = "|".join(supported)
            e.details["next_action"] = (
                "Drop --format (JSON is the default) or use one of: " + ", ".join(supported) + "."
            )
            raise e
        st.format = fmt
        fields_flag = c.flag("fields")
        if fields_flag is not None and fields_flag.changed:
            st.fields = parse_fields(fields_flag.string())
            if fmt == "raw":
                e = usage(
                    "conflicting_flags", "--fields", "--fields does not apply to --format raw."
                )
                e.details["next_action"] = "Drop --fields or use JSON output."
                raise e

    def check_fields(self: App, c: Node, m: Meta | None) -> None:
        """Field names the command's schema does not declare (CLI-040). context
        accepts every top-level key it returns (P13)."""
        st = self._run
        assert st is not None
        if not st.fields.set or st.fields.all:
            return
        fields = list(self.schema_of(m).fields)
        if c.builtin and c.name == "context" and c.parent is self._root:
            fields = self.context_fields()
        if not fields:
            return
        unknown = [n for n in st.fields.names if n not in fields and n != "ref"]
        if unknown:
            e = usage(
                "invalid_value",
                "--fields",
                f"--fields names {len(unknown)} field(s) this command does not return.",
            )
            e.details["expected"] = ",".join(fields)
            e.details["did_you_mean"] = closest_words(unknown[0], fields)
            e.details["next_action"] = "Use field names from details.expected, or --fields all."
            raise e

    def check_required(self: App, c: Node) -> None:
        """Missing required flags and violated flag groups (Meta.mutually_exclusive,
        required_together, one_required; cobra's flag groups) before the command runs."""
        missing = sorted(f.name for f in c.flags().values() if f.required and not f.changed)
        if missing:
            e = usage(
                "missing_parameter",
                "--" + missing[0],
                f"`{c.command_path}` needs --{', --'.join(missing)}.",
            )
            e.details["usage"] = usage_line(self, c)
            e.details["next_action"] = (
                f"Add --{missing[0]} <value>; run `{c.command_path} --help` for details."
            )
            raise e
        m = self.meta_for(c)
        if m is None:
            return

        def changed(name: str) -> bool:
            f = c.flag(name)
            return f is not None and f.changed

        # cobra's order: mutually exclusive, required together, one required; each
        # kind's groups by their names
        for names in sorted(m.mutually_exclusive, key=" ".join):
            given = [n for n in names if changed(n)]
            if len(given) > 1:
                e = usage(
                    "conflicting_flags", "--" + given[1], f"Use only one of --{', --'.join(names)}."
                )
                e.details["usage"] = usage_line(self, c)
                raise e
        for names in sorted(m.required_together, key=" ".join):
            unset = [n for n in names if not changed(n)]
            if unset and len(unset) < len(names):
                e = usage(
                    "missing_parameter",
                    "--" + unset[0],
                    f"--{', --'.join(names)} must be given together.",
                )
                e.details["usage"] = usage_line(self, c)
                raise e
        for names in sorted(m.one_required, key=" ".join):
            if names and not any(changed(n) for n in names):
                e = usage(
                    "missing_parameter",
                    "--" + names[0],
                    f"Give at least one of --{', --'.join(names)}.",
                )
                e.details["usage"] = usage_line(self, c)
                raise e

    def check_file_inputs(self: App, c: Node, m: Meta) -> None:
        """A literal and a file for the same bound value, and two flags reading stdin."""
        stdin_flags = []
        for file in sorted(m.file_inputs):
            bound = m.file_inputs[file]
            ff = c.flag(file)
            if ff is None or not ff.changed:
                continue
            lf = c.flag(bound)
            if lf is not None and lf.changed and lf is not ff:
                e = usage(
                    "conflicting_flags", "--" + file, f"Give --{bound} or --{file}, not both."
                )
                e.details["next_action"] = f"Drop --{bound} or --{file}."
                raise e
            if ff.string() == "-":
                stdin_flags.append(file)
        if len(stdin_flags) > 1:
            e = usage(
                "conflicting_flags", "--" + stdin_flags[1], "Only one flag can read stdin (-)."
            )
            e.details["next_action"] = "Write the other input to a file and pass its path."
            raise e

    def check_enums(self: App, c: Node, m: Meta) -> None:
        """Flags that declare allowed values in Meta.params."""
        for name in sorted(m.params):
            doc = m.params[name]
            f = c.flag(name)
            if f is None or not f.changed or not doc.enum:
                continue
            values = f.slice_strings() if f.multiple else [f.string()]
            for v in values:
                if v not in doc.enum:
                    raise enum_error(name, list(doc.enum), v)


def enum_error(name: str, allowed: list[str], value: str) -> Error:
    e = usage("invalid_value", "--" + name, f"--{name} accepts one of a fixed set of values.")
    e.details["expected"] = "|".join(allowed)
    e.details["did_you_mean"] = closest_words(value, allowed)
    e.details["next_action"] = f"Use one of: {', '.join(allowed)}."
    return e


def _choices_of(f: Flag) -> list[str]:
    if f.param is not None and isinstance(getattr(f.param, "type", None), click.Choice):
        return [str(x) for x in f.param.type.choices]
    return []
