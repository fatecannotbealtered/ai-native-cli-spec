# Vendored from ai-native-cli-spec template/python/aicli.
# Do not edit in tool repos; run scripts/sync-spec.js.
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sean Guo

"""Scope files (SEC-010 .. SEC-012): a person writes one to grant this tool
effects on targets for a while; the CLI only reads it and never creates or edits
it. Without a valid scope every write is refused before anything is sent.

Protection is honest (SEC-013): a scope stops injected instructions, agent drift
and wrong targets; it cannot stop an agent running as the same OS user from
editing the file."""

from __future__ import annotations

import datetime as _dt
import json
import ntpath
import os
import posixpath
import re
import stat
import sys
import urllib.parse
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ._util import date_string, go_quote, parse_time, sha256_hex, time_string, truncate_runes
from .contract_gen import (
    EFFECT_VALUES,
    SCOPE_OPTIONAL_KEYS,
    SCOPE_REQUIRED_KEYS,
    SUBTREE_BOUNDARIES,
    TARGET_MATCH_VALUES,
)
from .errors import Error, forbidden
from .keystore import IS_WINDOWS, is_link_or_reparse

if TYPE_CHECKING:
    from .app import App
    from .journal import Journal

MAX_SCOPE_BYTES = 64 << 10


def file_owner_problem(path: str, st: os.stat_result) -> str:
    """Why a human-written file (the scope file, the settings file) cannot be
    trusted on POSIX, as for confirm.key: another user can write it, or it is not
    the current user's. "" when it is fine; Windows relies on the profile ACL."""
    if IS_WINDOWS:
        return ""
    if stat.S_IMODE(st.st_mode) & 0o022:
        return f"{path} is writable by other users; only its owner may write it"
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None and st.st_uid != geteuid():
        return f"{path} is not owned by the current user"
    return ""


@dataclass
class TargetSelector:
    key: str
    match: str = ""


@dataclass
class ScopeFile:
    scope_id: str = ""
    tool: str = ""
    granted_by: str = ""
    granted_at: str = ""
    expires_at: str | None = None
    effects: list[str] = field(default_factory=list)
    dangerous: bool = False
    targets: list[TargetSelector] | None = None
    destinations: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    roots: list[str] = field(default_factory=list)
    max_operations: int | None = None
    limits_set: bool = False  # the file has a limits object (even an empty one)
    revoked: bool = False
    notes: str = ""


@dataclass
class ScopeState:
    """The resolved scope: granted, missing, invalid, expired or revoked."""

    status: str = ""
    path: str = ""
    digest: str = ""
    file: ScopeFile | None = None
    reason: str = ""

    def id(self) -> str | None:
        if self.file is not None and self.status != "invalid" and self.file.scope_id:
            return self.file.scope_id
        return None


@dataclass
class ScopeNeed:
    """What one write needs from the scope."""

    effects: list[str] = field(default_factory=list)
    dangerous: bool = False
    targets: list[str] = field(default_factory=list)
    destinations: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    operations: int = 0


class _ScopeError(ValueError):
    pass


_UTC_TIME = re.compile(
    r"[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]{1,9})?Z"
)


def _utc_time(s: str) -> _dt.datetime | None:
    """An ISO 8601 UTC time with the Z suffix (the contract's timestamp), or None."""
    return parse_time(s) if _UTC_TIME.fullmatch(s) else None


def _is_abs(p: str) -> bool:
    """filepath.IsAbs on this platform."""
    if os.name == "nt":
        return ntpath.isabs(p) and bool(ntpath.splitdrive(p)[0])
    return posixpath.isabs(p)


def _strict_pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for k, v in items:
        if k in obj:
            raise _ScopeError(f"the key {go_quote(truncate_runes(k, 40))} appears twice")
        obj[k] = v
    return obj


def _depth(v: Any, d: int = 0) -> int:
    if d > 32:
        raise _ScopeError("it is nested too deeply")
    if isinstance(v, dict):
        for x in v.values():
            _depth(x, d + 1)
    elif isinstance(v, list):
        for x in v:
            _depth(x, d + 1)
    return d


def parse_scope(raw: bytes) -> tuple[ScopeFile | None, str]:
    """Parse a scope file strictly: one UTF-8 JSON object without a BOM,
    duplicate and unknown keys rejected, required keys present and typed.
    Returns the file and, when it is invalid, the reason."""
    if raw.startswith(b"\xef\xbb\xbf"):
        return None, "it starts with a byte order mark"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, "it is not valid UTF-8"

    def reject(name: str) -> Any:
        raise _ScopeError("it is not valid JSON")

    try:
        dec = json.JSONDecoder(object_pairs_hook=_strict_pairs, parse_constant=reject)
        stripped = text.lstrip(" \t\r\n")
        if not stripped:
            return None, "it is not JSON"
        try:
            obj, end = dec.raw_decode(stripped)
        except json.JSONDecodeError:
            return None, "it is not valid JSON" if stripped[:1] in "{[" else "it is not JSON"
        _depth(obj)
    except _ScopeError as exc:
        return None, str(exc)
    except RecursionError:
        return None, "it is nested too deeply"
    if not isinstance(obj, dict):
        return None, "it is not a JSON object"
    for k in SCOPE_REQUIRED_KEYS:
        if k not in obj:
            return None, f'the required key "{k}" is missing'
    for k in obj:
        if k not in SCOPE_REQUIRED_KEYS and k not in SCOPE_OPTIONAL_KEYS:
            return None, unknown_key_reason(k)
    try:
        f = _scope_from(obj)
    except _ScopeError as exc:
        return None, str(exc)
    if stripped[end:].strip(" \t\r\n"):
        return None, "it holds more than one JSON value"
    if obj.get("effects") is None:
        return None, "effects must be an array"
    if not f.scope_id.strip():
        return None, "scope_id is empty"
    if not f.granted_by.strip():
        return None, "granted_by is empty"
    granted = _utc_time(f.granted_at)
    if granted is None:
        return None, "granted_at is not an ISO 8601 UTC time"
    if f.expires_at is not None:
        expires = _utc_time(f.expires_at)
        if expires is None:
            return None, "expires_at is not an ISO 8601 UTC time or null"
        if expires <= granted:
            return None, "expires_at is not after granted_at"
    for e in f.effects:
        if e not in EFFECT_VALUES:
            return None, f"effects lists an unknown effect {go_quote(truncate_runes(e, 40))}"
    if "targets" in obj and f.targets is None:
        return None, "targets must be an array; leave it out to allow every target"
    for t in f.targets or []:
        if not t.key:
            return None, "a targets entry has an empty key"
        if t.match and t.match not in TARGET_MATCH_VALUES:
            return None, "a targets entry has an unknown match; use subtree or exact"
    for d in f.destinations:
        if not d.strip():
            return None, "destinations holds an empty entry"
    for p in [*f.outputs, *f.roots]:
        if not _is_abs(p):
            return None, "outputs and roots must be absolute paths"
    if f.max_operations is not None and f.max_operations < 1:
        return None, "limits.max_operations must be at least 1"
    return f, ""


def unknown_key_reason(key: str) -> str:
    """An unknown key, with the closest known one."""
    from .ux import closest_words

    known = [*SCOPE_REQUIRED_KEYS, *SCOPE_OPTIONAL_KEYS, "key", "match", "max_operations"]
    reason = f"unknown key `{truncate_runes(key, 40)}`"
    near = closest_words(key, known)
    if near:
        reason += f" (did you mean `{near[0]}`?)"
    return reason


def _kind(v: Any) -> str:
    """A JSON value's kind as Go's json.UnmarshalTypeError names it."""
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, str):
        return "string"
    if isinstance(v, (int, float)):
        return "number" if isinstance(v, int) else "number " + json.dumps(v)
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return "a value"


def _type_error(field: str, want: str, v: Any) -> _ScopeError:
    return _ScopeError(f"{field} must be {want}, not {_kind(v)}")


def _want_str(v: Any, name: str, null_ok: bool = True) -> str:
    if v is None and null_ok:
        return ""
    if not isinstance(v, str):
        raise _type_error(name, "a string", v)
    return v


def _want_strs(v: Any, name: str) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list):
        raise _type_error(name, "an array", v)
    for x in v:
        if not isinstance(x, str):
            raise _type_error(name, "a string", x)
    return list(v)


def _want_bool(v: Any, name: str) -> bool:
    if v is None:
        return False
    if not isinstance(v, bool):
        raise _type_error(name, "true or false", v)
    return v


def _scope_from(obj: dict[str, Any]) -> ScopeFile:
    f = ScopeFile()
    f.scope_id = _want_str(obj.get("scope_id"), "scope_id")
    f.tool = _want_str(obj.get("tool"), "tool")
    f.granted_by = _want_str(obj.get("granted_by"), "granted_by")
    f.granted_at = _want_str(obj.get("granted_at"), "granted_at")
    exp = obj.get("expires_at")
    f.expires_at = None if exp is None else _want_str(exp, "expires_at", False)
    f.effects = _want_strs(obj.get("effects"), "effects")
    f.dangerous = _want_bool(obj.get("dangerous"), "dangerous")
    if "targets" in obj and obj["targets"] is not None:
        ts = obj["targets"]
        if not isinstance(ts, list):
            raise _type_error("targets", "an array", ts)
        f.targets = []
        for t in ts:
            if not isinstance(t, dict):
                raise _type_error("targets", "an object", t)
            for k in t:
                if k not in ("key", "match"):
                    raise _ScopeError(unknown_key_reason(k))
            f.targets.append(
                TargetSelector(
                    _want_str(t.get("key"), "targets.key"),
                    _want_str(t.get("match"), "targets.match"),
                )
            )
    f.destinations = _want_strs(obj.get("destinations"), "destinations")
    f.outputs = _want_strs(obj.get("outputs"), "outputs")
    f.roots = _want_strs(obj.get("roots"), "roots")
    limits = obj.get("limits")
    if limits is not None:
        if not isinstance(limits, dict):
            raise _type_error("limits", "an object", limits)
        for k in limits:
            if k != "max_operations":
                raise _ScopeError(unknown_key_reason(k))
        f.limits_set = True
        mo = limits.get("max_operations")
        if mo is not None:
            if isinstance(mo, bool) or not isinstance(mo, int):
                raise _type_error("limits.max_operations", "a number", mo)
            f.max_operations = mo
    f.revoked = _want_bool(obj.get("revoked"), "revoked")
    f.notes = _want_str(obj.get("notes"), "notes")
    return f


def subtree_match(sel: TargetSelector, key: str) -> bool:
    """Whether key lies within selector (SEC-012): equal, or the selector followed
    by one of / ! # : @ (a selector ending in one of them matches anything after
    it)."""
    if sel.match == "exact":
        return key == sel.key
    if key == sel.key:
        return True
    if not sel.key or not key.startswith(sel.key):
        return False
    boundaries = "".join(SUBTREE_BOUNDARIES)
    if sel.key[-1] in boundaries:
        return True
    return key[len(sel.key)] in boundaries


def destination_allowed(dest: str, granted: list[str]) -> bool:
    """A delivery destination against the granted ones (decision 29). A granted
    URL (with ://) matches by strict URL containment; any other granted
    destination matches exactly, and is a prefix only when it ends with "/", at
    "/" boundaries only. "@", ":", "#" and "!" are ordinary characters."""
    for g in granted:
        if dest == g:
            return True
        if "://" in g or "://" in dest:
            if url_within(dest, g):
                return True
            continue
        if g.endswith("/") and dest.startswith(g) and _clean_segments(dest[len(g) :]):
            return True
    return False


def _has_control(s: str) -> bool:
    return any(c < " " or c == "\x7f" for c in s)


def _clean_segments(rest: str) -> bool:
    """A non-empty remainder of plain segments: no empty, "." or ".." segment, no
    backslash and no control character."""
    if not rest or "\\" in rest or _has_control(rest):
        return False
    return all(seg not in ("", ".", "..") for seg in rest.split("/"))


_BAD_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")


def url_within(dest: str, granted: str) -> bool:
    """Scheme and host compared exactly and the path by whole segments; user info,
    queries, fragments and dot segments in the destination never match. A control
    character fails (urlsplit would silently drop tabs and newlines)."""
    if _has_control(dest) or _has_control(granted):
        return False
    try:
        d = urllib.parse.urlsplit(dest)
        g = urllib.parse.urlsplit(granted)
    except ValueError:
        return False
    if not d.scheme or not d.netloc or not g.netloc:
        return False
    if "@" in d.netloc or d.query or d.fragment or "\\" in dest or "?" in dest or "#" in dest:
        return False
    if d.scheme.lower() != g.scheme.lower() or d.netloc.lower() != g.netloc.lower():
        return False
    ds = url_segments(d.path)
    gs = url_segments(g.path)
    if ds is None or gs is None or len(gs) > len(ds):
        return False
    return ds[: len(gs)] == gs


def url_segments(escaped: str) -> list[bytes] | None:
    """An escaped URL path as decoded segments (bytes, as Go's url.PathUnescape),
    refusing malformed escapes, dot segments and encoded separators (decoded once)."""
    trimmed = escaped.strip("/")
    if not trimmed:
        return []
    out = []
    for raw in trimmed.split("/"):
        if _BAD_ESCAPE.search(raw):
            return None
        seg = urllib.parse.unquote_to_bytes(raw)
        if seg in (b".", b"..") or b"/" in seg or b"\\" in seg:
            return None
        out.append(seg)
    return out


def _fold(p: str) -> str:
    if sys.platform in ("win32", "darwin"):
        return p.lower()
    return p


class UnresolvedLink(ValueError):
    """A path whose links could not all be resolved: a symlink, junction or other
    name-surrogate reparse point is still in it after resolution (SEC-012)."""


_NAME_SURROGATE = 0x20000000  # Windows reparse tags that redirect a name (junctions, links)


def _redirects(st: os.stat_result) -> bool:
    """A symbolic link, or on Windows a junction or other name-surrogate reparse
    point. Cloud and dedup reparse points (OneDrive files) are not redirections."""
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_reparse_tag", 0) & _NAME_SURROGATE)


def _check_resolved(resolved: str, original: str) -> None:
    """Raise UnresolvedLink when a component of the resolved path is still a link
    or junction: realpath could not follow it (C2, decision 46)."""
    drive, rest = os.path.splitdrive(resolved)
    cur = drive + (os.sep if rest.startswith(("\\", "/")) else "")
    for part in [x for x in re.split(r"[\\/]+", rest) if x]:
        cur = os.path.join(cur, part) if cur else part
        try:
            st = os.lstat(cur)
        except OSError:
            return
        if _redirects(st):
            raise UnresolvedLink(f"a junction or other reparse point is in the path {original}")


def resolve_existing(p: str) -> str:
    """Links in the longest existing prefix of p resolved (os.path.realpath, which
    follows symlinks and junctions). Raises UnresolvedLink when one could not be
    followed."""
    p = os.path.normpath(p)
    rest = ""
    cur = p
    while True:
        if os.path.lexists(cur):
            resolved = os.path.realpath(cur)
            _check_resolved(resolved, p)
            return resolved if not rest else os.path.join(resolved, rest)
        parent = os.path.dirname(cur)
        if parent == cur:
            return p
        base = os.path.basename(cur)
        rest = base if not rest else os.path.join(base, rest)
        cur = parent


def resolved_within(target: str, roots: list[str]) -> bool:
    """Whether an already resolved path lies inside one of roots (each resolved
    the same way; a root that cannot be resolved matches nothing)."""
    for r in roots:
        if not _is_abs(r):
            continue
        try:
            root = resolve_existing(os.path.normpath(r))
            rel = os.path.relpath(_fold(target), _fold(root))
        except (OSError, ValueError):
            continue
        if rel == "." or (
            rel != ".." and not rel.startswith(".." + os.sep) and not os.path.isabs(rel)
        ):
            return True
    return False


def path_within(p: str, roots: list[str]) -> bool:
    """Whether a local path lies inside one of roots after resolving symbolic
    links (SEC-012)."""
    try:
        target = resolve_existing(os.path.abspath(p))
    except (OSError, ValueError):
        return False
    return resolved_within(target, roots)


def output_state(p: str) -> tuple[str, bool]:
    """A local output path resolved, and whether it exists after links are
    followed (an overwrite) or not (a new file). A path whose existence cannot be
    read counts as existing, the stricter case. Raises UnresolvedLink, OSError or
    ValueError."""
    resolved = resolve_existing(os.path.abspath(p))
    try:
        os.lstat(resolved)
    except FileNotFoundError:
        return resolved, False
    except OSError:
        return resolved, True
    return resolved, True


def split_outputs(
    paths: list[str],
) -> tuple[list[str], list[str], dict[str, str], dict[str, str]]:
    """The local paths a write names sorted into new files (which the scope's
    outputs must cover) and existing ones (an overwrite, which its roots must
    cover), after resolving links (C1, SEC-011, SEC-012), with each path's
    resolved form. refused maps a path whose links cannot be resolved (a junction
    or other reparse point realpath could not follow, C2) to the reason; it counts
    as existing, the stricter case."""
    new: list[str] = []
    existing: list[str] = []
    resolved: dict[str, str] = {}
    refused: dict[str, str] = {}
    for p in paths:
        try:
            r, exists = output_state(p)
        except UnresolvedLink as exc:
            refused[p] = str(exc)
        except ValueError:
            refused[p] = "it is not a valid path: " + truncate_runes(p, 200)
        except OSError:
            refused[p] = "the path cannot be resolved: " + p
        else:
            resolved[p] = r
            (existing if exists else new).append(p)
            continue
        existing.append(p)
    return new, existing, resolved, refused


def write_only(effects: list[str]) -> list[str]:
    return [e for e in effects if e != "observe"]


class ScopeMixin:
    def scope_path(self: App, state_dir: str) -> str:
        """The scope file: <PREFIX>_SCOPE_FILE (an absolute path; load_scope refuses
        a relative one), else <state>/scope.json. There is deliberately no flag
        (SEC-011)."""
        p = (self._env(self._prefix() + "_SCOPE_FILE") or "").strip()
        if p:
            return p
        return os.path.join(state_dir, "scope.json")

    def load_scope(self: App, state_dir: str) -> ScopeState:
        """Read and strictly validate the scope file."""
        path = self.scope_path(state_dir)
        self._debug_once("scope file: " + path)
        sc = ScopeState(path=path)

        def invalid(reason: str) -> ScopeState:
            sc.status = "invalid"
            sc.reason = reason
            return sc

        if not _is_abs(path):
            return invalid(f"{self._prefix()}_SCOPE_FILE must be an absolute path")
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            sc.status = "missing"
            return sc
        except OSError:
            return invalid("it cannot be read")
        if is_link_or_reparse(st) or not stat.S_ISREG(st.st_mode):
            return invalid("it must be a regular file, not a link")
        problem = file_owner_problem(path, st)
        if problem:
            return invalid(problem)
        if st.st_size > MAX_SCOPE_BYTES:
            return invalid("it is larger than 64 KiB")
        try:
            with open(path, "rb") as fh:
                raw = fh.read(MAX_SCOPE_BYTES + 1)
        except OSError:
            return invalid("it cannot be read")
        sc.digest = "sha256:" + sha256_hex(raw)
        f, reason = parse_scope(raw)
        if reason:
            return invalid(reason)
        assert f is not None
        sc.file = f
        if f.tool != self.tool_name():
            return invalid(
                f"it names the tool {go_quote(truncate_runes(f.tool, 40))}, not "
                f'"{self.tool_name()}"'
            )
        for d in [*f.outputs, *f.roots]:
            try:
                resolve_existing(os.path.normpath(d))
            except UnresolvedLink as exc:
                return invalid(str(exc))
            except (OSError, ValueError):
                pass
        if f.revoked:
            sc.status = "revoked"
            return sc
        if f.expires_at is not None:
            exp = parse_time(f.expires_at)
            if exp is None or not self._now() < exp:
                sc.status = "expired"
                return sc
        sc.status = "granted"
        return sc

    def check_scope(self: App, sc: ScopeState, need: ScopeNeed, j: Journal | None) -> None:
        """Refuse a write the scope does not cover (E_FORBIDDEN with
        details.scope), counting the operation budget from scope-usage.json. A new
        output file must lie under outputs and an existing one (an overwrite) under
        roots, after links are resolved (SEC-012); without roots nothing may be
        overwritten."""
        missing: dict[str, Any] = {}
        status = sc.status
        used = 0
        if sc.status == "granted":
            f = sc.file
            assert f is not None
            effs = [e for e in need.effects if e != "observe" and e not in f.effects]
            if effs:
                missing["effects"] = tuple(effs)
            if need.dangerous and not f.dangerous:
                missing["dangerous"] = True
            if f.targets is not None:
                for t in need.targets:
                    if not any(subtree_match(sel, t) for sel in f.targets):
                        missing["target"] = t
                        break
            dests = [d for d in need.destinations if not destination_allowed(d, f.destinations)]
            if dests:
                missing["destinations"] = tuple(dests)
            # A new file must be inside outputs and an existing one (an overwrite)
            # inside roots, after resolving links (C1, SEC-011); absent roots,
            # nothing may be overwritten.
            new, existing, resolved, refused = split_outputs(need.outputs)
            for p in need.outputs:
                if p in refused:
                    raise self.reparse_refusal(sc, need, p, refused[p])
            outs = [p for p in new if not resolved_within(resolved[p], f.outputs)]
            overs = [p for p in existing if not resolved_within(resolved[p], f.roots)]
            if outs:
                missing["outputs"] = tuple(outs)
            if overs:
                missing["roots"] = tuple(overs)
            if missing:
                status = "insufficient"
            elif f.max_operations is not None and need.operations > 0 and j is not None:
                n = self.scope_used(j, f.scope_id)
                if n + need.operations > f.max_operations:
                    status, used = "budget_spent", n
                else:
                    return
            else:
                return
        else:
            effs = write_only(need.effects)
            if effs:
                missing["effects"] = tuple(effs)
            if need.dangerous:
                missing["dangerous"] = True
            if need.targets:
                missing["target"] = need.targets[0]
            if need.destinations:
                missing["destinations"] = tuple(need.destinations)
            new, existing, _, _ = split_outputs(need.outputs)
            if new:
                missing["outputs"] = tuple(new)
            if existing:
                missing["roots"] = tuple(existing)
        raise self.scope_refusal(sc, status, need, missing, used)

    def reparse_refusal(
        self: App, sc: ScopeState, need: ScopeNeed, path: str, reason: str
    ) -> Error:
        """A write whose local path passes through a junction or other reparse point
        that cannot be resolved (C2): what the path leads to cannot be checked
        against the scope, and no grant can make it checkable."""
        key = "outputs"
        try:
            os.lstat(path)
            key = "roots"
        except (OSError, ValueError):
            pass
        e = self.scope_refusal(sc, "insufficient", need, {key: (path,)})
        scope = e.details.get("scope")
        if isinstance(scope, dict):
            scope.pop("grant_example", None)  # no grant covers it
        e.message = (
            "The output path cannot be checked against the scope (" + reason + "), so nothing "
            "was sent."
        )
        e.details["next_action"] = (
            "Write to a path without junctions or other reparse points; do not change the scope "
            "to work around it."
        )
        return e

    def scope_used(self: App, j: Journal, scope_id: str) -> int:
        """The operations already dispatched under scope_id: the durable count in
        <state>/scope-usage.json (A4), which pruning never touches. An unreadable
        count is E_IO: the budget cannot be checked."""
        from .journal import read_scope_usage

        return read_scope_usage(j.state_dir).get(scope_id, 0)

    def scope_refusal(
        self: App,
        sc: ScopeState,
        status: str,
        need: ScopeNeed,
        missing: dict[str, Any],
        used: int = 0,
    ) -> Error:
        """E_FORBIDDEN with details.scope (BLUEPRINT 4.5). The example grant is the
        whole file after the change: for a readable scope (insufficient, expired,
        revoked, budget spent) it is the current file with only what is missing
        added, so saving it never takes back what was granted; for a missing or
        invalid file it is a new grant for this write. missing.outputs lists new
        files whose directory is not under outputs, missing.roots existing files
        not under roots; the example adds their directories there."""
        missing.setdefault("dangerous", False)
        missing.setdefault("effects", ())
        if sc.file is not None and sc.status not in ("invalid", "missing"):
            example = self.extended_grant(sc, status, need, used)
        else:
            example = self.new_grant(need)
        details: dict[str, Any] = {
            "status": status,
            "path": sc.path,
            "scope_id": sc.id(),
            "missing": missing,
            "grant_example": example,
        }
        texts = {
            "missing": (
                "No scope grants this write, so nothing was sent.",
                "Ask the user to grant this scope by creating {} with the content of "
                "details.scope.grant_example, adjusted to what they allow; do not create or edit "
                "it yourself.",
            ),
            "invalid": (
                "The scope file is invalid (" + sc.reason + "), so nothing was sent.",
                "Ask the user to fix {}; details.scope.grant_example shows a valid grant for this "
                "write. Do not edit it yourself.",
            ),
            "expired": (
                "The scope has expired, so nothing was sent.",
                "Ask the user to renew the scope in {} (details.scope.grant_example is the file "
                "with a new expires_at); do not edit it yourself.",
            ),
            "revoked": (
                "The scope was revoked, so nothing was sent.",
                "The user revoked the scope in {}: ask them whether to grant it again "
                "(details.scope.grant_example shows it granted); do not edit it yourself.",
            ),
            "budget_spent": (
                "The scope's operation budget is spent, so nothing was sent.",
                "Ask the user to raise limits.max_operations in {} (details.scope.grant_example "
                "shows the file after the change); do not edit it yourself.",
            ),
        }
        message, nxt = texts.get(
            status,
            (
                "The scope does not cover this write (details.scope.missing), so nothing was sent.",
                "Ask the user to add what details.scope.missing lists to {} "
                "(details.scope.grant_example shows the whole file after the change); do not "
                "edit it yourself.",
            ),
        )
        return forbidden(
            message,
            {"scope": details, "dispatch_state": "not_started", "next_action": nxt.format(sc.path)},
        )

    def new_grant(self: App, need: ScopeNeed) -> dict[str, Any]:
        """An example scope file for this write alone."""
        now = self._now()
        effects = sorted(write_only(need.effects))
        example: dict[str, Any] = {
            "scope_id": f"scp_{date_string(now)}_{self.tool_name()}",
            "tool": self.tool_name(),
            "granted_by": "<your name>",
            "granted_at": time_string(now),
            "expires_at": time_string(now + _dt.timedelta(days=7)),
            "effects": tuple(effects),
            "dangerous": need.dangerous,
        }
        sel = [{"key": t} for t in need.targets[:MAX_EXAMPLE_TARGETS]]
        if sel:
            example["targets"] = sel
        if need.destinations:
            example["destinations"] = tuple(need.destinations)
        new, existing, _, _ = split_outputs(need.outputs)
        dirs = output_dirs(new)
        if dirs:
            example["outputs"] = tuple(dirs)
        dirs = output_dirs(existing)
        if dirs:
            example["roots"] = tuple(dirs)
        return example

    def extended_grant(
        self: App, sc: ScopeState, status: str, need: ScopeNeed, used: int
    ) -> dict[str, Any]:
        """The current scope file with what this write needs added: the union of
        effects, targets, destinations and outputs, dangerous when needed, a new
        expires_at only when the scope expired, a raised operation budget when it is
        spent, and the same scope_id."""
        f = sc.file
        assert f is not None
        effects = list(f.effects)
        for e in write_only(need.effects):
            if e not in effects:
                effects.append(e)
        effects.sort()
        example: dict[str, Any] = {
            "scope_id": f.scope_id,
            "tool": f.tool,
            "granted_by": f.granted_by,
            "granted_at": f.granted_at,
            "expires_at": f.expires_at,
            "effects": tuple(effects),
            "dangerous": f.dangerous or need.dangerous,
        }
        if status == "expired":
            example["expires_at"] = time_string(self._now() + _dt.timedelta(days=7))
        if f.targets is not None:
            sel: list[dict[str, Any]] = []
            for t in f.targets:
                o: dict[str, Any] = {"key": t.key}
                if t.match:
                    o["match"] = t.match
                sel.append(o)
            added = 0
            for t in need.targets:
                covered = any(subtree_match(s, t) for s in f.targets)
                if not covered and added < MAX_EXAMPLE_TARGETS:
                    sel.append({"key": t})
                    added += 1
            example["targets"] = sel
        dests = list(f.destinations)
        for d in need.destinations:
            if not destination_allowed(d, f.destinations) and d not in dests:
                dests.append(d)
        if dests:
            example["destinations"] = tuple(dests)
        new, existing, resolved, _ = split_outputs(need.outputs)
        outs = list(f.outputs)
        for d in output_dirs([p for p in new if not resolved_within(resolved[p], f.outputs)]):
            if d not in outs:
                outs.append(d)
        if outs:
            example["outputs"] = tuple(outs)
        roots = list(f.roots)
        overwritten = [
            p for p in existing if p not in resolved or not resolved_within(resolved[p], f.roots)
        ]
        for d in output_dirs(overwritten):
            if d not in roots:
                roots.append(d)
        if roots:
            example["roots"] = tuple(roots)
        if f.limits_set:
            limits: dict[str, Any] = {}
            if f.max_operations is not None:
                limits["max_operations"] = f.max_operations
                if status == "budget_spent":
                    limits["max_operations"] = max(f.max_operations, used + need.operations)
            example["limits"] = limits
        if f.notes:
            example["notes"] = f.notes
        return example

    def authorization_summary(self: App, sc: ScopeState) -> dict[str, Any]:
        """context.authorization (CLI-035)."""
        out: dict[str, Any] = {
            "status": sc.status,
            "scope_id": None,
            "path": sc.path,
            "effects": [],
            "dangerous": False,
            "expires_at": None,
        }
        if sc.status == "invalid":
            return out
        if sc.file is not None:
            out["scope_id"] = sc.file.scope_id
            out["effects"] = list(sc.file.effects)
            out["dangerous"] = sc.file.dangerous
            out["expires_at"] = sc.file.expires_at
        return out


# the targets an example grant adds at most
MAX_EXAMPLE_TARGETS = 5


def output_dirs(paths: list[str]) -> list[str]:
    """The directories of the given output paths, absolute and unique."""
    dirs: list[str] = []
    for p in paths:
        try:
            d = os.path.dirname(os.path.abspath(p))
        except (OSError, ValueError):
            continue
        if d not in dirs:
            dirs.append(d)
    return dirs


def validate_scope_file(raw: bytes) -> str:
    """The reason a scope file is invalid, or "" (the rules load_scope applies)."""
    _, reason = parse_scope(raw)
    return reason
