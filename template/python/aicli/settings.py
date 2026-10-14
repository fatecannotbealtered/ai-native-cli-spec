# Vendored from ai-native-cli-spec template/python/aicli.
# Do not edit in tool repos; run scripts/sync-spec.js.
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sean Guo

"""Settings and profiles (CLI-100, CLI-101). A tool declares its settings
(App(settings=[Setting(...)])); a person keeps per-account values in the profiles
of the settings file (<PREFIX>_SETTINGS_FILE, else <state>/settings.json);
app.setting(name) resolves one value and says where it came from: a flag of the
running command, the environment, the active profile or the default. Secrets never
come from the settings file (SEC-040): a file that sets one is invalid."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ._util import Ordered, RawNumber, go_bool_string, truncate_runes
from .errors import Error, internal_error, new_error
from .keystore import is_link_or_reparse
from .scope import _depth, _ScopeError, _strict_pairs

if TYPE_CHECKING:
    from ._tree import Node
    from .app import App
    from .builtins import Check

MAX_SETTINGS_BYTES = 64 << 10
PROFILE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
SETTINGS_FILE_KEYS = ("default_profile", "profiles")


@dataclass
class Setting:
    """One value the tool reads through app.setting (CLI-100). name is snake_case;
    env defaults to name in upper case and is read as <PREFIX>_<env>; flag
    (optional) names a flag of the running command that overrides the setting when
    it is given on the command line. A secret setting comes from that flag, the
    environment or its default, never from the settings file. default is the value
    when nothing else sets it; description says what it is for."""

    name: str
    env: str = ""
    flag: str = ""
    default: str = ""
    secret: bool = False
    description: str = ""

    def env_name(self) -> str:
        """The environment variable without the tool prefix."""
        return self.env or self.name.upper()


@dataclass
class SettingsFile:
    """A parsed settings file: the default profile and each profile's settings
    (values as text)."""

    default_profile: str | None = None
    profiles: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass
class ProfileState:
    """The settings file and the profile in force for one run."""

    path: str = ""  # the file read, or that would be read; "" when it cannot be resolved
    exists: bool = False
    reason: str = ""  # why the file is invalid; "" when it is valid or missing
    profiles: dict[str, dict[str, str]] = field(default_factory=dict)
    name: str | None = None  # the active profile
    source: str = "none"  # flag, env, config, single or none
    unknown: str = ""  # a requested profile that the file does not define
    unknown_source: str = ""  # where the unknown name came from: flag or env


def _near(word: str, known: list[str]) -> str:
    from .ux import closest_words

    near = closest_words(word, known)
    return f" (did you mean `{near[0]}`?)" if near else ""


def _ordered(v: Any) -> Any:
    """A decoded JSON value with every object as an Ordered, so it is written back
    in its key order (Q3)."""
    if isinstance(v, dict):
        return Ordered((k, _ordered(x)) for k, x in v.items())
    if isinstance(v, list):
        return [_ordered(x) for x in v]
    return v


def parse_settings_file(
    raw: bytes, settings: list[Setting], tool: str
) -> tuple[SettingsFile | None, str]:
    """Parse a settings file strictly, like the scope file: one UTF-8 JSON object
    without a BOM and without duplicate keys, shaped {"default_profile": name or
    null, "profiles": {name: {setting: string|number|bool}}}. Unknown keys, unknown
    settings, other values, any secret setting and a default_profile that names no
    profile make it invalid. Returns the file and, when it is invalid, the
    reason."""
    if raw.startswith(b"\xef\xbb\xbf"):
        return None, "it starts with a byte order mark"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, "it is not valid UTF-8"

    def reject(name: str) -> Any:
        raise _ScopeError("it is not valid JSON")

    stripped = text.lstrip(" \t\r\n")
    if not stripped:
        return None, "it is not JSON"
    try:
        dec = json.JSONDecoder(
            object_pairs_hook=_strict_pairs,
            parse_constant=reject,
            parse_int=RawNumber,
            parse_float=RawNumber,
        )
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
    # Go's order: the trailing value first, then the keys in sorted order; keys are
    # shown cut to 40 characters and profile names to 64.
    if stripped[end:].strip(" \t\r\n"):
        return None, "it holds more than one JSON value"
    for k in sorted(obj):
        if k not in SETTINGS_FILE_KEYS:
            return None, f"unknown key `{truncate_runes(k, 40)}`" + _near(
                k, list(SETTINGS_FILE_KEYS)
            )
    dp = obj.get("default_profile")
    if dp is not None and (isinstance(dp, RawNumber) or not isinstance(dp, str)):
        return None, "default_profile must be a string or null"
    profiles = obj.get("profiles")
    if profiles is None:
        profiles = {}
    if not isinstance(profiles, dict):
        return None, "profiles must be an object"
    declared = {s.name: s for s in settings}
    out = SettingsFile(default_profile=dp)
    for name in sorted(profiles):
        shown = truncate_runes(name, 64)
        if not PROFILE_NAME.fullmatch(name):
            return None, (
                f"the profile name `{shown}` is not allowed (use a-z, 0-9, _ and -, up to 64 "
                "characters)"
            )
        values = profiles[name]
        if not isinstance(values, dict):
            return None, f"profile `{shown}` must be an object"
        got: dict[str, str] = {}
        for key in sorted(values):
            key_shown = truncate_runes(key, 40)
            s = declared.get(key)
            if s is None:
                return None, (
                    f"profile `{shown}` sets `{key_shown}`, which is not a setting of {tool}"
                )
            if s.secret:
                return None, (
                    f"`{key_shown}` is a secret; secrets go in the OS keyring, not the settings "
                    "file (SEC-040)"
                )
            v = values[key]
            if isinstance(v, bool):
                got[key] = go_bool_string(v)
            elif isinstance(v, str):
                got[key] = str.__str__(v)  # a number keeps its text
            else:
                return None, (
                    f"`{key_shown}` in profile `{shown}` must be a string, number or true/false"
                )
        out.profiles[name] = got
    if dp is not None and dp not in out.profiles:
        return None, f"default_profile `{truncate_runes(dp, 64)}` is not one of its profiles"
    # the profiles in file order (App.profiles, Q3); problems were found in sorted order
    out.profiles = {name: out.profiles[name] for name in profiles}
    return out, ""


class SettingsMixin:
    """App methods for settings, the settings file and profiles."""

    def settings_path(self: App) -> str:
        """The settings file: <PREFIX>_SETTINGS_FILE (an absolute path; a relative
        one makes the file invalid), else <state>/settings.json; "" when no state
        directory can be resolved."""
        p = (self._env(self._prefix() + "_SETTINGS_FILE") or "").strip()
        if p:
            return p
        try:
            d = self.state_dir_path()
        except Error:
            return ""
        return os.path.join(d, "settings.json")

    def _read_settings_file(self: App, path: str) -> tuple[bool, SettingsFile | None, str]:
        """Whether the settings file exists, the parsed file and the reason it is
        invalid. A missing file is fine: there are no profiles. The file is a
        regular file, not a link, and on POSIX writable only by its owner, the
        current user (CLI-100)."""
        from .scope import _is_abs, file_owner_problem

        if not _is_abs(path):
            return True, None, f"{self._prefix()}_SETTINGS_FILE must be an absolute path"
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return False, None, ""
        except OSError:
            return True, None, "it cannot be read"
        if is_link_or_reparse(st):
            return True, None, "it must be a regular file, not a link"
        if not stat.S_ISREG(st.st_mode):
            return True, None, "it is not a regular file"
        problem = file_owner_problem(path, st)
        if problem:
            return True, None, problem
        if st.st_size > MAX_SETTINGS_BYTES:
            return True, None, "it is larger than 64 KiB"
        try:
            with open(path, "rb") as fh:
                raw = fh.read(MAX_SETTINGS_BYTES + 1)
        except OSError:
            return True, None, "it cannot be read"
        if len(raw) > MAX_SETTINGS_BYTES:
            return True, None, "it is larger than 64 KiB"
        f, reason = parse_settings_file(raw, list(self.settings or []), self.tool_name())
        return True, f, reason

    def _profile_state(self: App) -> ProfileState:
        """The settings file and the active profile: --profile, else
        <PREFIX>_PROFILE, else the file's default_profile, else its only profile.
        Read once per run; it never raises (an invalid file and an unknown profile
        are recorded)."""
        st = self._run
        if st is not None and isinstance(st.profile, ProfileState):
            return st.profile
        ps = ProfileState(path=self.settings_path())
        requested, source = "", "none"
        flag = self._root.persistent.get("profile") if self.settings is not None else None
        if flag is not None and flag.param is None and flag.changed and flag.string():
            requested, source = flag.string(), "flag"
        else:
            env = self._env(self._prefix() + "_PROFILE")
            if env:
                requested, source = env, "env"
        sf: SettingsFile | None = None
        if ps.path:
            ps.exists, sf, ps.reason = self._read_settings_file(ps.path)
        if not ps.reason:
            ps.profiles = dict(sf.profiles) if sf is not None else {}
            if not requested and sf is not None and sf.default_profile is not None:
                requested, source = sf.default_profile, "config"
            if requested:
                if requested in ps.profiles:
                    ps.name, ps.source = requested, source
                else:
                    ps.unknown, ps.unknown_source = requested, source
            elif len(ps.profiles) == 1:
                ps.name, ps.source = next(iter(ps.profiles)), "single"
        if ps.path:
            state = "not found"
            if ps.reason:
                state = "invalid: " + ps.reason
            elif ps.exists:
                state = "valid"
            self._debug_once(f"settings file: {ps.path} ({state})")
        if ps.name is not None:
            self._debug_once(f"active profile: {ps.name} ({ps.source})")
        else:
            self._debug_once("active profile: none")
        if st is not None:
            st.profile = ps
        return ps

    def _settings_invalid(self: App, ps: ProfileState) -> Error:
        return new_error(
            "E_CONFIG",
            f"The settings file {ps.path} is invalid: {ps.reason}.",
            {
                "dispatch_state": "not_started",
                "next_action": f"Ask the user to fix {ps.path}; `{self.tool_name()} context` shows "
                "which file is read.",
            },
        )

    def _profile_unknown(self: App, ps: ProfileState) -> Error:
        """E_CONFIG for a requested profile the settings file does not define (D5).
        The requested name is external text: it goes only in details.profile, marked
        untrusted, never in message or next_action (CLI-017). did_you_mean lists the
        closest profile names, else the file's first names."""
        from .ux import closest_words

        path = ps.path or "the settings file"
        if ps.exists:
            message = "The requested profile is not in the settings file."
            nxt = f"Use one of the profiles in details.did_you_mean, or add the profile to {path}."
        else:
            message = "The requested profile does not exist: there is no settings file."
            nxt = f"Create {path} with this profile, or run without --profile."
        e = new_error(
            "E_CONFIG",
            message,
            {
                "dispatch_state": "not_started",
                "next_action": nxt,
                "profile": truncate_runes(ps.unknown, 64),
                "source": ps.unknown_source or "flag",
            },
        )
        e.untrusted = ["profile"]
        names = sorted(ps.profiles)
        near = closest_words(ps.unknown, names) or names[:3]
        if near:
            e.details["did_you_mean"] = list(near)
        return e

    def profile(self: App) -> tuple[str, str]:
        """The active profile's name and where it came from (CLI-101): flag, env,
        config, single, or ("", "none") when no profile is active (no settings, an
        invalid settings file, or a requested profile the file does not define).
        Tools use it to key keyring entries per profile. For a command declared with
        Meta.allow_unknown_profile (P6, Q4), a requested profile the settings file
        does not have is the one returned, with its source (flag or env)."""
        if self.settings is None:
            return "", "none"
        ps = self._profile_state()
        if ps.reason:
            return "", "none"
        if ps.name is None:
            if ps.unknown and self._allows_unknown_profile():
                return ps.unknown, ps.unknown_source or "flag"
            return "", "none"
        return ps.name, ps.source

    def profiles(self: App) -> list[str]:
        """The profile names of the settings file in file order (Q3): empty without a
        file, and for an invalid one, whose E_CONFIG App.setting reports."""
        ps = self._profile_state()
        if ps.reason:
            return []
        return list(ps.profiles)

    def _allows_unknown_profile(self: App, node: Node | None = None) -> bool:
        """A command declared with Meta.allow_unknown_profile (P6, Q4); None means
        the running command."""
        if node is None:
            st = self._run
            node = st.cmd if st is not None else None
        m = self.meta_for(node) if node is not None else None
        return m is not None and m.allow_unknown_profile

    def set_profile_settings(self: App, profile: str, values: dict[str, str]) -> None:
        """Write values into one profile of the settings file (CLI-101, P5), for a
        configure command such as auth login to call from its apply. profile must
        match ^[a-z0-9][a-z0-9_-]{0,63}$ and every key must be a declared setting
        that is not secret (secrets go in the OS keyring, SEC-040). The current file
        is read with the same strict parser, and an invalid file is E_CONFIG and
        never overwritten. The profile is created when missing; every other key,
        profile and default_profile is kept, and a file without default_profile gets
        this profile as its default (Q3). The file keeps its order: default_profile
        first, then the profiles in file order (a new one last), each with its keys
        in file order (new keys last, sorted), indented by two spaces. It is written
        atomically (a temporary file in the same directory, fsync, rename) with mode
        0600; the state directory is created 0700 when the file lives there. A
        failure to write is E_IO. Raises the Error; nothing was written then, so
        apply raises it wrapped in not_dispatched."""
        from ._util import decode_generic, go_json, go_quote
        from .errors import io_error, validation
        from .journal import write_file_atomic

        if not isinstance(profile, str) or not PROFILE_NAME.fullmatch(profile):
            e = validation(
                "--profile",
                "a profile name: a-z, 0-9, _ and -, up to 64 characters",
                "The profile name is not allowed; nothing was written.",
            )
            e.details["next_action"] = (
                "Give a profile name of a-z, 0-9, _ and -, up to 64 characters, starting with a "
                "letter or a digit."
            )
            raise e
        declared = {s.name: s for s in self.settings or []}
        for k in sorted(values):
            s = declared.get(k)
            if s is None:
                raise internal_error(
                    f"App.set_profile_settings: {go_quote(k)} is not declared in App.settings."
                )
            if s.secret:
                raise internal_error(
                    f"App.set_profile_settings: {go_quote(k)} is a secret setting; secrets go in "
                    "the OS keyring, not the settings file (SEC-040)."
                )
        path = self.settings_path()
        if not path:
            self.state_dir_path()  # its own E_IO says why it cannot be resolved
            p = self._prefix()
            raise io_error(
                "The settings file cannot be found; nothing was written.",
                f"Set {p}_STATE_DIR or {p}_SETTINGS_FILE.",
            )
        exists, _, reason = self._read_settings_file(path)
        if reason:
            raise self._settings_invalid(ProfileState(path=path, exists=exists, reason=reason))
        top: dict[str, Any] = {}
        if exists:
            try:
                with open(path, "rb") as fh:
                    got = decode_generic(fh.read(MAX_SETTINGS_BYTES + 1))
            except (OSError, ValueError) as exc:
                raise io_error(
                    "The settings file cannot be read; nothing was written.", f"Check {path}.", exc
                ) from None
            if isinstance(got, dict):
                top = got
        # The file keeps its order (Q3): every object is written in its key order.
        profiles = _ordered(top.get("profiles"))
        if not isinstance(profiles, Ordered):
            profiles = Ordered()
        entry = profiles.get(profile)
        if not isinstance(entry, Ordered):
            entry = Ordered()
        for k in sorted(values):
            entry[k] = str(values[k])
        profiles[profile] = entry
        out = Ordered()
        dp = top.get("default_profile")
        # the first profile written becomes the default (Q3)
        out["default_profile"] = profile if dp is None else _ordered(dp)
        out["profiles"] = profiles
        data = (go_json(out, pretty=True) + "\n").encode("utf-8")
        d = os.path.dirname(path)
        try:
            state = self.state_dir_path()
        except Error:
            state = ""
        if state and os.path.normpath(state).lower() == os.path.normpath(d).lower():
            self.ensure_state_dir()
        try:
            write_file_atomic(d, os.path.basename(path), data)
        except OSError as exc:
            raise io_error(
                "The settings file cannot be written; nothing was changed.",
                f"Check that {d} is writable.",
                exc,
            ) from None
        st = self._run
        if st is not None:
            st.profile = None  # read again on the next setting

    def setting(self: App, name: str) -> tuple[str, str]:
        """A declared setting's value and its source (CLI-100): the running
        command's flag the setting names, when it was given on the command line
        (flag); else <PREFIX>_<ENV> (env); else the active profile (profile); else
        the default (default). An invalid settings file or an unknown profile is
        E_CONFIG whatever the value's source, except for a command declared with
        Meta.allow_unknown_profile (P6, Q4): it reads the flag, the environment and
        the default."""
        s = next((x for x in self.settings or [] if x.name == name), None)
        if s is None:
            raise internal_error(
                f"App.setting asks for `{name}`, which App.settings does not declare."
            )
        ps = self._profile_state()
        if ps.reason:
            raise self._settings_invalid(ps)
        if ps.unknown and not self._allows_unknown_profile():
            raise self._profile_unknown(ps)
        st = self._run
        if s.flag and st is not None and st.cmd is not None:
            f = st.cmd.flag(s.flag)
            if f is not None and f.changed:
                return f.string(), "flag"
        v = self._env(self._prefix() + "_" + s.env_name())
        if v != "":
            return v, "env"
        if not s.secret and ps.name is not None:
            values = ps.profiles.get(ps.name, {})
            if name in values:
                return values[name], "profile"
        return s.default, "default"

    def profile_context(self: App) -> dict[str, Any]:
        """context.profile (CLI-101): the active profile, where the choice came from,
        every profile of the settings file and the file (null when it does not
        exist). An invalid file, or a requested profile it does not define, reports
        no profile; context never fails on it."""
        ps = self._profile_state()
        if ps.reason:
            return {"name": None, "source": "none", "profiles": [], "file": ps.path or None}
        return {
            "name": ps.name,
            "source": ps.source if ps.name is not None else "none",
            "profiles": sorted(ps.profiles),
            "file": ps.path if ps.exists and ps.path else None,
        }

    def settings_check(self: App) -> Check:
        """doctor's settings check (CLI-101)."""
        from .builtins import Check

        ps = self._profile_state()
        if ps.reason:
            e = self._settings_invalid(ps)
            return Check(
                "settings",
                "fail",
                e.message,
                f"Fix {ps.path}, or move it aside to use defaults and environment variables.",
            )
        if ps.unknown:
            e = self._profile_unknown(ps)
            return Check("settings", "fail", e.message, e.details["next_action"])
        if not ps.exists:
            return Check(
                "settings", "pass", "No settings file; defaults and environment variables apply."
            )
        if ps.name is not None:
            return Check(
                "settings",
                "pass",
                f"{ps.path} is valid; profile `{ps.name}` is active ({ps.source}).",
            )
        return Check("settings", "pass", f"{ps.path} is valid; no profile is active.")

    def secret_setting_values(self: App) -> list[str]:
        """The values of the secret settings that are set (from a flag, the
        environment or the default), for redaction."""
        out: list[str] = []
        st = self._run
        for s in self.settings or []:
            if not s.secret:
                continue
            if s.flag and st is not None and st.cmd is not None:
                f = st.cmd.flag(s.flag)
                if f is not None and f.changed and f.string():
                    out.append(f.string())
            v = self._env(self._prefix() + "_" + s.env_name())
            if v:
                out.append(v)
            if s.default:
                out.append(s.default)
        return out
