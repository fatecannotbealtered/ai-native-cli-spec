# Vendored from ai-native-cli-spec template/python/aicli.
# Do not edit in tool repos; run scripts/sync-spec.js.
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Sean Guo

"""The built-in update (CLI-070, CLI-071). A tool opts in with
App(update=UpdateConfig(repo=...)); aicli then registers the root command update:
discover the release on GitHub, then either let the package manager that owns the
install upgrade it (and check what got installed), or replace the binary after
verifying the Sigstore signature of checksums.txt (exact workflow identity) and the
archive's checksum, and finally sync the installed Skills from the new package.

Network use follows CLI-110: 30 s per request, proxies from HTTPS_PROXY,
HTTP_PROXY and NO_PROXY in the App's environment (App.env), TLS always verified
(<PREFIX>_CA_FILE adds a PEM bundle to the system roots), a User-Agent, GITHUB_TOKEN
(else GH_TOKEN) sent only to the API host, and size caps on everything read."""

from __future__ import annotations

import base64
import contextlib
import functools
import hashlib
import http.client
import ipaddress
import json
import math
import os
import platform
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
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
from .contract_gen import UPDATE_INSTALL_METHODS
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

REQUEST_TIMEOUT = 30.0  # every request (CLI-110): the longest wait without data
DOWNLOAD_DEADLINE = 600.0  # the longest any one download may take in all (E13)
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
_IS_WINDOWS = os.name == "nt"  # the swap's Windows algorithm (tests set it)

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
        # The trust root refresh cannot use the update's network settings (E12):
        # sigstore-python (4.x) builds its TUF updater inside Verifier.production()
        # and offers no way to pass a session or fetcher, so python-tuf's default
        # urllib3 fetcher runs. It reads HTTPS_PROXY, HTTP_PROXY and NO_PROXY and
        # waits 30 s per request like the update, but it does not add
        # <PREFIX>_CA_FILE to the system roots and sends its own User-Agent. A tool
        # behind a TLS-inspecting proxy gets E_NETWORK at verify_signature.
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


def _pnpm_store(segs: list[str]) -> bool:
    """A store/v<n>/links run of segments: pnpm 11's global store."""
    return any(
        segs[i] == "store" and re.fullmatch(r"v[0-9]+", segs[i + 1]) and segs[i + 2] == "links"
        for i in range(len(segs) - 2)
    )


def install_method_for(path: str, frozen: bool) -> str:
    """The install method of a tool whose code lies at path (CLI-070, first match
    wins): "source" for a Python tool that is neither frozen nor installed in a
    site-packages or dist-packages directory; pnpm in pnpm 11's global store
    (store/v<n>/links); pnpm, yarn or npm under node_modules; brew in a Homebrew
    Cellar; pip for an installed Python package; else binary."""
    segs = _segments(path)
    slashed = path.replace("\\", "/")
    site = "site-packages" in segs or "dist-packages" in segs
    if not frozen and not site:
        return "source"
    if _pnpm_store(segs):
        return "pnpm"
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


class _InsecureRedirect(_NetError):
    """A redirect to a URL that is not https (E13)."""


class _BadAnswer(_NetError):
    pass


class _WriteFailed(Exception):
    pass


def _host(url: str) -> str:
    """host[:port] of a URL, without user info."""
    try:
        netloc = urllib.parse.urlsplit(url).netloc
    except ValueError:
        return ""
    return netloc.rsplit("@", 1)[-1].lower()


def https_url(raw: str) -> bool:
    """An https URL with a host (E13: release assets and redirects)."""
    try:
        p = urllib.parse.urlsplit(raw)
    except ValueError:
        return False
    return p.scheme.lower() == "https" and bool(p.hostname)


class _HttpsRedirects(urllib.request.HTTPRedirectHandler):
    """Redirects stay on https (E13); the GitHub token still goes to the API host
    only (it is an unredirected header)."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        target = urllib.parse.urljoin(req.full_url, newurl)
        if urllib.parse.urlsplit(target).scheme.lower() != "https":
            with contextlib.suppress(Exception):
                fp.close()
            raise _InsecureRedirect()
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def app_proxies(app: App) -> tuple[dict[str, str], str]:
    """The proxies of the App's environment (CLI-110, Q2), read through App.env and
    never the process environment, by Go's httpproxy rules: HTTPS_PROXY for https
    requests, HTTP_PROXY for http ones (ignored under CGI, REQUEST_METHOD set), and
    NO_PROXY, the hosts that go direct; each upper-case name before its lower-case
    form."""

    def env_any(*names: str) -> str:
        for name in names:
            v = app._env(name) or ""
            if v:
                return v
        return ""

    proxies: dict[str, str] = {}
    v = env_any("HTTPS_PROXY", "https_proxy")
    if v:
        proxies["https"] = v
    v = env_any("HTTP_PROXY", "http_proxy")
    if v and not app._env("REQUEST_METHOD"):
        proxies["http"] = v
    return proxies, env_any("NO_PROXY", "no_proxy")


def _loopback(host: str) -> bool:
    """localhost or a loopback address, which never goes through a proxy."""
    name = host.strip().lower()
    if name.startswith("["):
        name = name[1 : name.find("]")] if "]" in name else name[1:]
    elif name.count(":") == 1:
        name = name.split(":", 1)[0]
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


class _AppProxyHandler(urllib.request.ProxyHandler):
    """urllib's ProxyHandler over the App's proxies (Q2): its own NO_PROXY decides
    which hosts go direct, not the process environment or the Windows registry,
    and localhost never uses a proxy."""

    def __init__(self, proxies: dict[str, str], no_proxy: str) -> None:
        super().__init__(proxies)
        self.no_proxy = no_proxy

    def proxy_open(self, req: Any, proxy: str, type: str) -> Any:  # noqa: A002 - urllib's name
        orig_type = req.type
        proxy_type, user, password, hostport = urllib.request._parse_proxy(proxy)  # type: ignore[attr-defined]
        if proxy_type is None:
            proxy_type = orig_type
        if req.host and (
            _loopback(req.host)
            or (
                self.no_proxy
                and urllib.request.proxy_bypass_environment(req.host, {"no": self.no_proxy})
            )
        ):
            return None
        if user and password:
            pair = f"{urllib.parse.unquote(user)}:{urllib.parse.unquote(password)}"
            creds = base64.b64encode(pair.encode()).decode("ascii")
            req.add_header("Proxy-authorization", "Basic " + creds)
        req.set_proxy(urllib.parse.unquote(hostport), proxy_type)
        if orig_type == proxy_type or orig_type == "https":
            return None  # the other handlers take it from here
        return self.parent.open(req, timeout=req.timeout)


class _Net:
    """HTTP GET for the update (CLI-110)."""

    def __init__(self, app: App, cfg: UpdateConfig, timeout: float) -> None:
        self.app = app
        self.timeout = timeout
        self.api = (cfg.api_base or "https://api.github.com").rstrip("/")
        self.api_host = _host(self.api)
        self.user_agent = app.user_agent()
        self.token = (app._env("GITHUB_TOKEN") or app._env("GH_TOKEN")).strip()
        self._opener: urllib.request.OpenerDirector | None = None

    def opener(self) -> urllib.request.OpenerDirector:
        """The opener on App.ssl_context (CLI-110, P10): the proxy from the App's
        environment (Q2), the system roots plus <PREFIX>_CA_FILE, https-only
        redirects. An unusable CA file raises its E_CONFIG Error."""
        if self._opener is None:
            proxies, no_proxy = app_proxies(self.app)
            self._opener = urllib.request.build_opener(
                _AppProxyHandler(proxies, no_proxy),
                urllib.request.HTTPSHandler(context=self.app.ssl_context()),
                _HttpsRedirects(),
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

    def _read(self, resp: Any, n: int, host: str, deadline: float) -> bytes:
        """Up to n bytes: what one read of the connection returns (read1), so a
        server that trickles data cannot outlast the deadline (E13)."""
        if time.monotonic() > deadline:
            raise _TimedOut()
        try:
            read1 = getattr(resp, "read1", None)
            return read1(n) if read1 is not None else resp.read(n)
        except TimeoutError:
            raise _TimedOut() from None
        except (OSError, http.client.HTTPException, ValueError):
            raise _Unreachable(host) from None

    def get(self, url: str, limit: int) -> bytes:
        """The body of url, at most limit bytes (_TooLarge beyond), within the
        download deadline."""
        deadline = time.monotonic() + DOWNLOAD_DEADLINE
        resp = self._open(url)
        host = _host(url)
        body = bytearray()
        try:
            while True:
                chunk = self._read(resp, 1 << 16, host, deadline)
                if not chunk:
                    break
                body += chunk
                if len(body) > limit:
                    raise _TooLarge()
        finally:
            with contextlib.suppress(Exception):
                resp.close()
        return bytes(body)

    def get_json(self, url: str) -> Any:
        try:
            return json.loads(self.get(url, MAX_RELEASE_JSON).decode("utf-8"))
        except _TooLarge:
            raise _BadAnswer() from None
        except (UnicodeDecodeError, ValueError):
            raise _BadAnswer() from None

    def get_to(self, url: str, path: str, limit: int) -> str:
        """Stream url into path (at most limit bytes, within the download deadline
        of 10 minutes besides the idle timeout); the SHA-256 of the body."""
        deadline = time.monotonic() + DOWNLOAD_DEADLINE
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
                    chunk = self._read(resp, 1 << 20, host, deadline)
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
    """data.version of the JSON envelope a `--version` run printed, else a
    plain-text version (a 1.x tool prints "gitlab-cli version 1.2.17"): the last
    word of the first line whose last word is a semantic version."""
    lines = out.decode("utf-8", "replace").splitlines()
    for line in lines:
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
    for line in lines:
        words = line.split()
        version = _parse_or_none(words[-1]) if words else None
        if version is not None:
            return str(version)
    return None


# ---------------------------------------------------------------- archives


class _UnsafeArchive(Exception):
    """An archive entry that may not be extracted: an absolute path, a .. segment,
    a drive or stream name (":"), a control character, a link, a device file, a
    repeated name, a file over 512 MiB, or an archive over 10,000 entries or 1 GiB
    in all. name is the entry's name."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


class _BadArchive(Exception):
    """An archive that cannot be read."""


MAX_ENTRIES = 10_000  # entries in a release archive
MAX_TOTAL = 1 << 30  # bytes extracted from one archive


def _entry_path(name: str) -> str:
    """An archive entry's path relative to the archive root; _UnsafeArchive for an
    absolute path, a backslash, any ":" (a drive or a stream), a control character
    or a .. segment."""
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or ":" in name
        or any(ord(c) < 0x20 or ord(c) == 0x7F for c in name)
    ):
        raise _UnsafeArchive(name)
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise _UnsafeArchive(name)
    return "/".join(parts)


def _wanted(rel: str, binary: str) -> bool:
    return rel == binary or rel == "skills" or rel.startswith("skills/")


def _check_entries(entries: list[tuple[str, bool]]) -> None:
    """The archive-wide checks (E13): at most 10,000 entries, every name safe,
    and no file name twice (after "." and empty segments are dropped)."""
    if len(entries) > MAX_ENTRIES:
        raise _UnsafeArchive(entries[MAX_ENTRIES][0])
    seen: set[str] = set()
    for name, is_dir in entries:
        rel = _entry_path(name)
        if is_dir:
            continue
        if rel in seen:
            raise _UnsafeArchive(name)
        seen.add(rel)


class _Budget:
    """The bytes an extraction may still write (1 GiB in all)."""

    def __init__(self) -> None:
        self.left = MAX_TOTAL


def _write_member(src: Any, name: str, dest: str, size: int, budget: _Budget) -> None:
    if size > MAX_EXTRACTED or size > budget.left:
        raise _UnsafeArchive(name)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    n = 0
    with open(dest, "xb") as out:
        while True:
            chunk = src.read(1 << 20)
            if not chunk:
                break
            n += len(chunk)
            if n > MAX_EXTRACTED or len(chunk) > budget.left:
                raise _UnsafeArchive(name)
            budget.left -= len(chunk)
            out.write(chunk)


def extract_release(archive: str, dest: str, binary: str) -> None:
    """Extract binary and the skills/ tree from the archive root into dest. An
    unsafe entry anywhere in the archive (an absolute path, a .. segment, a ":", a
    link, a device file, a repeated name, a file over 512 MiB) or an archive over
    10,000 entries or 1 GiB extracted makes it unsafe (_UnsafeArchive); an archive
    that cannot be read is _BadArchive. Other files are ignored. OSError when dest
    cannot be written."""
    os.makedirs(dest, exist_ok=True)
    budget = _Budget()
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
            _check_entries([(i.filename, i.is_dir()) for i in infos])
            for info in infos:
                rel = _entry_path(info.filename)
                if not rel or info.is_dir() or not _wanted(rel, binary):
                    continue
                target = os.path.join(dest, *rel.split("/"))
                try:
                    with zf.open(info) as src:
                        _write_member(src, info.filename, target, info.file_size, budget)
                except FileExistsError:
                    raise _UnsafeArchive(info.filename) from None
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
        _check_entries([(m.name, m.isdir()) for m in members])
        for m in members:
            rel = _entry_path(m.name)
            if not rel or m.isdir() or not _wanted(rel, binary):
                continue
            try:
                src = tf.extractfile(m)
                if src is None:
                    raise _UnsafeArchive(m.name)
                with src:
                    target = os.path.join(dest, *rel.split("/"))
                    _write_member(src, m.name, target, m.size, budget)
            except FileExistsError:
                raise _UnsafeArchive(m.name) from None
            except (tarfile.TarError, EOFError):
                raise _BadArchive(archive) from None


# ---------------------------------------------------------------- Skills


def _copy_tree(src: str, dst: str) -> None:
    """Copy a Skill directory without following links (E3): directories and
    regular files are copied; symbolic links, junctions and special files are
    left out."""
    os.makedirs(dst)
    with os.scandir(src) as it:
        entries = sorted(it, key=lambda e: e.name)
    for e in entries:
        st = os.lstat(e.path)
        if is_link_or_reparse(st):
            continue
        target = os.path.join(dst, e.name)
        if stat.S_ISDIR(st.st_mode):
            _copy_tree(e.path, target)
        elif stat.S_ISREG(st.st_mode):
            shutil.copyfile(e.path, target, follow_symlinks=False)
            with contextlib.suppress(OSError):
                shutil.copymode(e.path, target, follow_symlinks=False)


def _skill_version(skill_dir: str) -> str:
    try:
        with open(os.path.join(skill_dir, "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read(256 << 10)
    except (OSError, UnicodeDecodeError):
        return ""
    return skill_versions(text)[0]


def _replace_dir(src: str, root: str, name: str) -> None:
    """Replace <root>/<name> with a copy of src (links not followed): copy beside
    it, move the old one aside, move the copy in, remove the old one; a missing
    <root>/<name> is simply created. Renames are retried briefly on Windows, where
    a scanner may still hold the files just written."""
    tag = secrets.token_hex(4)
    new = os.path.join(root, f".{name}.new-{tag}")
    old = os.path.join(root, f".{name}.old-{tag}")
    dst = os.path.join(root, name)
    try:
        _copy_tree(src, new)
        if not os.path.lexists(dst):
            rename_retry(new, dst)
            return
        rename_retry(dst, old)
        try:
            rename_retry(new, dst)
        except OSError:
            rename_retry(old, dst)
            raise
    finally:
        if os.path.lexists(new):
            shutil.rmtree(new, ignore_errors=True)
    if os.path.islink(old):
        with contextlib.suppress(OSError):
            os.remove(old)
    else:
        shutil.rmtree(old, ignore_errors=True)


def skill_names(skills_dir: str) -> list[str]:
    """The Skills in a skills/ directory: real directories (not links) holding a
    SKILL.md, by name."""
    try:
        entries = list(os.scandir(skills_dir))
    except OSError:
        return []
    out = []
    for e in entries:
        if e.name.startswith("."):
            continue
        try:
            st = os.lstat(e.path)
            md = os.lstat(os.path.join(e.path, "SKILL.md"))
        except OSError:
            continue
        if is_link_or_reparse(st) or not stat.S_ISDIR(st.st_mode):
            continue
        if is_link_or_reparse(md) or not stat.S_ISREG(md.st_mode):
            continue
        out.append(e.name)
    return sorted(out)


def _dir_names(d: str) -> list[str]:
    """The directories (not links) directly in d, by name."""
    try:
        entries = list(os.scandir(d))
    except OSError:
        return []
    out = []
    for e in entries:
        try:
            st = os.lstat(e.path)
        except OSError:
            continue
        if not is_link_or_reparse(st) and stat.S_ISDIR(st.st_mode):
            out.append(e.name)
    return sorted(out)


_SKILL_FRONTMATTER = re.compile(r"^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)")
_SKILL_REQUIRES = re.compile(r"^[ \t]+requires\.skills:[ \t]*\"?([^\"\s#]+)\"?", re.MULTILINE)


def _same_version_text(a: str, b: str) -> bool:
    """Two equal semantic versions."""
    va, vb = _parse_or_none(a), _parse_or_none(b)
    return va is not None and vb is not None and compare_versions(va, vb) == 0


def npm_package_root(exe: str, package: str) -> str | None:
    """The directory of the npm package named package that holds exe: walk up from
    exe to the first package.json whose name is package."""
    if not package or not exe:
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


def find_program(name: str, path: str | None = None) -> str | None:
    """A program found on PATH the safe way (CLI-070 item 2, E1): only absolute
    PATH entries are searched, so an empty, "." or relative entry is skipped and
    the current directory is never searched implicitly (shutil.which searches it
    first on Windows, where a cloned repository's npm.bat would run). On Windows the
    name is tried with each PATHEXT extension (as is too when it has one). path is
    the PATH value (default the process environment's)."""
    from .scope import _is_abs

    if not name:
        return None
    if path is None:
        path = os.environ.get("PATH", "")
    windows = os.name == "nt"
    candidates = [name]
    if windows:
        exts = [e for e in (os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD").split(";") if e]
        candidates = ([name] if os.path.splitext(name)[1] else []) + [name + e for e in exts]
    for d in path.split(os.pathsep):
        d = d.strip().strip('"') if windows else d
        if not d or not _is_abs(d):
            continue
        for c in candidates:
            p = os.path.join(d, c)
            if os.path.isfile(p) and (windows or os.access(p, os.X_OK)):
                return p
    return None


def _default_run(argv: list[str], timeout: float, path: str | None = None) -> tuple[int, bytes]:
    """Run argv without a shell; the exit code and stdout with stderr. A bare
    program name is looked up with find_program (absolute PATH entries only); a
    program that does not exist raises FileNotFoundError; a run over timeout is
    killed and fails."""
    prog = argv[0]
    if not (os.path.isabs(prog) or os.sep in prog or (os.altsep and os.altsep in prog)):
        found = find_program(prog, path)
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
        self.current = app._version()
        self.stage = "discover"
        self.method = ""
        self.exe = ""
        self.target: Version | None = None
        self.dispatched = False
        self.dispatch_unknown = False
        self.replaced = False
        self.signature = "not_checked"
        self.old_binary = ""  # where a failed swap left the running binary
        self.tmp = ""
        self.interrupts = _Interrupts()
        # the Skills the verified release ships (binary path); None when no
        # release was installed in this run
        self.release_skills: list[str] | None = None
        self.path = app._env("PATH")  # where programs are looked up (absolute entries)

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
        if isinstance(exc, _InsecureRedirect):
            # usually a network's sign-in page; nothing was fetched over plain http
            return self.fail(
                "E_NETWORK",
                "The release source redirected to a URL that is not https, so nothing was "
                "downloaded.",
                "Check the network or HTTPS_PROXY (a network's sign-in page redirects like this), "
                f"then run `{tool} update` again.",
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
                # A swap that could not be undone keeps the new binary where it is:
                # the tool may be missing, and the user may need it (E10).
                if self.tmp and not self.dispatch_unknown:
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
            return self.install_argv(package_manager_args(self.method, self.package_name(), target))
        except ValueError:
            return None

    def install_argv(self, argv: list[str]) -> list[str]:
        """The argv to run: pip is the running interpreter's `python -m pip`, so the
        upgrade lands in the environment this tool runs from (E7)."""
        if self.method == "pip" and argv and argv[0] == "pip":
            return [sys.executable, "-m", *argv]
        return argv

    def flow(self, check: bool, target_text: str | None, allow: bool) -> dict[str, Any]:
        app, cfg, tool = self.app, self.cfg, self.tool
        self.begin("discover")
        # A development build (App.version not a semantic version, P14) has no
        # release to compare with or to replace: nothing is contacted or downloaded.
        if app._dev_build():
            raise self.fail(
                "E_UNSUPPORTED",
                f"{tool} {self.current} is a development build; install a release to update.",
                f"Install {tool} with the install command in its README.",
            )
        # the order of the checks: the target, how the tool was installed, the HTTP
        # client (<PREFIX>_CA_FILE), then the clean-up and the network
        requested: Version | None = None
        if target_text is not None:
            try:
                requested = check_update_target(self.current, target_text, allow)
            except Error as e:
                raise self.wrap(e) from None
        # --check never needs the executable; the binary path asks for it at download
        try:
            self.exe = (cfg.executable or default_executable)() or ""
        except OSError:
            self.exe = ""
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
        except Error as e:
            raise self.wrap(e) from None
        if not check and method == "binary":
            self.remove_stale(self.exe)
        try:
            target, latest, release, considered = self.discover(net, requested)
        except _NetError as exc:
            raise self.net_error(exc) from None
        cur = _parse_or_none(self.current)
        if cur is None:
            up_to_date = False  # a development build takes any release
        else:
            # a downgrade needs both --target-version and --allow-downgrade (E5)
            c = compare_versions(target, cur)
            up_to_date = c <= 0 and not (allow and requested is not None and c < 0)
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
        # Only a sync that could not finish names the command that finishes it; a
        # Skill that is not installed is not a reason, since update never installs
        # one (CLI-070 item 6, F10).
        if sync == "failed":
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
                v = parse_version(tag)
            except ValueError:
                self.app.debug(f"update: skipped release tag {tag} (not a semantic version)")
                continue
            if v.pre:
                # a prerelease is installed only when asked for with --target-version,
                # whatever the release's prerelease flag says (E2)
                self.app.debug(f"update: skipped release tag {tag} (a prerelease)")
                continue
            considered.append((v, rel))
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
        return _default_run(argv, timeout, self.path)

    def installed_version_argv(self) -> list[str]:
        """How to ask the installed tool its version after a package manager ran
        (E6): the tool's command found again on PATH (absolute entries only), since
        an upgrade can move it (pnpm 11 leaves the old store path behind); else the
        running copy's own path."""
        found = find_program(self.tool, self.path)
        if found:
            return _version_argv(found)
        return _version_argv(self.exe) if self.cfg.executable else _self_version_argv()

    def observe_version(self, argv: list[str]) -> str | None:
        """The version a `--version` run reports (_reported_version): None when it
        does not run or exits non-zero, "" when it ran but reported no version."""
        try:
            code, out = self.run_cmd(argv, VERSION_TIMEOUT)
        except OSError:
            return None
        if code != 0:
            return None
        return _reported_version(out) or ""

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
            argv = self.install_argv(package_manager_args(manager, pkg, target))
        except ValueError as exc:
            raise self.fail(
                "E_UNSUPPORTED",
                f"{manager} cannot install {tool} {target}: {exc}.",
                f"Install {tool} with the package-manager command in its README instead.",
            ) from None
        if manager == "pip":
            self.require_pip(pkg)
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
            code = -1  # the manager never started: nothing was installed
        observed = self.observe_version(self.installed_version_argv())
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

    def require_pip(self, pkg: str) -> None:
        """The running interpreter must have pip (E7): a uv tool or some pipx
        environments have none, and their own upgrade command is the way."""
        try:
            code, _ = self.run_cmd([sys.executable, "-m", "pip", "--version"], VERSION_TIMEOUT)
        except OSError:
            code = -1
        if code != 0:
            raise self.fail(
                "E_UNSUPPORTED",
                f"{self.tool} cannot update itself with pip: its Python environment has no pip.",
                f"This Python environment has no pip; upgrade with `pipx upgrade {pkg}` or "
                f"`uv tool upgrade {pkg}`.",
            )

    # ---------------------------------------------------------------- binary

    def remove_stale(self, exe: str) -> None:
        """Remove <exe>.old and <exe>.old-<n> left by an earlier update on Windows
        (best effort)."""
        if not exe:
            return
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
        # Every download is https (E13); a release that names another one cannot be
        # installed by update (the stage codes of download have no integrity failure).
        for name in (archive_name, CHECKSUMS, BUNDLE):
            if not https_url(assets[name]):
                raise self.fail(
                    "E_UNSUPPORTED",
                    f"Release v{version} names a download that is not https, so it was not "
                    "installed.",
                    f"Install {tool} with the package-manager command in its README instead, and "
                    "report the release to the tool's maintainers.",
                )
        if not self.exe:
            raise self.fail(
                "E_IO",
                f"{tool} cannot find its own executable, so it cannot update itself.",
                f"Reinstall {tool} with the install command in its README.",
            )
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
        except _NetError as exc:
            raise self.net_error(exc, version) from None
        archive = os.path.join(self.tmp, archive_name)
        try:
            digest = net.get_to(assets[archive_name], archive, MAX_ARCHIVE)
        except _TooLarge:
            raise self.checksum_failed() from None
        except _WriteFailed:
            raise self.cannot_write(exe_dir) from None
        except _NetError as exc:
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
        if not reported:
            raise self.fail(
                "E_IO",
                "The downloaded binary did not report its version, so it was not installed.",
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
        move the running one aside first, and back if the second rename fails). When
        moving it back fails too, the tool may be missing: dispatch_state unknown and
        a next_action that names the file to rename back (E10)."""
        exe = self.exe
        self.interrupts.committing = True
        try:
            if _IS_WINDOWS:
                self._swap_windows(new, exe)
            else:
                rename_retry(new, exe)
        except OSError as exc:
            if self.dispatch_unknown:
                raise self.fail(
                    "E_IO",
                    "The update failed and the old version could not be put back; "
                    f"{self.tool} may be missing.",
                    f"Rename {self.old_binary} back to {exe}, then run {self.tool} --version.",
                ) from None
            reason = _reason(exc)
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
            except OSError:
                self.dispatch_unknown = True
                self.old_binary = old
            raise

    def replace_skills(self, extracted: str) -> None:
        """In <exe dir>/skills, put each Skill directory of the verified release's
        skills/ at skills/<name>, one name at a time (E3): the directory itself and
        the other names in it, which may be other tools' Skills in a shared
        directory, are left alone; a release without skills/ leaves it alone. The
        names become the Skills this run syncs."""
        src = os.path.join(extracted, "skills")
        self.release_skills = []
        try:
            st = os.lstat(src)
        except OSError:
            return
        if is_link_or_reparse(st) or not stat.S_ISDIR(st.st_mode):
            return
        names = _dir_names(src)
        dst = os.path.join(os.path.dirname(os.path.abspath(self.exe)), "skills")
        try:
            st = os.lstat(dst)
        except FileNotFoundError:
            os.mkdir(dst, 0o755)
        else:
            if is_link_or_reparse(st) or not stat.S_ISDIR(st.st_mode):
                raise OSError(f"{dst} is not a directory")
        for name in names:
            _replace_dir(os.path.join(src, name), dst, name)
            self.release_skills.append(name)

    # ---------------------------------------------------------------- Skills

    def skill_source(self) -> str | None:
        """Where the installed package keeps its Skills: <exe dir>/skills for a
        binary, skills/ in the npm wrapper package, none for the others."""
        if self.method == "binary":
            if not self.exe:
                return None
            return os.path.join(os.path.dirname(os.path.abspath(self.exe)), "skills")
        if self.method in ("npm", "pnpm", "yarn"):
            root = npm_package_root(self.exe, self.cfg.npm_package)
            return os.path.join(root, "skills") if root else None
        return None

    def own_skills(self, source: str) -> list[str] | None:
        """The Skills this run syncs (E3). After an install, the names the verified
        release shipped. On an up-to-date run of a binary install, the tool's own
        Skills in <exe dir>/skills, which other tools in the same directory may share:
        the entry Skill named after the tool and the domain Skills whose
        metadata.requires.skills names it (SKL-010). None, every Skill of the
        source, for the package-manager installs, whose package holds only this
        tool's Skills."""
        if self.release_skills is not None:
            return self.release_skills
        if self.method != "binary":
            return None
        names: list[str] = []
        for name in _dir_names(source):
            if name == self.tool:
                names.append(name)
                continue
            try:
                with open(os.path.join(source, name, "SKILL.md"), "rb") as fh:
                    text = fh.read(256 << 10).decode("utf-8", "replace")
            except OSError:
                continue
            fm = _SKILL_FRONTMATTER.match(text)
            m = _SKILL_REQUIRES.search(fm.group(1)) if fm else None
            if m is not None and m.group(1) == self.tool:
                names.append(name)
        return names

    def sync(self, version: str) -> str:
        """Copy each Skill of the installed package into the Skill directories that
        hold it (stage skill_sync): a real directory is replaced by a copy, a link is
        left alone, a missing one is not created; own_skills limits which Skills.
        Links are never followed: a source Skill or a skills/ directory that is a
        link is not copied. Returns skipped (no source, or no Skill installed
        anywhere), not_needed (every installed copy already has this version) or
        synced (something was copied); a failure raises."""
        self.begin("skill_sync")
        source = self.skill_source()
        if not source:
            return "skipped"
        try:
            st = os.lstat(source)
        except OSError:
            return "skipped"
        if is_link_or_reparse(st) or not stat.S_ISDIR(st.st_mode):
            return "skipped"
        names = self.own_skills(source)
        skills = [n for n in skill_names(source) if names is None or n in names]
        found: list[tuple[str, str, bool, bool]] = []  # skill, path, link, current
        for name in skills:
            for root in self.app._skill_dirs():
                dst = os.path.join(root, name)
                try:
                    dst_st = os.lstat(dst)
                except OSError:
                    continue
                link = is_link_or_reparse(dst_st)
                if not link and not stat.S_ISDIR(dst_st.st_mode):
                    continue
                current = _same_version_text(_skill_version(dst), version)
                found.append((name, root, link, current))
        if not found:
            return "skipped"
        if all(current for _, _, _, current in found):
            return "not_needed"
        copied = 0
        for name, root, link, current in found:
            if link or current:
                continue
            _replace_dir(os.path.join(source, name), root, name)
            copied += 1
        return "synced" if copied else "skipped"

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
        if not isinstance(rel, dict) or rel.get("draft") or rel.get("prerelease"):
            return
        tag = rel.get("tag_name")
        v = parse_version(tag if isinstance(tag, str) else "")
        if v.pre:
            return  # a prerelease is never advertised (E2, CLI-071)
        severity = update_severity(_parse_or_none(app._version()), v, [(v, rel)])
        app.record_update_check(str(v), severity)
    except Exception:  # best effort: doctor never fails on it
        return
