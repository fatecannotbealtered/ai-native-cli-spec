# Vendored from ai-native-cli-spec template/python/aicli.
# Do not edit in tool repos; run scripts/sync-spec.js.
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sean Guo

"""Self-update helpers (CLI-070, CLI-071), used by the built-in update (update.py):
targets are strict semver, package-manager argv is built only from validated parts
(never through a shell), downgrades need an explicit flag, and the update notice
comes from a local cache only."""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ._util import parse_time, time_string, truncate_runes
from .contract_gen import DOWNGRADE_FLAG, NOTICE_SEVERITY_VALUES
from .envelope import Notice
from .errors import Error, internal_error, io_error, usage

if TYPE_CHECKING:
    from .app import App
    from .envelope import RunState


@dataclass(frozen=True)
class Version:
    """A parsed semantic version (https://semver.org)."""

    major: int
    minor: int
    patch: int
    pre: str = ""
    build: str = ""

    def __str__(self) -> str:
        """The version without a leading "v"."""
        s = f"{self.major}.{self.minor}.{self.patch}"
        if self.pre:
            s += "-" + self.pre
        if self.build:
            s += "+" + self.build
        return s

    def core(self) -> str:
        """The version without build metadata (what package managers install)."""
        s = f"{self.major}.{self.minor}.{self.patch}"
        if self.pre:
            s += "-" + self.pre
        return s


_SEMVER = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9]*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9][0-9]*|[0-9]*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?",
    re.ASCII,
)
_MAX_INT = (1 << 63) - 1


def parse_version(s: str) -> Version:
    """A strict semantic version with an optional leading "v". Anything else
    (spaces, shell characters, leading zeros, missing parts) raises ValueError."""
    if not isinstance(s, str) or len(s) > 128:
        raise ValueError("version is too long")
    t = s[1:] if s.startswith("v") else s
    m = _SEMVER.fullmatch(t)
    if m is None:
        raise ValueError(f'"{truncate_runes(s, 40)}" is not a semantic version like 1.2.3')
    nums = []
    for i in range(1, 4):
        n = int(m.group(i))
        if n > _MAX_INT:
            raise ValueError("version number is out of range")
        nums.append(n)
    return Version(nums[0], nums[1], nums[2], m.group(4) or "", m.group(5) or "")


def _compare_ident(a: str, b: str) -> int:
    an = a.isdigit() and a.isascii()
    bn = b.isdigit() and b.isascii()
    if an and bn:
        x, y = int(a), int(b)
        return (x > y) - (x < y)
    if an:
        return -1
    if bn:
        return 1
    return (a > b) - (a < b)


def compare_versions(a: Version, b: Version) -> int:
    """Semver precedence (build metadata ignored): -1, 0 or 1."""
    for x, y in ((a.major, b.major), (a.minor, b.minor), (a.patch, b.patch)):
        if x != y:
            return -1 if x < y else 1
    if a.pre == b.pre:
        return 0
    if not a.pre:
        return 1
    if not b.pre:
        return -1
    ai, bi = a.pre.split("."), b.pre.split(".")
    for x, y in zip(ai, bi, strict=False):
        c = _compare_ident(x, y)
        if c:
            return c
    return (len(ai) > len(bi)) - (len(ai) < len(bi))


_NPM_NAME = re.compile(r"(?:@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*", re.ASCII)
_GO_MODULE = re.compile(r"[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(?:/[A-Za-z0-9._~-]+)+", re.ASCII)
_PIP_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", re.ASCII)
_BREW_NAME = re.compile(r"[a-z0-9][a-z0-9@._+-]*(?:/[a-z0-9][a-z0-9._-]*){0,2}", re.ASCII)


def package_manager_args(manager: str, pkg: str, v: Version) -> list[str]:
    """The argv that installs pkg at version v with a package manager: npm, pnpm,
    yarn, go, pip or brew. Every part is validated against a strict character
    set, so the argv is safe to run without a shell, including through npm.cmd on
    Windows. brew cannot pin a version: its argv upgrades to the formula's current
    version, which the caller must compare with v afterwards. pip accepts release
    versions only. Build metadata is never passed on. Raises ValueError."""
    if pkg.startswith("-") or len(pkg) > 214:
        raise ValueError(f'package name "{truncate_runes(pkg, 40)}" is not allowed')
    ver = v.core()
    if not _SEMVER.fullmatch(ver):
        raise ValueError("version is not a semantic version")
    if manager in ("npm", "pnpm", "yarn"):
        if not _NPM_NAME.fullmatch(pkg):
            raise ValueError(f'"{truncate_runes(pkg, 40)}" is not a valid npm package name')
        spec = pkg + "@" + ver
        if manager == "npm":
            return ["npm", "install", "-g", spec]
        if manager == "pnpm":
            return ["pnpm", "add", "-g", spec]
        return ["yarn", "global", "add", spec]
    if manager == "go":
        if not _GO_MODULE.fullmatch(pkg) or ".." in pkg:
            raise ValueError(f'"{truncate_runes(pkg, 40)}" is not a valid Go package path')
        return ["go", "install", pkg + "@v" + ver]
    if manager == "pip":
        if not _PIP_NAME.fullmatch(pkg):
            raise ValueError(f'"{truncate_runes(pkg, 40)}" is not a valid Python package name')
        if v.pre:
            raise ValueError("pip installs release versions only")
        return ["pip", "install", "--upgrade", pkg + "==" + ver]
    if manager == "brew":
        if not _BREW_NAME.fullmatch(pkg):
            raise ValueError(f'"{truncate_runes(pkg, 40)}" is not a valid Homebrew formula name')
        return ["brew", "upgrade", pkg]
    raise ValueError(
        f'unsupported package manager "{truncate_runes(manager, 20)}" (use npm, pnpm, yarn, go, '
        "pip or brew)"
    )


def check_update_target(current: str, target: str, allow_downgrade: bool) -> Version:
    """Validate an update target (CLI-070): strict semver (E_USAGE invalid_value
    otherwise) and not older than the installed version unless allow_downgrade
    (the --allow-downgrade flag). A development build (unparseable current
    version) accepts any valid target. Raises Error."""
    try:
        t = parse_version(target)
    except ValueError:
        e = usage(
            "invalid_value",
            "--target-version",
            "The target version is not a semantic version; nothing was installed.",
        )
        e.details["expected"] = "semantic version, e.g. 1.2.3"
        raise e from None
    try:
        c = parse_version(current)
    except ValueError:
        return t
    if compare_versions(t, c) < 0 and not allow_downgrade:
        e = usage(
            "invalid_value",
            "--target-version",
            "The target is older than the installed version; nothing was installed.",
        )
        e.details["expected"] = "a version newer than " + str(c) + ", or " + DOWNGRADE_FLAG
        e.details["next_action"] = (
            "Ask the user before downgrading; then add " + DOWNGRADE_FLAG + "."
        )
        raise e
    return t


def _ordered_cache(c: dict[str, str]) -> str:
    """update-check.json as Go's json.Marshal writes its cache struct."""
    from ._util import Ordered, go_json

    o = Ordered()
    for k in ("checked_at", "current_version", "latest_version", "severity", "command"):
        o[k] = c.get(k, "")
    if c.get("notified_at"):
        o["notified_at"] = c["notified_at"]
    return go_json(o, escape_html=True)


def notice_severity(severity: str, current: str, latest: Version) -> str:
    """An update notice's severity (CLI-071): the given one when valid (info
    otherwise), and always warning when the update crosses a major version."""
    if severity not in NOTICE_SEVERITY_VALUES:
        severity = "info"
    try:
        cur = parse_version(current)
    except ValueError:
        return severity
    if latest.major > cur.major:
        severity = "warning"
    return severity


class SelfUpdateMixin:
    def record_update_check(self: App, latest: str, severity: str) -> None:
        """Store the latest released version found by the tool's own update check
        (CLI-071). Commands then attach an update_available notice from this cache
        only (no network), at most once a day, while the installed version is older.
        severity is info or warning (security fix or major version). Raises Error."""
        from .journal import write_file_atomic

        try:
            lv = parse_version(latest)
        except ValueError:
            raise usage(
                "invalid_value", "latest", "The latest version is not a semantic version."
            ) from None
        severity = notice_severity(severity, self._version(), lv)
        d = self.ensure_state_dir()
        c = {
            "checked_at": time_string(self._now()),
            "current_version": self._version(),
            "latest_version": latest,
            "severity": severity,
            "command": self.tool_name() + " update",
        }
        try:
            data = (_ordered_cache(c) + "\n").encode("utf-8")
        except (TypeError, ValueError):
            raise internal_error("The update cache cannot be encoded.") from None
        try:
            write_file_atomic(d, "update-check.json", data)
        except OSError as exc:
            raise io_error("The update cache cannot be written.", f"Check {d}.", exc) from None

    def _attach_update_notice(self: App, st: RunState) -> None:
        """The cached update notice in meta.notices (CLI-047): local cache only, at
        most once a day, never when the installed version is current, and never on
        update itself (its data says what is available)."""
        from .journal import write_file_atomic

        if st.cmd is not None and st.cmd.builtin and st.cmd.path == "update":
            return
        try:
            d = self.state_dir_path()
        except Error:
            return
        path = os.path.join(d, "update-check.json")
        try:
            with open(path, "rb") as fh:
                b = fh.read(4097)
        except OSError:
            return
        if len(b) > 4096:
            return
        try:
            c = json.loads(b.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(c, dict):
            return
        c = {k: v for k, v in c.items() if isinstance(v, str)}
        try:
            cur = parse_version(self._version())
            latest = parse_version(c.get("latest_version", ""))
        except ValueError:
            return
        if compare_versions(latest, cur) <= 0:
            return
        now = self._now()
        t = parse_time(c.get("notified_at", ""))
        if t is not None and now - t < _dt.timedelta(hours=24) and now > t:
            return
        severity = notice_severity(c.get("severity", ""), self._version(), latest)
        command = self.tool_name() + " update"
        self.notice(
            Notice(
                type="update_available",
                severity=severity,
                message=f"{self.tool_name()} {latest} is available (installed {cur}); run "
                f"`{command}`.",
                current_version=str(cur),
                latest_version=str(latest),
                command=command,
                checked_at=c.get("checked_at", ""),
            )
        )
        c["notified_at"] = time_string(now)
        try:
            write_file_atomic(d, "update-check.json", (_ordered_cache(c) + "\n").encode("utf-8"))
        except OSError:
            pass
