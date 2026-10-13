# Vendored from ai-native-cli-spec template/python/aicli.
# Do not edit in tool repos; run scripts/sync-spec.js.
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sean Guo

"""The built-in update (CLI-070, CLI-071). A tool opts in with
App(update=UpdateConfig(repo=...)); aicli then registers the root command update:
discover the release on GitHub, then either let the package manager that owns the
install upgrade it (and check what got installed), or replace the binary after
verifying the Sigstore signature of checksums.txt (exact workflow identity) and the
archive's checksum, and finally sync the installed Skills from the new package.

Network use follows CLI-110: 30 s per request, proxies from HTTPS_PROXY,
HTTP_PROXY and NO_PROXY, TLS always verified (<PREFIX>_CA_FILE adds a PEM bundle to
the system roots), a User-Agent, GITHUB_TOKEN (else GH_TOKEN) sent only to the API
host, and size caps on everything read."""

from __future__ import annotations

import contextlib
import functools
import hashlib
import http.client
import json
import math
import os
import platform
import re
import secrets
import shutil
import signal
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any

from ._util import go_quote, truncate_runes
from .builtins import skill_versions
from .contract_gen import SPEC_VERSION, UPDATE_INSTALL_METHODS
from .errors import Error, internal_error, new_error, usage
from .journal import rename_retry
from .keystore import is_link_or_reparse
from .selfupdate import (
    Version,
    check_update_target,
    compare_versions,
    package_manager_args,
    parse_version,
)

if TYPE_CHECKING:
    from ._tree import Node
    from .app import App

REQUEST_TIMEOUT = 30.0  # every request (CLI-110)
REFRESH_TIMEOUT = 3.0  # doctor's refresh of the update notice
VERSION_TIMEOUT = 30.0  # `<binary> --version`
MANAGER_TIMEOUT = 600.0  # a package manager run
MAX_RELEASE_JSON = 4 << 20
MAX_SIGNED_FILE = 1 << 20  # checksums.txt and its Sigstore bundle, each
MAX_ARCHIVE = 512 << 20
MAX_EXTRACTED = 512 << 20  # any one extracted file
OIDC_ISSUER = "https://token.actions.githubusercontent.com"
CHECKSUMS = "checksums.txt"
BUNDLE = "checksums.txt.sigstore.json"

SIGNATURE_FAILED = (
    "The release signature did not verify, so the update was refused and the installed version "
    "is unchanged."
)
SIGNATURE_NEXT = "Do not retry; tell the user the release failed signature verification."
CHECKSUM_FAILED = (
    "The downloaded archive does not match its signed checksum, so it was not installed."
)
CHECKSUM_NEXT = "Do not retry; tell the user the release archive failed its checksum."


@dataclass
class UpdateConfig:
    """Opts a tool into the built-in update (App(update=UpdateConfig(...))).

    repo is the GitHub "owner/name" that publishes the releases; workflow the
    release workflow file that signs checksums.txt. The package names are those of
    the channels the tool is published on ("" when it is not): npm_package (also
    used for pnpm and yarn), go_package (`go install <go_package>@v<version>`),
    pypi_package and brew_formula. api_base is the GitHub API (tests point it at a
    fake server).

    Test seams (None means the production behaviour): verify(artifact, bundle,
    identity, issuer) raises when the signature does not verify (default
    verify_sigstore_bundle; raise TrustRootUnavailable when the trust root cannot be
    loaded); executable() is the path of the running tool; install_method(exe)
    names how it was installed (default detect_install_method); run(argv) runs a
    command without a shell and returns its exit code and combined output (raise
    FileNotFoundError when the program does not exist)."""

    repo: str
    workflow: str = "release.yml"
    npm_package: str = ""
    go_package: str = ""
    pypi_package: str = ""
    brew_formula: str = ""
    api_base: str = "https://api.github.com"
    verify: Callable[[bytes, bytes, str, str], None] | None = None
    executable: Callable[[], str] | None = None
    install_method: Callable[[str], str] | None = None
    run: Callable[[list[str]], tuple[int, bytes]] | None = None


class TrustRootUnavailable(Exception):
    """The Sigstore trust root could not be loaded (the TUF refresh failed), so the
    signature could not be checked. update reports it as E_NETWORK (retryable);
    any other failure of a verify function is a signature that did not verify
    (E_INTEGRITY)."""


class _NoVerifier(Exception):
    """The sigstore package is missing: this build cannot verify signatures."""


def verify_sigstore_bundle(artifact: bytes, bundle: bytes, identity: str, issuer: str) -> None:
    """Verify a Sigstore bundle over artifact (checksums.txt): the certificate must
    name exactly identity (the release workflow at the tag) and issuer, and the
    signature and transparency-log entry must check against Sigstore's production
    trust root. Raises TrustRootUnavailable when that trust root cannot be loaded and
    any other exception when the signature does not verify. sigstore-python is
    imported here, on first use; without it the update fails closed."""
    try:
        from sigstore.models import Bundle
        from sigstore.verify import Verifier
        from sigstore.verify.policy import Identity
    except ImportError as exc:
        raise _NoVerifier(str(exc)) from None
    try:
        verifier = Verifier.production()
    except Exception as exc:  # the TUF refresh of the trust root failed
        raise TrustRootUnavailable(str(exc)) from exc
    verifier.verify_artifact(
        input_=artifact,
        bundle=Bundle.from_json(bundle),
        policy=Identity(identity=identity, issuer=issuer),
    )


# ---------------------------------------------------------------- install method


def _segments(path: str) -> list[str]:
    return [s for s in re.split(r"[\\/]+", path) if s]


def install_method_for(path: str, frozen: bool) -> str:
    """The install method of a tool whose code lies at path (CLI-070, first match
    wins): "source" for a Python tool that is neither frozen nor installed in a
    site-packages or dist-packages directory; pnpm, yarn or npm under node_modules;
    brew in a Homebrew Cellar; pip for an installed Python package; else binary."""
    segs = _segments(path)
    slashed = path.replace("\\", "/")
    site = "site-packages" in segs or "dist-packages" in segs
    if not frozen and not site:
        return "source"
    if "node_modules" in segs:
        if ".pnpm" in segs:
            return "pnpm"
        if ".yarn" in segs or "/yarn/global/" in slashed:
            return "yarn"
        return "npm"
    if (
        "Cellar" in segs
        or slashed.startswith("/opt/homebrew/")
        or slashed.startswith("/home/linuxbrew/")
    ):
        return "brew"
    if not frozen and site:
        return "pip"
    return "binary"


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def detect_install_method(exe: str) -> str:
    """The default UpdateConfig.install_method. A frozen binary (sys.frozen) is
    judged by its own path; a Python tool by where its code lies, so a copy that
    runs from source (a checkout or an editable install) is "source" and cannot
    update itself."""
    if _frozen():
        return install_method_for(exe, True)
    return install_method_for(os.path.realpath(os.path.abspath(__file__)), False)


def default_executable() -> str:
    """The default UpdateConfig.executable: the frozen binary (sys.executable), or
    the script that started a Python tool, with symbolic links resolved."""
    if _frozen():
        return os.path.realpath(sys.executable)
    argv0 = sys.argv[0] if sys.argv and sys.argv[0] not in ("", "-c", "-m") else ""
    return os.path.realpath(argv0 or sys.executable)


def _self_version_argv() -> list[str]:
    """How to run this tool again with --version (the default executable)."""
    if _frozen():
        return [os.path.realpath(sys.executable), "--version"]
    main = sys.modules.get("__main__")
    spec = getattr(main, "__spec__", None)
    if spec is not None and spec.name:
        return [sys.executable, "-m", spec.name.removesuffix(".__main__"), "--version"]
    return _version_argv(default_executable())


def _version_argv(exe: str) -> list[str]:
    if exe.lower().endswith((".py", ".pyw")):
        return [sys.executable, exe, "--version"]
    return [exe, "--version"]


def release_platform() -> tuple[str, str]:
    """The release os and arch names of this machine (linux, darwin, windows;
    amd64, arm64)."""
    osname = {"linux": "linux", "darwin": "darwin", "win32": "windows"}.get(
        sys.platform, sys.platform
    )
    machine = platform.machine().lower()
    arch = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "x64": "amd64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(machine, machine or "unknown")
    return osname, arch


def asset_names(tool: str, version: str, osname: str, arch: str) -> list[str]:
    """The archive names a release may carry for this platform, preferred first:
    <tool>-<version>-<os>-<arch>.zip on Windows (arm64 falls back to amd64),
    .tar.gz elsewhere."""
    ext = "zip" if osname == "windows" else "tar.gz"
    out = [f"{tool}-{version}-{osname}-{arch}.{ext}"]
    if osname == "windows" and arch == "arm64":
        out.append(f"{tool}-{version}-windows-amd64.zip")
    return out


# ---------------------------------------------------------------- network


class _NetError(Exception):
    pass


class _HTTPStatus(_NetError):
    def __init__(self, status: int, headers: Any) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.headers = headers


class _Unreachable(_NetError):
    def __init__(self, host: str) -> None:
        super().__init__(host)
        self.host = host


class _TimedOut(_NetError):
    pass


class _TooLarge(_NetError):
    pass


class _BadAnswer(_NetError):
    pass


class _WriteFailed(Exception):
    pass


class _CAFileError(Exception):
    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.path = path


def _host(url: str) -> str:
    """host[:port] of a URL, without user info."""
    try:
        netloc = urllib.parse.urlsplit(url).netloc
    except ValueError:
        return ""
    return netloc.rsplit("@", 1)[-1].lower()


class _NoDowngradeRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects, but never from https to anything else."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        if req.full_url.lower().startswith("https:") and not newurl.lower().startswith("https:"):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _Net:
    """HTTP GET for the update (CLI-110)."""

    def __init__(self, app: App, cfg: UpdateConfig, timeout: float) -> None:
        self.app = app
        self.timeout = timeout
        self.api = (cfg.api_base or "https://api.github.com").rstrip("/")
        self.api_host = _host(self.api)
        self.user_agent = f"{app.tool_name()}/{app.version} aicli/{SPEC_VERSION}"
        self.token = (app._env("GITHUB_TOKEN") or app._env("GH_TOKEN")).strip()
        self._opener: urllib.request.OpenerDirector | None = None

    def opener(self) -> urllib.request.OpenerDirector:
        if self._opener is None:
            ctx = ssl.create_default_context()  # system roots, verification on
            ca = (self.app._env(self.app._prefix() + "_CA_FILE") or "").strip()
            if ca:
                try:
                    ctx.load_verify_locations(cafile=ca)
                except (OSError, ssl.SSLError, ValueError):
                    raise _CAFileError(ca) from None
            self._opener = urllib.request.build_opener(
                urllib.request.ProxyHandler(urllib.request.getproxies_environment()),
                urllib.request.HTTPSHandler(context=ctx),
                _NoDowngradeRedirect(),
            )
        return self._opener

    def _open(self, url: str) -> Any:
        host = _host(url)
        api = host == self.api_host
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/vnd.github+json" if api else "application/octet-stream",
            },
        )
        if api and self.token:
            # never forwarded on a redirect, and never sent to any other host
            req.add_unredirected_header("Authorization", "Bearer " + self.token)
        try:
            return self.opener().open(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            headers = exc.headers
            with contextlib.suppress(Exception):
                exc.close()
            raise _HTTPStatus(exc.code, headers) from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise _TimedOut() from None
            raise _Unreachable(host) from None
        except TimeoutError:
            raise _TimedOut() from None
        except (OSError, http.client.HTTPException, ValueError):
            raise _Unreachable(host) from None

    def _read(self, resp: Any, n: int, host: str) -> bytes:
        try:
            return resp.read(n)
        except TimeoutError:
            raise _TimedOut() from None
        except (OSError, http.client.HTTPException, ValueError):
            raise _Unreachable(host) from None

    def get(self, url: str, limit: int) -> bytes:
        """The body of url, at most limit bytes (_TooLarge beyond)."""
        resp = self._open(url)
        try:
            body = self._read(resp, limit + 1, _host(url))
        finally:
            with contextlib.suppress(Exception):
                resp.close()
        if len(body) > limit:
            raise _TooLarge()
        return body

    def get_json(self, url: str) -> Any:
        try:
            return json.loads(self.get(url, MAX_RELEASE_JSON).decode("utf-8"))
        except _TooLarge:
            raise _BadAnswer() from None
        except (UnicodeDecodeError, ValueError):
            raise _BadAnswer() from None

    def get_to(self, url: str, path: str, limit: int) -> str:
        """Stream url into path (at most limit bytes); the SHA-256 of the body."""
        resp = self._open(url)
        h = hashlib.sha256()
        n = 0
        host = _host(url)
        try:
            with contextlib.ExitStack() as stack:
                try:
                    fh = stack.enter_context(open(path, "wb"))
                except OSError:
                    raise _WriteFailed() from None
                while True:
                    chunk = self._read(resp, 1 << 20, host)
                    if not chunk:
                        break
                    n += len(chunk)
                    if n > limit:
                        raise _TooLarge()
                    h.update(chunk)
                    try:
                        fh.write(chunk)
                    except OSError:
                        raise _WriteFailed() from None
        finally:
            with contextlib.suppress(Exception):
                resp.close()
        return h.hexdigest()


def _retry_after_ms(headers: Any, now: float) -> int | None:
    """Retry-After (seconds or an HTTP date), else X-RateLimit-Reset (epoch
    seconds), as milliseconds from now."""
    if headers is None:
        return None
    ra = (headers.get("Retry-After") or "").strip()
    if ra:
        if ra.isdigit():
            return int(ra) * 1000
        try:
            when = parsedate_to_datetime(ra).timestamp()
        except (TypeError, ValueError, IndexError, OverflowError):
            when = None
        if when is not None:
            return max(0, int((when - now) * 1000))
    reset = (headers.get("X-RateLimit-Reset") or "").strip()
    if reset.isdigit():
        return max(0, int((int(reset) - now) * 1000))
    return None


# ---------------------------------------------------------------- releases


_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(.+?)[ \t]*$", re.MULTILINE)


def has_security_heading(body: Any) -> bool:
    """Whether release notes hold a Markdown heading whose text is Security (any
    level, any case)."""
    if not isinstance(body, str):
        return False
    return any(
        m.group(1).rstrip("#").strip().lower() == "security" for m in _HEADING.finditer(body)
    )


def update_severity(
    current: Version | None, target: Version, releases: list[tuple[Version, dict[str, Any]]]
) -> str:
    """The update notice's severity (CLI-071): warning when the major version
    changes or a release newer than the current version and not newer than the
    target has a Security heading in its notes, else info."""
    if current is not None and target.major != current.major:
        return "warning"
    for v, rel in releases:
        if current is not None and compare_versions(v, current) <= 0:
            continue
        if compare_versions(v, target) > 0:
            continue
        if has_security_heading(rel.get("body")):
            return "warning"
    return "info"


def _parse_or_none(s: str) -> Version | None:
    try:
        return parse_version(s)
    except ValueError:
        return None


def _same_version(reported: str | None, want: Version) -> bool:
    v = _parse_or_none(reported or "")
    return v is not None and compare_versions(v, want) == 0


def _assets(rel: dict[str, Any]) -> dict[str, str]:
    """Asset name -> download URL."""
    out: dict[str, str] = {}
    for a in rel.get("assets") or []:
        if not isinstance(a, dict):
            continue
        name, url = a.get("name"), a.get("browser_download_url")
        if isinstance(name, str) and isinstance(url, str) and url:
            out[name] = url
    return out


_CHECKSUM_LINE = re.compile(r"([0-9a-f]{64})  (.+)")


def checksum_for(checksums: bytes, asset: str) -> str | None:
    """The SHA-256 checksums.txt gives asset: exactly one line
    "<64 lowercase hex>  <asset>", else None."""
    try:
        text = checksums.decode("utf-8")
    except UnicodeDecodeError:
        return None
    found = []
    for line in text.split("\n"):
        m = _CHECKSUM_LINE.fullmatch(line.rstrip("\r"))
        if m and m.group(2) == asset:
            found.append(m.group(1))
    return found[0] if len(found) == 1 else None


def _reported_version(out: bytes) -> str | None:
    """data.version of the JSON envelope a `--version` run printed."""
    for line in out.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            env = json.loads(line)
        except ValueError:
            continue
        data = env.get("data") if isinstance(env, dict) else None
        v = data.get("version") if isinstance(data, dict) else None
        if isinstance(v, str) and v:
            return v
    return None


# ---------------------------------------------------------------- archives


class _UnsafeArchive(Exception):
    """An archive entry that may not be extracted: an absolute path, a .. segment,
    a link, a device file or a file over 512 MiB. name is the entry's name."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


class _BadArchive(Exception):
    """An archive that cannot be read."""


def _entry_path(name: str) -> str:
    """An archive entry's path relative to the archive root; _UnsafeArchive for an
    absolute path, a backslash or a .. segment."""
    if not name or name.startswith("/") or "\\" in name or re.match(r"[A-Za-z]:", name):
        raise _UnsafeArchive(name)
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise _UnsafeArchive(name)
    return "/".join(parts)


def _wanted(rel: str, binary: str) -> bool:
    return rel == binary or rel == "skills" or rel.startswith("skills/")


def _write_member(src: Any, name: str, dest: str, size: int) -> None:
    if size > MAX_EXTRACTED:
        raise _UnsafeArchive(name)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    n = 0
    with open(dest, "wb") as out:
        while True:
            chunk = src.read(1 << 20)
            if not chunk:
                break
            n += len(chunk)
            if n > MAX_EXTRACTED:
                raise _UnsafeArchive(name)
            out.write(chunk)


def extract_release(archive: str, dest: str, binary: str) -> None:
    """Extract binary and the skills/ tree from the archive root into dest. An
    absolute path, a .. segment, a link or a device file anywhere in the archive,
    or a file over 512 MiB, makes it unsafe (_UnsafeArchive); an archive that cannot
    be read is _BadArchive. Other files are ignored. OSError when dest cannot be
    written."""
    os.makedirs(dest, exist_ok=True)
    if archive.endswith(".zip"):
        try:
            zf = zipfile.ZipFile(archive)
        except (OSError, zipfile.BadZipFile, ValueError):
            raise _BadArchive(archive) from None
        with zf:
            infos = zf.infolist()
            for info in infos:
                kind = stat.S_IFMT((info.external_attr >> 16) & 0xFFFF)
                if kind and kind not in (stat.S_IFREG, stat.S_IFDIR):
                    raise _UnsafeArchive(info.filename)  # a link or a device
                _entry_path(info.filename)
            for info in infos:
                rel = _entry_path(info.filename)
                if not rel or info.is_dir() or not _wanted(rel, binary):
                    continue
                target = os.path.join(dest, *rel.split("/"))
                try:
                    with zf.open(info) as src:
                        _write_member(src, info.filename, target, info.file_size)
                except (zipfile.BadZipFile, ValueError, RuntimeError, EOFError):
                    raise _BadArchive(archive) from None
        return
    try:
        tf = tarfile.open(archive, "r:gz")
    except (OSError, tarfile.TarError, EOFError):
        raise _BadArchive(archive) from None
    with tf:
        try:
            members = tf.getmembers()
        except (OSError, tarfile.TarError, EOFError):
            raise _BadArchive(archive) from None
        for m in members:
            if not (m.isfile() or m.isdir()):
                raise _UnsafeArchive(m.name)  # a link or a device
            _entry_path(m.name)
        for m in members:
            rel = _entry_path(m.name)
            if not rel or m.isdir() or not _wanted(rel, binary):
                continue
            try:
                src = tf.extractfile(m)
                if src is None:
                    raise _UnsafeArchive(m.name)
                with src:
                    _write_member(src, m.name, os.path.join(dest, *rel.split("/")), m.size)
            except (tarfile.TarError, EOFError):
                raise _BadArchive(archive) from None


# ---------------------------------------------------------------- Skills


def _copy_tree(src: str, dst: str) -> None:
    """Copy a Skill directory (links are followed)."""
    shutil.copytree(src, dst)


def _skill_version(skill_dir: str) -> str:
    try:
        with open(os.path.join(skill_dir, "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read(256 << 10)
    except (OSError, UnicodeDecodeError):
        return ""
    return skill_versions(text)[0]


def _replace_dir(src: str, root: str, name: str) -> None:
    """Replace <root>/<name> with a copy of src: copy beside it, move the old one
    aside, move the copy in, remove the old one. Renames are retried briefly on
    Windows, where a scanner may still hold the files just written."""
    tag = secrets.token_hex(4)
    new = os.path.join(root, f".{name}.new-{tag}")
    old = os.path.join(root, f".{name}.old-{tag}")
    dst = os.path.join(root, name)
    try:
        _copy_tree(src, new)
        rename_retry(dst, old)
        try:
            rename_retry(new, dst)
        except OSError:
            rename_retry(old, dst)
            raise
    finally:
        if os.path.lexists(new):
            shutil.rmtree(new, ignore_errors=True)
    shutil.rmtree(old, ignore_errors=True)


def npm_package_root(exe: str, package: str) -> str | None:
    """The directory of the npm package named package that holds exe: walk up from
    exe to the first package.json whose name is package."""
    if not package:
        return None
    d = os.path.dirname(os.path.abspath(exe))
    while True:
        try:
            with open(os.path.join(d, "package.json"), "rb") as fh:
                obj = json.loads(fh.read(1 << 20).decode("utf-8"))
            if isinstance(obj, dict) and obj.get("name") == package:
                return d
        except (OSError, ValueError):
            pass
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


# ---------------------------------------------------------------- interrupts


class _Interrupts:
    """SIGINT and SIGTERM during an update: before the commit point they stop it
    (KeyboardInterrupt, reported as E_INTERRUPTED); from the commit point on they
    are held so the run finishes and reports what happened."""

    def __init__(self) -> None:
        self.committing = False
        self.pending = False
        self._old: dict[int, Any] = {}

    def __enter__(self) -> _Interrupts:
        if threading.current_thread() is not threading.main_thread():
            return self
        for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
            if sig is None:
                continue
            try:
                self._old[sig] = signal.signal(sig, self._handle)
            except (ValueError, OSError):
                continue
        return self

    def _handle(self, signum: int, frame: Any) -> None:
        if self.committing:
            self.pending = True
            return
        raise KeyboardInterrupt

    def __exit__(self, *exc: Any) -> None:
        for sig, old in self._old.items():
            with contextlib.suppress(ValueError, OSError, TypeError):
                signal.signal(sig, old)
        self._old.clear()


# ---------------------------------------------------------------- the run


def _default_run(argv: list[str], timeout: float) -> tuple[int, bytes]:
    """Run argv without a shell; the exit code and stdout with stderr. A program
    that does not exist raises FileNotFoundError; a run over timeout is killed and
    fails."""
    prog = argv[0]
    if not (os.path.isabs(prog) or os.sep in prog or (os.altsep and os.altsep in prog)):
        found = shutil.which(prog)
        if found is None:
            raise FileNotFoundError(prog)
        prog = found
    try:
        p = subprocess.run(
            [prog, *argv[1:]],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.output if isinstance(exc.output, bytes) else b""
        return -1, out
    return p.returncode, p.stdout or b""


def _reason(exc: OSError) -> str:
    """The OS error text, without the paths and the trailing period."""
    text = exc.strerror or type(exc).__name__
    return truncate_runes(text.strip().rstrip("."), 120)


# discover's result: the target, the latest release found by the list, the target's
# release object and the releases considered for the notice's severity
_Discovered = tuple[Version, Version | None, dict[str, Any], list[tuple[Version, dict[str, Any]]]]


class _Updater:
    """One run of the update built-in."""

    def __init__(self, app: App, node: Node, cfg: UpdateConfig) -> None:
        self.app = app
        self.node = node
        self.cfg = cfg
        self.tool = app.tool_name()
        self.current = app.version
        self.stage = "discover"
        self.method = ""
        self.exe = ""
        self.target: Version | None = None
        self.dispatched = False
        self.dispatch_unknown = False
        self.replaced = False
        self.signature = "not_checked"
        self.rollback = ""  # why moving the old binary back failed, for the swap error
        self.tmp = ""
        self.interrupts = _Interrupts()

    # ---------------------------------------------------------------- errors

    def begin(self, stage: str) -> None:
        self.stage = stage
        self.app.debug("update stage: " + stage)

    def failure_details(self, stage: str = "") -> dict[str, Any]:
        state = "dispatched" if self.dispatched else "not_started"
        if self.dispatch_unknown:
            state = "unknown"
        d: dict[str, Any] = {
            "dispatch_state": state,
            "stage": stage or self.stage,
            "current_version": self.current,
            "binary_replaced": False,
            "skill_sync_status": "skipped",
        }
        if self.target is not None:
            d["target_version"] = str(self.target)
        if self.method:
            d["install_method"] = self.method
        return d

    def fail(
        self,
        code: str,
        message: str,
        next_action: str,
        *,
        stage: str = "",
        extra: dict[str, Any] | None = None,
    ) -> Error:
        d = self.failure_details(stage)
        d["next_action"] = next_action
        d.update(extra or {})
        return new_error(code, message, d)

    def wrap(self, e: Error) -> Error:
        """An error from a helper with the update failure details added."""
        e = e.copy()
        for k, v in self.failure_details().items():
            e.details.setdefault(k, v)
        return e

    def again(self) -> str:
        return (
            f"Check the network or HTTPS_PROXY, then run `{self.tool} update` again; it is safe "
            "to repeat."
        )

    def net_error(self, exc: Exception, version: str = "") -> Error:
        """A network failure while checking for releases, or while downloading
        v<version>."""
        tool = self.tool
        if isinstance(exc, _CAFileError):
            prefix = self.app._prefix()
            return self.fail(
                "E_CONFIG",
                f"{prefix}_CA_FILE names {exc.path}, which is not a readable PEM certificate file.",
                f"Point {prefix}_CA_FILE at a PEM file of CA certificates, or unset it.",
            )
        if isinstance(exc, _HTTPStatus):
            remaining = ""
            if exc.headers is not None:
                remaining = (exc.headers.get("X-RateLimit-Remaining") or "").strip()
            if exc.status == 429 or (exc.status == 403 and remaining == "0"):
                ms = _retry_after_ms(exc.headers, self.app._now().timestamp())
                wait = 60 if ms is None else max(1, math.ceil(ms / 1000))
                e = self.fail(
                    "E_RATE_LIMITED",
                    "GitHub's API rate limit was hit.",
                    f"Wait {wait} s, or set GITHUB_TOKEN, then run `{tool} update` again.",
                )
                if ms is not None:
                    e.details["retry_after_ms"] = ms
                return e
            return self.fail(
                "E_NETWORK", f"The release source answered HTTP {exc.status}.", self.again()
            )
        if isinstance(exc, _TimedOut):
            msg = "The release check timed out."
            if version:
                msg = f"The download of v{version} timed out."
            return self.fail("E_TIMEOUT", msg, self.again())
        if isinstance(exc, _BadAnswer):
            return self.fail(
                "E_NETWORK", f"The release source sent an answer {tool} cannot read.", self.again()
            )
        host = exc.host if isinstance(exc, _Unreachable) else ""
        msg = f"Could not reach {host} to check for releases."
        if version:
            msg = f"Could not reach {host} to download v{version}."
        return self.fail("E_NETWORK", msg, self.again())

    def interrupted(self) -> Error:
        return self.fail(
            "E_INTERRUPTED",
            f"The update was interrupted before anything was installed; {self.tool} is still "
            f"{self.current}.",
            f"Run `{self.tool} update` again when ready.",
        )

    def partial(self, previous: str) -> Error:
        """E_PARTIAL: the new version is installed (or was, for an up-to-date run)
        but the Skill sync failed."""
        tool = self.tool
        target = str(self.target) if self.target is not None else self.current
        return new_error(
            "E_PARTIAL",
            f"{tool} was updated but its Skills were not synced, so the installed Skill still "
            "describes the old version.",
            {
                "dispatch_state": "dispatched",
                "next_action": f"Run `{tool} update` again to finish the Skill sync, then read "
                f"`{tool} changelog --since {previous}`.",
                "status": "partial",
                "stage": "skill_sync",
                "binary_replaced": True,
                "skill_sync_status": "failed",
                "skill_sync_command": f"{tool} update",
                "previous_version": previous,
                "target_version": target,
                "current_version": target,
                "update_available": False,
                "install_method": self.method,
                "signature_status": self.signature,
            },
        )

    def cannot_write(self, d: str) -> Error:
        return self.fail(
            "E_IO",
            f"Cannot write to {d}, where {self.tool} is installed.",
            f"Run the update from an account that can write there, or reinstall {self.tool} in "
            "a directory you own.",
            stage="replace",
        )

    def no_signed_checksums(self, version: str) -> Error:
        return self.fail(
            "E_INTEGRITY",
            f"Release v{version} has no signed checksums, so it was not installed.",
            SIGNATURE_NEXT,
            stage="verify_signature",
        )

    def checksum_failed(self) -> Error:
        return self.fail("E_INTEGRITY", CHECKSUM_FAILED, CHECKSUM_NEXT, stage="verify_checksum")

    # ---------------------------------------------------------------- the flow

    def run(self) -> dict[str, Any]:
        node = self.node
        tool = self.tool
        confirm = node.flag("confirm")
        if confirm is not None and confirm.changed:
            e = usage(
                "invalid_value",
                "--confirm",
                f"update takes no --confirm token; run `{tool} update`, or `{tool} update --check` "
                "to only look.",
            )
            e.details["next_action"] = (
                f"Run `{tool} update`, or `{tool} update --check` to only look."
            )
            raise self.wrap(e)
        dry_run = node.flag("dry-run")
        check = bool(node.flag("check").value) or bool(dry_run is not None and dry_run.value)
        tf = node.flag("target-version")
        target_text = tf.string() if tf is not None and tf.changed else None
        allow = bool(node.flag("allow-downgrade").value)
        with self.interrupts:
            try:
                return self.flow(check, target_text, allow)
            except KeyboardInterrupt:
                if self.replaced:
                    raise self.partial(self.current) from None
                raise self.interrupted() from None
            finally:
                if self.tmp:
                    shutil.rmtree(self.tmp, ignore_errors=True)

    def package_name(self) -> str:
        cfg = self.cfg
        return {
            "npm": cfg.npm_package,
            "pnpm": cfg.npm_package,
            "yarn": cfg.npm_package,
            "go": cfg.go_package,
            "pip": cfg.pypi_package,
            "brew": cfg.brew_formula,
        }.get(self.method, "")

    def manager_argv(self, target: Version) -> list[str] | None:
        try:
            return package_manager_args(self.method, self.package_name(), target)
        except ValueError:
            return None

    def flow(self, check: bool, target_text: str | None, allow: bool) -> dict[str, Any]:
        app, cfg, tool = self.app, self.cfg, self.tool
        self.begin("discover")
        # the order of the checks: the target, how the tool was installed, the HTTP
        # client (<PREFIX>_CA_FILE), then the clean-up and the network
        requested: Version | None = None
        if target_text is not None:
            try:
                requested = check_update_target(self.current, target_text, allow)
            except Error as e:
                raise self.wrap(e) from None
        try:
            self.exe = (cfg.executable or default_executable)()
        except OSError:
            self.exe = ""
        if not self.exe:
            raise self.fail(
                "E_IO",
                f"{tool} cannot find its own executable, so it cannot update itself.",
                f"Reinstall {tool} with the install command in its README.",
            )
        method = (cfg.install_method or detect_install_method)(self.exe)
        if method == "source":
            raise self.fail(
                "E_UNSUPPORTED",
                f"This copy of {tool} runs from source, so it cannot update itself.",
                "Update the checkout with git, or install a release build.",
            )
        if method not in UPDATE_INSTALL_METHODS:
            raise internal_error(
                f"UpdateConfig.install_method returned {go_quote(truncate_runes(str(method), 20))}"
                ", which is not an install method."
            )
        self.method = method
        net = _Net(app, cfg, REQUEST_TIMEOUT)
        try:
            net.opener()
        except _CAFileError as exc:
            raise self.net_error(exc) from None
        if not check and method == "binary":
            self.remove_stale(self.exe)
        try:
            target, latest, release, considered = self.discover(net, requested)
        except (_NetError, _CAFileError) as exc:
            raise self.net_error(exc) from None
        cur = _parse_or_none(self.current)
        if cur is None:
            up_to_date = False  # a development build takes any release
        else:
            c = compare_versions(target, cur)
            up_to_date = c <= 0 and not (allow and c < 0)
        self.target = target
        if latest is not None:
            with contextlib.suppress(Error, OSError):
                app.record_update_check(str(latest), update_severity(cur, latest, considered))
        if check:
            return self.check_data(None if up_to_date else target)
        if up_to_date:
            # nothing to install: sync the Skills from the installed package
            self.target = cur
            sync = self.sync_or_partial(self.current, self.current)
            data: dict[str, Any] = {
                "status": "up_to_date",
                "current_version": self.current,
                "target_version": self.current,
                "update_available": False,
                "install_method": method,
                "signature_status": "not_checked",
                "skill_sync_status": sync,
                "binary_replaced": False,
            }
        else:
            data = {
                "status": "updated",
                "current_version": str(target),
                "target_version": str(target),
                "previous_version": self.current,
                "update_available": False,
                "install_method": method,
                "binary_replaced": True,
            }
            if method == "binary":
                self.install_binary(net, release, target)
                data.update(
                    signature_status="verified", signature_verified=True, checksum_verified=True
                )
            else:
                argv = self.install_package(target)
                data.update(signature_status="not_checked", signature_verified=False, command=argv)
            sync = self.sync_or_partial(self.current, str(target))
            data["skill_sync_status"] = sync
        if sync in ("failed", "skipped"):
            data["skill_sync_command"] = f"{tool} update"
        return data

    def discover(self, net: _Net, requested: Version | None) -> _Discovered:
        """The release to install (CLI-070 discover): the --target-version release
        (a 404 is a usage error), else the newest release that is neither a draft
        nor a prerelease (tags that are not semantic versions are skipped; none
        left is E_UNSUPPORTED). Returns the target, the latest release the list
        found (None with --target-version), the target's release object and the
        releases that decide the notice's severity."""
        cfg, tool = self.cfg, self.tool
        repo = "/".join(urllib.parse.quote(p, safe="") for p in cfg.repo.split("/"))
        base = f"{net.api}/repos/{repo}/releases"
        if requested is not None:
            tag = urllib.parse.quote("v" + str(requested), safe="")
            try:
                rel = net.get_json(base + "/tags/" + tag)
            except _HTTPStatus as exc:
                if exc.status != 404:
                    raise
                e = usage(
                    "invalid_value",
                    "--target-version",
                    f"No release v{requested} exists in {cfg.repo}.",
                )
                e.details["next_action"] = (
                    f"Run `{tool} update --check` to see the latest version, or give an "
                    "existing version."
                )
                self.target = requested
                raise self.wrap(e) from None
            if not isinstance(rel, dict):
                raise _BadAnswer()
            return requested, None, rel, [(requested, rel)]
        rels = net.get_json(base + "?per_page=50")
        if not isinstance(rels, list):
            raise _BadAnswer()
        considered: list[tuple[Version, dict[str, Any]]] = []
        for rel in rels:
            if not isinstance(rel, dict) or rel.get("draft") or rel.get("prerelease"):
                continue
            tag = rel.get("tag_name")
            tag = tag if isinstance(tag, str) else ""
            try:
                considered.append((parse_version(tag), rel))
            except ValueError:
                self.app.debug(f"update: skipped release tag {tag} (not a semantic version)")
        if not considered:
            raise self.fail(
                "E_UNSUPPORTED",
                f"No release of {cfg.repo} has a semantic-version tag, so there is nothing to "
                "update to.",
                "Report this to the tool's maintainers; nothing was changed.",
            )
        key = functools.cmp_to_key(lambda a, b: compare_versions(a[0], b[0]))
        latest, release = max(considered, key=key)
        return latest, latest, release, considered

    def check_data(self, available: Version | None) -> dict[str, Any]:
        """update --check (or --dry-run): what an update would do; nothing else is
        touched."""
        data: dict[str, Any] = {
            "status": "available" if available is not None else "up_to_date",
            "current_version": self.current,
            "target_version": str(available) if available is not None else self.current,
            "update_available": available is not None,
            "install_method": self.method,
            "signature_status": "not_checked",
            "skill_sync_status": "skipped",
        }
        if available is not None and self.method != "binary":
            argv = self.manager_argv(available)
            if argv:
                data["command"] = argv
        return data

    # ---------------------------------------------------------------- package managers

    def run_cmd(self, argv: list[str], timeout: float) -> tuple[int, bytes]:
        if self.cfg.run is not None:
            return self.cfg.run(list(argv))
        return _default_run(argv, timeout)

    def observe_version(self, argv: list[str]) -> str | None:
        try:
            code, out = self.run_cmd(argv, VERSION_TIMEOUT)
        except OSError:
            return None
        if code != 0:
            return None
        return _reported_version(out)

    def install_package(self, target: Version) -> list[str]:
        """The package-manager path: run the manager, then check the version the
        installed tool reports."""
        tool, manager = self.tool, self.method
        self.begin("replace")
        unknown = self.fail(
            "E_CONFIG",
            f"{tool} does not know its {manager} package name, so it cannot update this install.",
            f"Reinstall {tool} with the install command in its README.",
        )
        pkg = self.package_name()
        if not pkg:
            raise unknown
        try:
            argv = package_manager_args(manager, pkg, target)
        except ValueError as exc:
            raise self.fail(
                "E_UNSUPPORTED",
                f"{manager} cannot install {tool} {target}: {exc}.",
                f"Install {tool} with the package-manager command in its README instead.",
            ) from None
        self.interrupts.committing = True
        try:
            code, _ = self.run_cmd(argv, MANAGER_TIMEOUT)
            self.dispatched = True
        except FileNotFoundError:
            self.interrupts.committing = False
            raise self.fail(
                "E_CONFIG",
                f"{manager} was not found on PATH.",
                f"Install {manager}, or reinstall {tool} with the install command in its README.",
            ) from None
        except OSError:
            code = -1
        cmd_argv = _version_argv(self.exe) if self.cfg.executable else _self_version_argv()
        observed = self.observe_version(cmd_argv)
        if not _same_version(observed, target):
            text = " ".join(argv)
            if code != 0:
                raise self.fail(
                    "E_IO",
                    f"`{text}` failed, so {tool} is still {self.current}.",
                    f"Run `{text}` yourself, then run `{tool} update` to sync the Skills.",
                    extra={"command": argv},
                )
            shown = truncate_runes(observed, 40) if observed else "an unknown version"
            raise self.fail(
                "E_IO",
                f"{manager} finished, but `{tool} --version` still reports {shown}.",
                f"Check which {tool} comes first on PATH, then run `{tool} update` again.",
            )
        self.replaced = True
        return argv

    # ---------------------------------------------------------------- binary

    def remove_stale(self, exe: str) -> None:
        """Remove <exe>.old and <exe>.old-<n> left by an earlier update on Windows
        (best effort)."""
        d, base = os.path.split(exe)
        try:
            names = os.listdir(d or ".")
        except OSError:
            return
        for name in names:
            if name == base + ".old" or name.startswith(base + ".old-"):
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(d, name))

    def install_binary(self, net: _Net, release: dict[str, Any], target: Version) -> None:
        """The binary path: download, verify the signature of checksums.txt and the
        archive's checksum, extract, check the new binary's version, swap it in
        and replace <exe dir>/skills."""
        tool, cfg = self.tool, self.cfg
        version = str(target)
        self.begin("download")
        assets = _assets(release)
        osname, arch = release_platform()
        archive_name = next(
            (n for n in asset_names(tool, version, osname, arch) if n in assets), ""
        )
        if not archive_name:
            raise self.fail(
                "E_UNSUPPORTED",
                f"Release v{version} has no build for {osname}/{arch}.",
                f"Install {tool} with the package-manager command in its README instead.",
            )
        if CHECKSUMS not in assets or BUNDLE not in assets:
            raise self.no_signed_checksums(version)
        exe_dir = os.path.dirname(os.path.abspath(self.exe))
        try:
            self.tmp = tempfile.mkdtemp(prefix=f".{tool}-update-", dir=exe_dir)
        except OSError:
            raise self.cannot_write(exe_dir) from None
        try:
            checksums = net.get(assets[CHECKSUMS], MAX_SIGNED_FILE)
            bundle = net.get(assets[BUNDLE], MAX_SIGNED_FILE)
        except _TooLarge:
            raise self.no_signed_checksums(version) from None
        except (_NetError, _CAFileError) as exc:
            raise self.net_error(exc, version) from None
        archive = os.path.join(self.tmp, archive_name)
        try:
            digest = net.get_to(assets[archive_name], archive, MAX_ARCHIVE)
        except _TooLarge:
            raise self.checksum_failed() from None
        except _WriteFailed:
            raise self.cannot_write(exe_dir) from None
        except (_NetError, _CAFileError) as exc:
            raise self.net_error(exc, version) from None

        self.begin("verify_signature")
        identity = (
            f"https://github.com/{cfg.repo}/.github/workflows/{cfg.workflow}@refs/tags/v{version}"
        )
        try:
            (cfg.verify or verify_sigstore_bundle)(checksums, bundle, identity, OIDC_ISSUER)
        except TrustRootUnavailable:
            raise self.fail(
                "E_NETWORK",
                "Could not load the Sigstore trust root, so the release signature could not be "
                "checked; nothing was installed.",
                f"Check the network, then run `{tool} update` again.",
            ) from None
        except _NoVerifier:
            raise self.fail(
                "E_INTEGRITY",
                f"This build of {tool} cannot verify release signatures, so it will not install a "
                "release.",
                f"Install {tool} with the package-manager command in its README.",
            ) from None
        except Exception:
            raise self.fail("E_INTEGRITY", SIGNATURE_FAILED, SIGNATURE_NEXT) from None
        self.signature = "verified"

        self.begin("verify_checksum")
        if checksum_for(checksums, archive_name) != digest:
            raise self.checksum_failed()

        self.begin("replace")
        binary_name = tool + (".exe" if os.name == "nt" else "")
        extracted = os.path.join(self.tmp, "extract")
        try:
            extract_release(archive, extracted, binary_name)
        except _UnsafeArchive as exc:
            raise self.fail(
                "E_INTEGRITY",
                f"The release archive holds an unsafe entry `{exc.name}`, so it was not installed.",
                "Do not retry; tell the user the release archive is unsafe.",
            ) from None
        except _BadArchive:
            raise self.checksum_failed() from None
        except OSError:
            raise self.cannot_write(exe_dir) from None
        new_binary = os.path.join(extracted, binary_name)
        if not os.path.isfile(new_binary):
            raise self.checksum_failed()  # the archive holds no binary
        if os.name != "nt":
            with contextlib.suppress(OSError):
                os.chmod(new_binary, 0o755)
        reported = self.observe_version([new_binary, "--version"])
        if reported is None:
            raise self.fail(
                "E_IO",
                "The downloaded binary does not run on this machine, so it was not installed.",
                f"Install {tool} with the package-manager command in its README instead.",
            )
        if not _same_version(reported, target):
            raise self.fail(
                "E_INTEGRITY",
                f"The downloaded binary reports version {truncate_runes(reported, 40)}, not "
                f"{version}, so it was not installed.",
                SIGNATURE_NEXT,
            )
        self.swap(new_binary)
        try:
            self.replace_skills(extracted)
        except OSError:
            raise self.partial(self.current) from None

    def swap(self, new: str) -> None:
        """The commit point: rename the new binary over the running one (Windows:
        move the running one aside first, and back if the second rename fails)."""
        exe = self.exe
        self.interrupts.committing = True
        self.rollback = ""
        try:
            if os.name == "nt":
                self._swap_windows(new, exe)
            else:
                rename_retry(new, exe)
        except OSError as exc:
            reason = _reason(exc) + self.rollback
            raise self.fail(
                "E_IO",
                f"Replacing {exe} failed: {reason}; {self.tool} is still {self.current}.",
                f"Close other running copies of {self.tool}, then run `{self.tool} update` again.",
            ) from None
        self.dispatched = True
        self.replaced = True

    def _swap_windows(self, new: str, exe: str) -> None:
        old = exe + ".old"
        try:
            os.remove(old)
        except FileNotFoundError:
            pass
        except OSError:
            n = 1
            while os.path.lexists(f"{exe}.old-{n}"):
                n += 1
            old = f"{exe}.old-{n}"
        rename_retry(exe, old)
        try:
            rename_retry(new, exe)
        except OSError:
            try:
                rename_retry(old, exe)
            except OSError as back:
                self.dispatch_unknown = True
                self.rollback = (
                    f" (moving the old binary back also failed: {_reason(back)}; it is at {old})"
                )
            raise

    def replace_skills(self, extracted: str) -> None:
        """<exe dir>/skills becomes the release's skills/ (the source of later
        syncs)."""
        exe_dir = os.path.dirname(os.path.abspath(self.exe))
        dst = os.path.join(exe_dir, "skills")
        src = os.path.join(extracted, "skills")
        aside = ""
        if os.path.lexists(dst):
            aside = os.path.join(exe_dir, f".skills.old-{secrets.token_hex(4)}")
            rename_retry(dst, aside)
        try:
            if os.path.isdir(src):
                rename_retry(src, dst)
        except OSError:
            if aside:
                with contextlib.suppress(OSError):
                    rename_retry(aside, dst)
            raise
        if aside:
            if os.path.islink(aside):
                with contextlib.suppress(OSError):
                    os.remove(aside)
            else:
                shutil.rmtree(aside, ignore_errors=True)

    # ---------------------------------------------------------------- Skills

    def skill_source(self) -> str | None:
        """Where the installed package keeps its Skills: <exe dir>/skills for a
        binary, skills/ in the npm wrapper package, none for the others."""
        if self.method == "binary":
            return os.path.join(os.path.dirname(os.path.abspath(self.exe)), "skills")
        if self.method in ("npm", "pnpm", "yarn"):
            root = npm_package_root(self.exe, self.cfg.npm_package)
            return os.path.join(root, "skills") if root else None
        return None

    def sync(self, version: str) -> str:
        """Copy each Skill of the installed package over the installed copies that
        are real directories with another metadata.version (links are left alone,
        missing copies are not created): synced, not_needed or skipped."""
        self.begin("skill_sync")
        source = self.skill_source()
        if not source or not os.path.isdir(source):
            return "skipped"
        want = _parse_or_none(version)
        names = sorted(
            n
            for n in os.listdir(source)
            if not n.startswith(".") and os.path.isdir(os.path.join(source, n))
        )
        installed = copied = False
        for name in names:
            for root in self.app._skill_dirs():
                dst = os.path.join(root, name)
                try:
                    st = os.lstat(dst)
                except FileNotFoundError:
                    continue
                installed = True
                if is_link_or_reparse(st) or not stat.S_ISDIR(st.st_mode):
                    continue
                have = _skill_version(dst)
                hv = _parse_or_none(have)
                if have == version or (
                    hv is not None and want is not None and compare_versions(hv, want) == 0
                ):
                    continue
                _replace_dir(os.path.join(source, name), root, name)
                copied = True
        if not installed:
            return "skipped"
        return "synced" if copied else "not_needed"

    def sync_or_partial(self, previous: str, version: str) -> str:
        """The Skill sync after an install, or of an up-to-date install: its
        failure is E_PARTIAL (the binary is the new one; the Skills are not)."""
        self.interrupts.committing = True
        try:
            return self.sync(version)
        except Exception:
            self.replaced = True
            raise self.partial(previous) from None


def run_update(app: App, node: Node) -> dict[str, Any]:
    """The update built-in's data, or raises its Error."""
    if app.update is None:
        raise internal_error("update runs only when App.update is set.")
    return _Updater(app, node, app.update).run()


def refresh_update_notice(app: App, timeout: float = REFRESH_TIMEOUT) -> None:
    """doctor's refresh (CLI-071): read the latest release and record it for the
    update notice; failures are ignored."""
    cfg = app.update
    if cfg is None:
        return
    try:
        net = _Net(app, cfg, timeout)
        repo = "/".join(urllib.parse.quote(p, safe="") for p in cfg.repo.split("/"))
        rel = net.get_json(f"{net.api}/repos/{repo}/releases/latest")
        if not isinstance(rel, dict):
            return
        tag = rel.get("tag_name")
        v = parse_version(tag if isinstance(tag, str) else "")
        severity = update_severity(_parse_or_none(app.version), v, [(v, rel)])
        app.record_update_check(str(v), severity)
    except Exception:  # best effort: doctor never fails on it
        return
