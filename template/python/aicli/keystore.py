# Vendored from ai-native-cli-spec template/python/aicli.
# Do not edit in tool repos; run scripts/sync-spec.js.
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sean Guo

"""The state store (SEC-030): <state>/confirm.key (32 random bytes), the operation
journal under <state>/operations, the default scope file and the update-notice
cache. A broken store never degrades to "no check": a missing, corrupt, linked or
unreadable key or directory means no token and no write (CLI-055)."""

from __future__ import annotations

import errno
import os
import stat
import tempfile
import time
from typing import TYPE_CHECKING

from .errors import Error, io_error

if TYPE_CHECKING:
    from .app import App

KEY_SIZE = 32
IS_WINDOWS = os.name == "nt"
_O_BINARY = getattr(os, "O_BINARY", 0)
_O_NOINHERIT = getattr(os, "O_NOINHERIT", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class KeyExistsError(Exception):
    """The confirm key was created by someone else first."""


def is_link_or_reparse(st: os.stat_result) -> bool:
    """A symlink, or (Windows) a junction or other reparse point."""
    if stat.S_ISLNK(st.st_mode):
        return True
    if IS_WINDOWS:
        if getattr(st, "st_file_attributes", 0) & _REPARSE:
            return True
        if getattr(st, "st_reparse_tag", 0):
            return True
    return False


def mkdir_all(path: str, mode: int = 0o700) -> None:
    """os.MkdirAll: create path and its missing parents, every new one with mode."""
    path = os.path.abspath(path)
    missing = []
    cur = path
    while not os.path.exists(cur):
        missing.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    for d in reversed(missing):
        try:
            os.mkdir(d, mode)
        except FileExistsError:
            pass


def sync_dir(d: str) -> None:
    """Flush a directory entry change to disk where the platform supports it."""
    if IS_WINDOWS:
        return
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def exclusive_create(path: str, data: bytes = b"", mode: int = 0o600) -> bool:
    """Create path with O_CREAT|O_EXCL and write data. False when it exists."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY | _O_NOINHERIT | _O_NOFOLLOW
    try:
        fd = os.open(path, flags, mode)
    except FileExistsError:
        return False
    try:
        if data:
            os.write(fd, data)
        try:
            os.fsync(fd)
        except OSError:
            pass
    finally:
        os.close(fd)
    return True


def check_state_dir_info(d: str, st: os.stat_result) -> Error | None:
    """Refuse linked, non-directory or (POSIX) shared state dirs."""
    if is_link_or_reparse(st):
        return io_error(
            "The state directory is a link or reparse point; the tool refuses writes.",
            f"Replace {d} with a real directory owned by you.",
        )
    if not stat.S_ISDIR(st.st_mode):
        return io_error(
            "The state directory path is not a directory; the tool refuses writes.",
            f"Ask the user to move {d} aside so the tool can create its state directory.",
        )
    if not IS_WINDOWS and stat.S_IMODE(st.st_mode) & 0o077:
        return io_error(
            "The state directory is accessible to other users; the tool refuses writes.",
            f"Run `chmod 700 {d}`.",
        )
    return None


def inside_profile(d: str) -> bool:
    """Whether d is inside the user's home directory (Windows relies on the
    profile ACL there)."""
    home = os.path.expanduser("~")
    if not home or home == "~":
        return False
    try:
        rel = os.path.relpath(
            os.path.normcase(os.path.abspath(d)), os.path.normcase(os.path.abspath(home))
        )
    except ValueError:  # another drive
        return False
    return rel != ".." and not rel.startswith(".." + os.sep) and not os.path.isabs(rel)


def protection(d: str | None) -> str:
    """How the state directory is protected (context.state.protection, decision
    30): POSIX 0700; on Windows the profile ACL inside the user profile, else
    unverified (no explicit ACL is set or verified by the library)."""
    if not IS_WINDOWS:
        return "posix-0700"
    if d and inside_profile(d):
        return "windows-profile-acl"
    return "windows-unverified"


def read_key(path: str) -> tuple[bytes | None, bool]:
    """The key file's bytes, validated. exists is False only when it is absent.
    Raises Error (E_IO) for a key that cannot be trusted."""

    def broken(why: str, exc: BaseException | None = None) -> Error:
        return io_error(
            "The confirm key " + why + "; the tool refuses writes until it is fixed.",
            f"Delete {path} so the tool creates a new key; tokens issued before become invalid.",
            exc,
        )

    attempt = 0
    while True:
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return None, False
        except OSError as exc:
            raise broken("cannot be read", exc) from None
        if is_link_or_reparse(st) or not stat.S_ISREG(st.st_mode):
            raise broken("is not a regular file")
        if not IS_WINDOWS and stat.S_IMODE(st.st_mode) & 0o077:
            raise io_error(
                "The confirm key is readable by other users; the tool refuses writes.",
                f"Run `chmod 600 {path}`, or delete it so the tool creates a new key.",
            )
        if st.st_size != KEY_SIZE:
            # A concurrent creator on a file system without hard links may still be
            # writing; give it a moment before failing closed.
            if st.st_size < KEY_SIZE and attempt < 3 and time.time() - st.st_mtime < 2:
                attempt += 1
                time.sleep(0.02)
                continue
            raise broken("has the wrong size")
        try:
            with open(path, "rb") as fh:
                b = fh.read(KEY_SIZE + 1)
        except OSError as exc:
            raise broken("cannot be read", exc) from None
        if len(b) != KEY_SIZE:
            raise broken("has the wrong size")
        return b, True


def create_key(d: str, path: str) -> bytes:
    """A new random key written to a temporary file and linked into place, so a
    reader never sees a partial key and two creators cannot both win."""
    key = os.urandom(KEY_SIZE)
    fd, tmp = tempfile.mkstemp(prefix=".confirm.key-", suffix=".tmp", dir=d)
    try:
        try:
            os.write(fd, key)
            os.fsync(fd)
        finally:
            os.close(fd)
        if not IS_WINDOWS:
            os.chmod(tmp, 0o600)
        try:
            os.link(tmp, path)
            sync_dir(d)
            return key
        except FileExistsError:
            raise KeyExistsError() from None
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                raise KeyExistsError() from None
        # Hard links unsupported here: fall back to an exclusive create.
        if not exclusive_create(path, key):
            raise KeyExistsError()
        sync_dir(d)
        return key
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


class KeystoreMixin:
    """App methods for the state directory and the confirm key."""

    def state_dir_path(self: App) -> str:
        """The state directory: App.state_dir, else <PREFIX>_STATE_DIR, else
        ~/.<name>. Creates nothing; raises E_IO when it cannot be resolved."""
        d = self.state_dir or self._env(self._prefix() + "_STATE_DIR")
        if not d:
            home = os.path.expanduser("~")
            if not home or home == "~":
                raise io_error(
                    "The home directory is unknown, so the state directory cannot be found; "
                    "nothing was sent.",
                    f"Set {self._prefix()}_STATE_DIR to a private directory.",
                )
            d = os.path.join(home, "." + self.tool_name())
        try:
            d = os.path.abspath(d)
        except (OSError, ValueError) as exc:
            raise io_error(
                "The state directory path is invalid; nothing was sent.", "", exc
            ) from None
        self._debug_once("state directory: " + d)
        return d

    def ensure_state_dir(self: App) -> str:
        """The state directory, created (0700) when missing; a link, a
        non-directory or (POSIX) a directory open to other users is E_IO."""
        d = self.state_dir_path()
        try:
            st = os.lstat(d)
        except FileNotFoundError:
            try:
                mkdir_all(d, 0o700)
            except OSError as exc:
                raise io_error(
                    "The state directory cannot be created; nothing was sent.",
                    f"Check that {d} can be created, or set {self._prefix()}_STATE_DIR.",
                    exc,
                ) from None
            try:
                st = os.lstat(d)
            except OSError as exc:
                raise io_error(
                    "The state directory cannot be read; nothing was sent.", f"Check {d}.", exc
                ) from None
        except OSError as exc:
            raise io_error(
                "The state directory cannot be read; nothing was sent.", f"Check {d}.", exc
            ) from None
        e = check_state_dir_info(d, st)
        if e is not None:
            raise e
        return d

    def inspect_state_dir(self: App) -> tuple[str, bool, str]:
        """The state directory without creating it (context, doctor): dir,
        available and the problem when it is not."""
        try:
            d = self.state_dir_path()
        except Error as e:
            return "", False, e.message
        try:
            st = os.lstat(d)
        except FileNotFoundError:
            return d, True, ""
        except OSError as exc:
            return d, False, str(exc)
        e = check_state_dir_info(d, st)
        if e is not None:
            return d, False, e.message
        try:
            read_key(os.path.join(d, "confirm.key"))
        except Error as ke:
            return d, False, ke.message
        ops = os.path.join(d, "operations")
        try:
            ost = os.lstat(ops)
            if is_link_or_reparse(ost) or not stat.S_ISDIR(ost.st_mode):
                return d, False, "the operations journal path is not a directory"
        except FileNotFoundError:
            pass
        except OSError as exc:
            return d, False, str(exc)
        return d, True, ""

    def load_key(self: App, state_dir: str, create: bool) -> bytes | None:
        """The 32-byte confirm key from <state>/confirm.key. With create, a new key
        is made when none exists (exclusive, 0600); without, a missing key is
        None. A key that exists but is not exactly 32 readable bytes in a regular
        file is E_IO: the tool never falls back to an unkeyed digest."""
        path = os.path.join(state_dir, "confirm.key")
        for _ in range(5):
            key, exists = read_key(path)
            if exists:
                return key
            if not create:
                return None
            try:
                return create_key(state_dir, path)
            except KeyExistsError:
                continue
            except OSError as exc:
                raise io_error(
                    "The confirm key cannot be created; no token was issued and nothing was sent.",
                    f"Check that {state_dir} is writable.",
                    exc,
                ) from None
        raise io_error(
            "The confirm key could not be read after it was created; nothing was sent.",
            "Run the command again.",
        )
