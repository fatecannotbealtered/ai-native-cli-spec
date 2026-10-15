"""The built-in update (D38): the golden results G-U1..G-U5 and the failure paths,
against a fake GitHub (a local HTTPS server), a fake verifier and fake commands. No
network, no package manager and no real signature are involved."""

from __future__ import annotations

import hashlib
import io
import json
import os
import ssl
import sys
import tarfile
import tempfile
import threading
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import click
import pytest
from aicli_testapp import THarness, TOut, Upstream, notes_app

import aicli
from aicli import OBSERVE, App, Meta, SchemaDef, UpdateConfig
from aicli import update as U
from aicli import validate as V

REPO = "fatecannotbealtered/notes-cli-example"
NPM = "notes-cli-example"
RELEASES = f"/repos/{REPO}/releases"
LIST = RELEASES + "?per_page=50"
IDENTITY = f"https://github.com/{REPO}/.github/workflows/release.yml@refs/tags/v1.3.1"
ISSUER = "https://token.actions.githubusercontent.com"

# The golden results of DESIGN-ADDITIONS D38 (data / error.details, minus duration_ms).
G_U1 = {
    "status": "available",
    "current_version": "1.3.0",
    "target_version": "1.3.1",
    "update_available": True,
    "install_method": "binary",
    "signature_status": "not_checked",
    "skill_sync_status": "skipped",
}
G_U2 = {
    "status": "updated",
    "current_version": "1.3.1",
    "target_version": "1.3.1",
    "previous_version": "1.3.0",
    "update_available": False,
    "install_method": "binary",
    "signature_status": "verified",
    "signature_verified": True,
    "checksum_verified": True,
    "skill_sync_status": "synced",
    "binary_replaced": True,
}
G_U3 = {
    "status": "updated",
    "current_version": "1.3.1",
    "target_version": "1.3.1",
    "previous_version": "1.3.0",
    "update_available": False,
    "install_method": "npm",
    "signature_status": "not_checked",
    "signature_verified": False,
    "skill_sync_status": "synced",
    "binary_replaced": True,
    "command": ["npm", "install", "-g", "notes-cli-example@1.3.1"],
}
G_U4 = {
    "dispatch_state": "not_started",
    "next_action": "Do not retry; tell the user the release failed signature verification.",
    "stage": "verify_signature",
    "current_version": "1.3.0",
    "target_version": "1.3.1",
    "binary_replaced": False,
    "skill_sync_status": "skipped",
    "install_method": "binary",
}


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake GitHub is local: never route it through a proxy of the machine."""
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------- the fake GitHub

# Release assets and redirects must be https (E13): the fake GitHub serves TLS with
# this certificate for 127.0.0.1, issued by a test CA that the tests trust through
# NOTES_CA_FILE like a private CA (CLI-110). Generated once for these tests only
# (ECDSA P-256, valid 2026-2125); the key protects nothing.
FAKE_GITHUB_CA = """-----BEGIN CERTIFICATE-----
MIIBtjCCAVygAwIBAgIUL2u8/3etJHv1q60g/8MOV3f8LzQwCgYIKoZIzj0EAwIw
JjEkMCIGA1UEAwwbYWljbGkgdGVzdCBDQSAoZmFrZSBHaXRIdWIpMCAXDTI2MDEw
MTAwMDAwMFoYDzIxMjUxMjMxMDAwMDAwWjAmMSQwIgYDVQQDDBthaWNsaSB0ZXN0
IENBIChmYWtlIEdpdEh1YikwWTATBgcqhkjOPQIBBggqhkjOPQMBBwNCAAQvGXO1
AtxIgQcXbVM+gfqgQCkJMF+RU/YrnL/FAltoVe5FcF5fB5rMz8RFIBIRvJ/iR8uJ
JsCLWZ7qOG1wviapo2YwZDASBgNVHRMBAf8ECDAGAQH/AgEAMA4GA1UdDwEB/wQE
AwIBBjAdBgNVHQ4EFgQUGjNklabH3W3lvaaM10GCatVrj18wHwYDVR0jBBgwFoAU
GjNklabH3W3lvaaM10GCatVrj18wCgYIKoZIzj0EAwIDSAAwRQIgRRpegJT81poH
THbeIXkJM/PlOcwKAUvA3U9kjLZbhYgCIQDPGXpESkPh0SEUmL7Km5KSJ5gU5m3k
rWGzz5LX7FqbjQ==
-----END CERTIFICATE-----
"""
FAKE_GITHUB_CERT = """-----BEGIN CERTIFICATE-----
MIIB0TCCAXegAwIBAgIUUCq6l9eAwFuS5dFuN4lT7u17mrcwCgYIKoZIzj0EAwIw
JjEkMCIGA1UEAwwbYWljbGkgdGVzdCBDQSAoZmFrZSBHaXRIdWIpMCAXDTI2MDEw
MTAwMDAwMFoYDzIxMjUxMjMxMDAwMDAwWjAUMRIwEAYDVQQDDAlsb2NhbGhvc3Qw
WTATBgcqhkjOPQIBBggqhkjOPQMBBwNCAAS6b/GZi2VIujKKsM5v4xklnw7HcS3d
9WKi6ZD3PWmCPg90dkl+RzFHsVxjTuDU097Z6m4J4Sc+zJqEGLm4QgKCo4GSMIGP
MAwGA1UdEwEB/wQCMAAwDgYDVR0PAQH/BAQDAgeAMBMGA1UdJQQMMAoGCCsGAQUF
BwMBMBoGA1UdEQQTMBGHBH8AAAGCCWxvY2FsaG9zdDAdBgNVHQ4EFgQUmTg1mFDe
tM6Xs3t3HwL9PkgKWhwwHwYDVR0jBBgwFoAUGjNklabH3W3lvaaM10GCatVrj18w
CgYIKoZIzj0EAwIDSAAwRQIgT/0klEJWt1nmbPBTUePOH1K2+2JYc/jVIbAINvTr
GSICIQDu6SofqMdtaQ+MfreJfQKB+bhEnboGncsotGki4TwrTQ==
-----END CERTIFICATE-----
"""
FAKE_GITHUB_KEY = """-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgteYGucRBaX2x7EV1
CjrPut4sSohJKN5Dj5D8vqNeQOWhRANCAAS6b/GZi2VIujKKsM5v4xklnw7HcS3d
9WKi6ZD3PWmCPg90dkl+RzFHsVxjTuDU097Z6m4J4Sc+zJqEGLm4QgKC
-----END PRIVATE KEY-----
"""


class FakeGitHub:
    """The GitHub API and release downloads on https://127.0.0.1. routes maps a
    request path (with its query) to (status, headers, body), or to "drop" to close
    the connection without an answer; anything else is 404. ca_file is the PEM of
    the CA its certificate is issued by."""

    def __init__(self) -> None:
        self.routes: dict[str, Any] = {}
        self.requests: list[tuple[str, dict[str, str]]] = []
        self._pem = tempfile.TemporaryDirectory(prefix="fake-github-")
        pem: dict[str, str] = {}
        for name, text in (
            ("ca.pem", FAKE_GITHUB_CA),
            ("cert.pem", FAKE_GITHUB_CERT),
            ("key.pem", FAKE_GITHUB_KEY),
        ):
            pem[name] = os.path.join(self._pem.name, name)
            with open(pem[name], "w", encoding="ascii") as fh:
                fh.write(text)
        self.ca_file = pem["ca.pem"]
        gh = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                gh.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
                route = gh.routes.get(self.path)
                if route == "drop":
                    self.close_connection = True
                    return
                if route is None:
                    route = (404, {"Content-Type": "application/json"}, b'{"message":"Not Found"}')
                status, headers, body = route
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(pem["cert.pem"], pem["key.pem"])
        self.server.socket = tls.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()
        self.host = f"127.0.0.1:{self.server.server_address[1]}"
        self.base = "https://" + self.host

    def json(self, path: str, obj: Any, status: int = 200, headers: dict[str, str] | None = None):
        self.routes[path] = (
            status,
            {"Content-Type": "application/json", **(headers or {})},
            json.dumps(obj).encode("utf-8"),
        )

    def file(self, path: str, body: bytes) -> None:
        self.routes[path] = (200, {"Content-Type": "application/octet-stream"}, body)

    def downloads(self) -> list[str]:
        return [p for p, _ in self.requests if p.startswith("/dl/")]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._pem.cleanup()


def skill_md(version: str) -> str:
    return (
        f"---\nname: notes\ndescription: Notes.\nmetadata:\n  version: {version}\n"
        f"  min_version: {version}\n---\n\n# notes\n"
    )


def exe_name() -> str:
    return "notes.exe" if os.name == "nt" else "notes"


def archive(
    version: str, binary: bytes, skill_version: str, extra: list[tuple[str, bytes]]
) -> tuple[str, bytes]:
    """The release archive for this machine: the binary and skills/ at the root."""
    osname, arch = U.release_platform()
    name = U.asset_names("notes", version, osname, arch)[0]
    files = [
        (exe_name(), binary),
        ("skills/notes/SKILL.md", skill_md(skill_version).encode("utf-8")),
        ("README.md", b"not extracted"),
        *extra,
    ]
    buf = io.BytesIO()
    if name.endswith(".zip"):
        with zipfile.ZipFile(buf, "w") as zf:
            for n, b in files:
                zf.writestr(n, b)
    else:
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for n, b in files:
                info = tarfile.TarInfo(n)
                info.size, info.mode = len(b), 0o755
                tf.addfile(info, io.BytesIO(b))
    return name, buf.getvalue()


def publish(
    gh: FakeGitHub,
    version: str = "1.3.1",
    *,
    binary: bytes = b"new binary",
    checksum: str = "",
    signed: bool = True,
    body: str = "",
    dl: FakeGitHub | None = None,
    extra: list[tuple[str, bytes]] | None = None,
) -> dict[str, Any]:
    """A release with its archive (extra entries added), checksums.txt and bundle
    (served by dl, default gh); its tag route too."""
    dl = dl or gh
    name, data = archive(version, binary, version, extra or [])
    digest = checksum or hashlib.sha256(data).hexdigest()
    sums = f"{'0' * 64}  notes-{version}-plan9-amd64.tar.gz\n{digest}  {name}\n"
    dl.file(f"/dl/v{version}/{name}", data)
    dl.file(f"/dl/v{version}/checksums.txt", sums.encode("utf-8"))
    dl.file(f"/dl/v{version}/checksums.txt.sigstore.json", b'{"fake": "bundle"}')
    names = [name, "checksums.txt", "checksums.txt.sigstore.json"] if signed else [name]
    rel = {
        "tag_name": "v" + version,
        "draft": False,
        "prerelease": False,
        "body": body,
        "assets": [
            {"name": n, "browser_download_url": f"{dl.base}/dl/v{version}/{n}"} for n in names
        ],
    }
    gh.json(f"{RELEASES}/tags/v{version}", rel)
    return rel


def release(version: str, **kw: Any) -> dict[str, Any]:
    return {"tag_name": "v" + version, "draft": False, "prerelease": False, "assets": [], **kw}


# ---------------------------------------------------------------- an installed tool


@dataclass
class Box:
    """An installed copy of notes 1.3.0 (bin/notes and bin/skills), two Skill roots
    with the Skill installed in the first, a fake GitHub and fake commands."""

    root: str
    gh: FakeGitHub
    h: THarness = field(init=False)
    method: str | None = "binary"
    exe: str = ""
    installed_version: str = "1.3.1"  # what the installed tool reports after a manager run
    new_version: str = "1.3.1"  # what a downloaded binary reports
    manager: tuple[int, bytes] = (0, b"added 1 package")
    verify_error: BaseException | None = None
    runs: list[list[str]] = field(default_factory=list)
    verified: list[tuple[bytes, str, str]] = field(default_factory=list)
    cfg: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.bin = os.path.join(self.root, "bin")
        self.exe = os.path.join(self.bin, exe_name())
        write(self.exe, b"old binary")
        write(os.path.join(self.bin, "skills", "notes", "SKILL.md"), skill_md("1.3.0"))
        self.skill_roots = [os.path.join(self.root, "agents"), os.path.join(self.root, "claude")]
        for r in self.skill_roots:
            os.makedirs(r, exist_ok=True)
        write(os.path.join(self.skill_roots[0], "notes", "SKILL.md"), skill_md("1.3.0"))
        os.makedirs(os.path.join(self.root, "h"), exist_ok=True)
        self.h = THarness(os.path.join(self.root, "h"))
        self.h.env["NOTES_CA_FILE"] = self.gh.ca_file  # trusts the fake GitHub's TLS
        self.h.build = self.build

    def fake_run(self, argv: list[str]) -> tuple[int, bytes]:
        self.runs.append(list(argv))
        if argv[-1] == "--version":
            v = self.new_version if ".notes-update-" in argv[0] else self.installed_version
            env = {"ok": True, "schema_version": "2.0", "data": {"tool": "notes", "version": v}}
            return 0, (json.dumps(env) + "\n").encode("utf-8")
        return self.manager

    def fake_verify(self, artifact: bytes, bundle: bytes, identity: str, issuer: str) -> None:
        self.verified.append((artifact, identity, issuer))
        if self.verify_error is not None:
            raise self.verify_error

    def build(self, u: Upstream) -> App:
        a = notes_app(u)
        a.version = "1.3.0"
        a.skill_dirs = list(self.skill_roots)
        kw: dict[str, Any] = {
            "repo": REPO,
            "npm_package": NPM,
            "api_base": self.gh.base,
            "verify": self.fake_verify,
            "executable": lambda: self.exe,
            "run": self.fake_run,
        }
        if self.method is not None:
            kw["install_method"] = lambda exe: self.method
        kw.update(self.cfg)
        a.update = UpdateConfig(**kw)
        return a

    def run(self, *args: str) -> TOut:
        return self.h.run(*args)

    def skill(self, root: int) -> str:
        p = os.path.join(self.skill_roots[root], "notes", "SKILL.md")
        with open(p, encoding="utf-8") as fh:
            return aicli.builtins.skill_versions(fh.read())[0]

    def leftovers(self) -> list[str]:
        return [n for n in os.listdir(self.bin) if n.startswith(".")]


def write(path: str, content: str | bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = content.encode("utf-8") if isinstance(content, str) else content
    with open(path, "wb") as fh:
        fh.write(data)


def read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


@pytest.fixture
def box(tmp_path: Any) -> Iterator[Box]:
    gh = FakeGitHub()
    try:
        yield Box(str(tmp_path), gh)
    finally:
        gh.close()


# ---------------------------------------------------------------- the golden results


def test_g_u1_check_binary(box: Box) -> None:
    """G-U1: update --check on a binary install, latest 1.3.1, current 1.3.0. Drafts
    and prereleases are skipped; nothing is downloaded, run or replaced."""
    publish(box.gh, "1.3.1")
    draft = release("2.0.0", draft=True)
    pre = release("1.4.0-rc.1", prerelease=True)
    box.gh.json(LIST, [draft, pre, release("1.3.1"), release("1.3.0")])
    r = box.run("update", "--check")
    assert r.exit == 0 and r.data() == G_U1, r
    assert V._run(lambda v, d: V._update(v, "data", d), r.data()) is None
    assert box.gh.downloads() == [] and box.runs == [] and read(box.exe) == b"old binary"
    assert "notices" not in r.env["meta"], "update never carries the update notice"
    with open(os.path.join(box.h.state, "update-check.json"), encoding="utf-8") as fh:
        cache = json.load(fh)
    assert cache["latest_version"] == "1.3.1" and cache["severity"] == "info", cache
    # --dry-run is a synonym of --check; another command now shows the notice
    assert box.run("update", "--dry-run").data() == G_U1
    notice = box.run("note", "get", "1").env["meta"]["notices"][0]
    assert notice["type"] == "update_available" and notice["latest_version"] == "1.3.1", notice


def test_g_u2_update_binary(box: Box) -> None:
    """G-U2: the binary path with a passing verifier; the Skill installed in one
    root at 1.3.0 is synced, the other root gets nothing."""
    box.gh.json(LIST, [publish(box.gh, "1.3.1"), release("1.3.0")])
    r = box.run("update")
    assert r.exit == 0 and r.data() == G_U2, r
    V._run(lambda v, d: V._update(v, "data", d), r.data())
    assert read(box.exe) == b"new binary"
    assert box.skill(0) == "1.3.1"
    assert not os.path.exists(os.path.join(box.skill_roots[1], "notes")), "never installs a Skill"
    with open(os.path.join(box.bin, "skills", "notes", "SKILL.md"), encoding="utf-8") as fh:
        assert "version: 1.3.1" in fh.read(), "<exe dir>/skills is the release's skills/"
    assert [(i, s) for _, i, s in box.verified] == [(IDENTITY, ISSUER)]
    assert box.verified[0][0].endswith(b"\n") and b"  notes-1.3.1-" in box.verified[0][0]
    assert box.leftovers() == [], "the temporary directory and the old skills/ are removed"
    assert len(box.runs) == 1 and box.runs[0][-1] == "--version", box.runs  # pre-swap check
    old = box.exe + ".old"
    assert os.path.exists(old) == (os.name == "nt"), "Windows moves the running binary aside"
    # A later update removes the <exe>.old a Windows swap left behind.
    box.gh.json(LIST, [release("1.3.0")])
    r = box.run("update")
    assert r.exit == 0 and r.data()["status"] == "up_to_date", r
    assert not os.path.exists(old)


def test_g_u3_update_npm(box: Box) -> None:
    """G-U3: the npm path; the manager succeeds and the tool then reports 1.3.1;
    the Skill comes from the wrapper package's skills/."""
    pkg = os.path.join(box.root, "prefix", "lib", "node_modules", NPM)
    write(os.path.join(pkg, "package.json"), json.dumps({"name": NPM, "version": "1.3.1"}))
    write(os.path.join(pkg, "skills", "notes", "SKILL.md"), skill_md("1.3.1"))
    platform_pkg = os.path.join(pkg, "node_modules", NPM + "-platform")
    write(os.path.join(platform_pkg, "package.json"), json.dumps({"name": NPM + "-platform"}))
    box.exe = os.path.join(platform_pkg, "bin", exe_name())
    write(box.exe, b"npm binary")
    box.method = "npm"
    box.gh.json(LIST, [release("1.3.1"), release("1.3.0")])
    r = box.run("update")
    assert r.exit == 0 and r.data() == G_U3, r
    V._run(lambda v, d: V._update(v, "data", d), r.data())
    assert box.runs == [G_U3["command"], [box.exe, "--version"]], box.runs
    assert box.skill(0) == "1.3.1"
    assert box.gh.downloads() == [], "a package manager install downloads nothing itself"


def test_g_u4_bad_signature(box: Box) -> None:
    """G-U4: the signature does not verify: E_INTEGRITY, nothing installed, the
    temporary directory removed."""
    box.gh.json(LIST, [publish(box.gh, "1.3.1")])
    box.verify_error = ValueError("certificate identity mismatch")
    r = box.run("update")
    assert r.exit == 1 and r.code() == "E_INTEGRITY", r
    assert r.details() == G_U4, r.details()
    assert r.env["error"]["message"] == U.SIGNATURE_FAILED
    assert r.env["error"]["retryable"] is False
    assert read(box.exe) == b"old binary" and box.leftovers() == [] and box.skill(0) == "1.3.0"


def test_g_u5_target_version_is_checked_before_any_request(box: Box) -> None:
    """G-U5: --target-version '1.0.12&calc' is the existing check_update_target
    result, stage discover, checked before anything else (so no install_method)
    and before any request."""
    r = box.run("update", "--target-version", "1.0.12&calc")
    assert r.exit == 2 and r.code() == "E_USAGE", r
    assert r.details() == {
        "dispatch_state": "not_started",
        "next_action": "Give --target-version a value from details.expected and run the command "
        "again.",
        "problem": "invalid_value",
        "parameter": "--target-version",
        "expected": "semantic version, e.g. 1.2.3",
        "stage": "discover",
        "current_version": "1.3.0",
        "binary_replaced": False,
        "skill_sync_status": "skipped",
    }, r.details()
    assert box.gh.requests == [], box.gh.requests


# ---------------------------------------------------------------- failure paths


def test_checksum_mismatch(box: Box) -> None:
    box.gh.json(LIST, [publish(box.gh, "1.3.1", checksum="ab" * 32)])
    r = box.run("update")
    assert r.code() == "E_INTEGRITY" and r.env["error"]["message"] == U.CHECKSUM_FAILED, r
    d = r.details()
    assert d["stage"] == "verify_checksum" and d["binary_replaced"] is False, d
    assert (
        d["next_action"]
        == U.CHECKSUM_NEXT
        == ("Do not retry; tell the user the release archive failed its checksum.")
    )
    assert read(box.exe) == b"old binary" and box.leftovers() == []


def test_unsigned_release_and_wrong_binary_version(box: Box) -> None:
    box.gh.json(LIST, [publish(box.gh, "1.3.1", signed=False)])
    r = box.run("update")
    assert r.code() == "E_INTEGRITY" and r.details()["stage"] == "verify_signature", r
    assert r.env["error"]["message"] == (
        "Release v1.3.1 has no signed checksums, so it was not installed."
    )
    box.gh.json(LIST, [publish(box.gh, "1.3.1")])
    box.new_version = "1.2.0"
    r = box.run("update")
    assert r.code() == "E_INTEGRITY" and r.details()["stage"] == "replace", r
    assert r.env["error"]["message"] == (
        "The downloaded binary reports version 1.2.0, not 1.3.1, so it was not installed."
    )
    assert read(box.exe) == b"old binary"


def test_unsafe_archive_entry(box: Box) -> None:
    box.gh.json(LIST, [publish(box.gh, "1.3.1", extra=[("../evil", b"x")])])
    r = box.run("update")
    assert r.exit == 1 and r.code() == "E_INTEGRITY", r
    assert r.env["error"]["message"] == (
        "The release archive holds an unsafe entry `../evil`, so it was not installed."
    )
    d = r.details()
    assert d["stage"] == "replace" and d["binary_replaced"] is False, d
    assert d["next_action"] == "Do not retry; tell the user the release archive is unsafe."
    assert read(box.exe) == b"old binary" and box.leftovers() == []
    assert not os.path.exists(os.path.join(box.root, "evil"))


def test_tags_that_are_not_semver_are_skipped(box: Box) -> None:
    box.h.env["NOTES_DEBUG"] = "1"
    box.gh.json(LIST, [release("nightly"), release("1.3.1"), release("1.3.0")])
    r = box.run("update", "--check")
    assert r.exit == 0 and r.data()["target_version"] == "1.3.1", r
    assert "debug: update: skipped release tag vnightly (not a semantic version)\n" in r.stderr
    box.gh.json(LIST, [release("nightly"), release("1.3.1", prerelease=True)])
    r = box.run("update", "--check")
    assert r.exit == 2 and r.code() == "E_UNSUPPORTED", r
    assert r.env["error"]["message"] == (
        f"No release of {REPO} has a semantic-version tag, so there is nothing to update to."
    )
    assert r.details()["next_action"] == (
        "Report this to the tool's maintainers; nothing was changed."
    )
    assert r.details()["stage"] == "discover", r.details()


def test_missing_target_release(box: Box) -> None:
    r = box.run("update", "--target-version", "1.9.9")
    assert r.exit == 2 and r.code() == "E_USAGE", r
    assert r.env["error"]["message"] == f"No release v1.9.9 exists in {REPO}.", r
    d = r.details()
    assert d["parameter"] == "--target-version" and d["target_version"] == "1.9.9", d
    assert d["next_action"] == (
        "Run `notes update --check` to see the latest version, or give an existing version."
    )
    assert [p for p, _ in box.gh.requests] == [RELEASES + "/tags/v1.9.9"]


def test_network_failure_in_discover(box: Box) -> None:
    box.gh.routes[LIST] = "drop"
    r = box.run("update")
    assert r.exit == 7 and r.code() == "E_NETWORK" and r.env["error"]["retryable"] is True, r
    assert r.env["error"]["message"] == f"Could not reach {box.gh.host} to check for releases."
    d = r.details()
    assert d["stage"] == "discover" and d["dispatch_state"] == "not_started", d
    assert d["next_action"] == (
        "Check the network or HTTPS_PROXY, then run `notes update` again; it is safe to repeat."
    )
    box.gh.routes[LIST] = (
        403,
        {"X-RateLimit-Remaining": "0", "Retry-After": "42"},
        b'{"message":"API rate limit exceeded"}',
    )
    r = box.run("update")
    assert r.code() == "E_RATE_LIMITED" and r.details()["retry_after_ms"] == 42000, r
    assert r.details()["next_action"] == (
        "Wait 42 s, or set GITHUB_TOKEN, then run `notes update` again."
    )
    box.gh.routes[LIST] = (500, {}, b"oops")
    r = box.run("update")
    assert r.env["error"]["message"] == "The release source answered HTTP 500.", r


def test_token_goes_only_to_the_api_host(box: Box) -> None:
    """GITHUB_TOKEN is sent to the API host only, never with the downloads (served
    from another host, as github.com is not api.github.com)."""
    box.h.env["GITHUB_TOKEN"] = "ghp_secret"
    downloads = FakeGitHub()
    try:
        box.gh.json(LIST, [publish(box.gh, "1.3.1", dl=downloads)])
        assert box.run("update").exit == 0
        assert len(downloads.downloads()) == 3, downloads.requests
        requests = box.gh.requests + downloads.requests
    finally:
        downloads.close()
    for path, headers in requests:
        auth = headers.get("authorization")
        assert (auth == "Bearer ghp_secret") == path.startswith("/repos/"), (path, auth)
        assert headers.get("user-agent") == "notes/1.3.0 aicli/" + aicli.SPEC_VERSION


def test_package_manager_failure(box: Box) -> None:
    box.method = "npm"
    box.manager = (1, b"npm ERR! code E403")
    box.installed_version = "1.3.0"
    box.gh.json(LIST, [release("1.3.1")])
    r = box.run("update")
    assert r.exit == 1 and r.code() == "E_IO", r
    assert r.env["error"]["message"] == (
        "`npm install -g notes-cli-example@1.3.1` failed, so notes is still 1.3.0."
    )
    d = r.details()
    assert d["command"] == ["npm", "install", "-g", "notes-cli-example@1.3.1"], d
    assert d["dispatch_state"] == "dispatched" and d["stage"] == "replace", d
    assert d["next_action"] == (
        "Run `npm install -g notes-cli-example@1.3.1` yourself, then run `notes update` to sync "
        "the Skills."
    )
    box.manager = (0, b"")  # succeeded, but another notes comes first on PATH
    r = box.run("update")
    assert r.env["error"]["message"] == (
        "npm finished, but `notes --version` still reports 1.3.0."
    ), r
    box.installed_version = ""  # the installed tool prints no version
    r = box.run("update")
    assert r.env["error"]["message"] == (
        "npm finished, but `notes --version` still reports an unknown version."
    ), r


def test_package_manager_missing_or_unknown_package(box: Box) -> None:
    box.method = "pip"
    box.gh.json(LIST, [release("1.3.1")])
    r = box.run("update")
    assert r.exit == 4 and r.code() == "E_CONFIG", r
    assert r.env["error"]["message"] == (
        "notes does not know its pip package name, so it cannot update this install."
    )
    assert r.details()["stage"] == "replace" and box.runs == [], r.details()
    box.cfg["pypi_package"] = "notes-cli-example"
    box.gh.json(RELEASES + "/tags/v1.4.0-rc.1", release("1.4.0-rc.1", prerelease=True))
    r = box.run("update", "--target-version", "1.4.0-rc.1")
    assert r.exit == 2 and r.code() == "E_UNSUPPORTED", r
    assert r.env["error"]["message"] == (
        "pip cannot install notes 1.4.0-rc.1: pip installs release versions only."
    )
    assert r.details()["next_action"] == (
        "Install notes with the package-manager command in its README instead."
    )

    def missing(argv: list[str]) -> tuple[int, bytes]:
        raise FileNotFoundError(argv[0])

    box.cfg["run"] = missing
    box.method = "npm"
    r = box.run("update")
    assert r.code() == "E_CONFIG" and r.env["error"]["message"] == "npm was not found on PATH.", r
    assert r.details()["dispatch_state"] == "not_started"


def test_pip_update_has_no_skill_source(box: Box) -> None:
    """updated with skill_sync_status skipped (pip has no Skill source) is valid,
    without skill_sync_command (F10); pip is this interpreter's `python -m pip`
    (E7), checked first."""
    box.method = "pip"
    box.cfg["pypi_package"] = "notes-cli-example"
    box.gh.json(LIST, [release("1.3.1")])
    r = box.run("update")
    assert r.exit == 0, r
    command = [sys.executable, "-m", "pip", "install", "--upgrade", "notes-cli-example==1.3.1"]
    assert r.data() == {
        "status": "updated",
        "current_version": "1.3.1",
        "target_version": "1.3.1",
        "previous_version": "1.3.0",
        "update_available": False,
        "install_method": "pip",
        "signature_status": "not_checked",
        "signature_verified": False,
        "skill_sync_status": "skipped",
        "binary_replaced": True,
        "command": command,
    }, r.data()
    V._run(lambda v, d: V._update(v, "data", d), r.data())
    assert box.runs[:2] == [[sys.executable, "-m", "pip", "--version"], command], box.runs


def test_check_of_a_downgrade_and_a_missing_executable(box: Box) -> None:
    box.gh.json(RELEASES + "/tags/v1.2.0", release("1.2.0"))
    r = box.run("update", "--check", "--target-version", "1.2.0", "--allow-downgrade")
    assert r.exit == 0 and r.data()["status"] == "available", r
    assert r.data()["target_version"] == "1.2.0" and r.data()["update_available"] is True, r
    V._run(lambda v, d: V._update(v, "data", d), r.data())
    # E11: --check never needs the executable; the binary path fails at download
    box.exe = ""
    box.gh.json(LIST, [publish(box.gh, "1.3.1")])
    r = box.run("update", "--check")
    assert r.exit == 0 and r.data()["status"] == "available", r
    r = box.run("update")
    assert r.exit == 1 and r.code() == "E_IO", r
    assert r.env["error"]["message"] == (
        "notes cannot find its own executable, so it cannot update itself."
    )
    assert r.details()["stage"] == "download" and r.details()["dispatch_state"] == "not_started"
    assert r.details()["next_action"] == "Reinstall notes with the install command in its README."


def test_skill_sync_failure_after_the_swap(box: Box, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(src: str, dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(U, "_copy_tree", broken)
    box.gh.json(LIST, [publish(box.gh, "1.3.1")])
    r = box.run("update")
    assert r.exit == 1 and r.code() == "E_PARTIAL", r
    assert r.env["error"]["message"] == (
        "notes was updated but its Skills were not synced, so the installed Skill still describes "
        "the old version."
    )
    assert r.details() == {
        "dispatch_state": "dispatched",
        "next_action": "Run `notes update` again to finish the Skill sync, then read "
        "`notes changelog --since 1.3.0`.",
        "status": "partial",
        "stage": "skill_sync",
        "binary_replaced": True,
        "skill_sync_status": "failed",
        "skill_sync_command": "notes update",
        "previous_version": "1.3.0",
        "target_version": "1.3.1",
        "current_version": "1.3.1",
        "update_available": False,
        "install_method": "binary",
        "signature_status": "verified",
    }, r.details()
    assert read(box.exe) == b"new binary" and box.skill(0) == "1.3.0"


def test_up_to_date_run_syncs_the_skills(box: Box) -> None:
    write(os.path.join(box.skill_roots[0], "notes", "SKILL.md"), skill_md("1.2.0"))
    box.gh.json(LIST, [release("1.3.0"), release("1.2.0")])
    want = {
        "status": "up_to_date",
        "current_version": "1.3.0",
        "target_version": "1.3.0",
        "update_available": False,
        "install_method": "binary",
        "signature_status": "not_checked",
        "skill_sync_status": "synced",
        "binary_replaced": False,
    }
    r = box.run("update")
    assert r.exit == 0 and r.data() == want, r
    assert box.skill(0) == "1.3.0" and box.gh.downloads() == [] and box.runs == []
    r = box.run("update")
    assert r.data()["skill_sync_status"] == "not_needed", r
    os.remove(os.path.join(box.skill_roots[0], "notes", "SKILL.md"))
    os.rmdir(os.path.join(box.skill_roots[0], "notes"))
    r = box.run("update")
    assert r.data()["skill_sync_status"] == "skipped", r
    # F10: no Skill installed is no reason for the command: update never installs one
    assert "skill_sync_command" not in r.data(), r


def test_runs_from_source_is_unsupported(box: Box) -> None:
    box.method = None  # the default detection: this checkout is neither frozen nor installed
    r = box.run("update")
    assert r.exit == 2 and r.code() == "E_UNSUPPORTED", r
    assert r.env["error"]["message"] == (
        "This copy of notes runs from source, so it cannot update itself."
    )
    assert r.details() == {
        "dispatch_state": "not_started",
        "next_action": "Update the checkout with git, or install a release build.",
        "stage": "discover",
        "current_version": "1.3.0",
        "binary_replaced": False,
        "skill_sync_status": "skipped",
    }, r.details()
    assert box.gh.requests == []


def test_install_method_rules() -> None:
    f = U.install_method_for
    assert f("/home/me/src/notes/notes/_aicli/update.py", False) == "source"
    assert f("/venv/lib/python3.12/site-packages/notes/_aicli/update.py", False) == "pip"
    assert f("/usr/lib/python3/dist-packages/notes/_aicli/update.py", False) == "pip"
    assert f("/opt/homebrew/Cellar/notes/1.0/libexec/lib/site-packages/notes/x.py", False) == "brew"
    assert f("/usr/lib/node_modules/notes/node_modules/notes-linux-x64/bin/notes", True) == "npm"
    assert f("/home/me/.pnpm/node_modules/.pnpm/notes@1/node_modules/notes/bin/notes", True) == (
        "pnpm"
    )
    assert f("/home/me/.config/yarn/global/node_modules/notes-cli/bin/notes", True) == "yarn"
    assert f(r"C:\Users\me\AppData\Roaming\npm\node_modules\notes-cli\bin\notes.exe", True) == (
        "npm"
    )
    assert f("/usr/local/Cellar/notes/1.0/bin/notes", True) == "brew"
    assert f("/usr/local/bin/notes", True) == "binary"


def test_confirm_is_a_usage_error(box: Box) -> None:
    r = box.run("update", "--confirm", "ct2_x")
    assert r.exit == 2 and r.code() == "E_USAGE", r
    assert r.env["error"]["message"] == (
        "update takes no --confirm token; run `notes update`, or `notes update --check` to only "
        "look."
    )
    d = r.details()
    assert d["parameter"] == "--confirm" and d["problem"] == "invalid_value", d
    assert d["next_action"] == "Run `notes update`, or `notes update --check` to only look."
    assert d["stage"] == "discover" and d["skill_sync_status"] == "skipped", d
    assert box.gh.requests == []


def test_interrupt_before_the_swap(box: Box) -> None:
    box.gh.json(LIST, [publish(box.gh, "1.3.1")])
    box.verify_error = KeyboardInterrupt()
    r = box.run("update")
    assert r.exit == 130 and r.code() == "E_INTERRUPTED" and r.env["error"]["retryable"], r
    assert r.env["error"]["message"] == (
        "The update was interrupted before anything was installed; notes is still 1.3.0."
    )
    assert r.details()["stage"] == "verify_signature", r.details()
    assert read(box.exe) == b"old binary" and box.leftovers() == []


def test_trust_root_and_missing_sigstore(box: Box, monkeypatch: pytest.MonkeyPatch) -> None:
    box.gh.json(LIST, [publish(box.gh, "1.3.1")])
    box.verify_error = aicli.TrustRootUnavailable("TUF refresh failed")
    r = box.run("update")
    assert r.exit == 7 and r.code() == "E_NETWORK" and r.env["error"]["retryable"], r
    assert r.env["error"]["message"] == (
        "Could not load the Sigstore trust root, so the release signature could not be checked; "
        "nothing was installed."
    )
    assert r.details()["stage"] == "verify_signature"
    # The default verifier without sigstore installed fails closed.
    for mod in ("sigstore", "sigstore.models", "sigstore.verify", "sigstore.verify.policy"):
        monkeypatch.setitem(sys.modules, mod, None)
    box.cfg["verify"] = None
    r = box.run("update")
    assert r.code() == "E_INTEGRITY" and r.details()["stage"] == "verify_signature", r
    assert r.env["error"]["message"] == (
        "This build of notes cannot verify release signatures, so it will not install a release."
    )
    assert r.details()["next_action"] == (
        "Install notes with the package-manager command in its README."
    )


def test_verify_sigstore_bundle_maps_a_trust_root_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    verify = pytest.importorskip("sigstore.verify")

    def offline(cls: Any, *, offline: bool = False) -> Any:
        raise OSError("no route to tuf-repo-cdn.sigstore.dev")

    monkeypatch.setattr(verify.Verifier, "production", classmethod(offline))
    with pytest.raises(aicli.TrustRootUnavailable):
        aicli.verify_sigstore_bundle(b"x", b"{}", IDENTITY, ISSUER)


def test_unsafe_archives_are_refused(tmp_path: Any) -> None:
    def tar(name: str, kind: bytes = tarfile.REGTYPE) -> str:
        p = str(tmp_path / f"a{len(os.listdir(tmp_path))}.tar.gz")
        with tarfile.open(p, "w:gz") as tf:
            info = tarfile.TarInfo(name)
            info.type = kind
            if kind == tarfile.SYMTYPE:
                info.linkname = "/etc/passwd"
            else:
                info.size = 1
            tf.addfile(info, io.BytesIO(b"x") if kind == tarfile.REGTYPE else None)
        return p

    for p in (tar("../evil"), tar("/abs"), tar("skills/x", tarfile.SYMTYPE)):
        with pytest.raises(U._UnsafeArchive):
            U.extract_release(p, str(tmp_path / "out"), "notes")
    ok = tar("skills/notes/SKILL.md")
    U.extract_release(ok, str(tmp_path / "ok"), "notes")
    assert os.path.isfile(tmp_path / "ok" / "skills" / "notes" / "SKILL.md")


def test_security_notes_make_the_notice_a_warning() -> None:
    cur = aicli.parse_version("1.3.0")
    rels = [(aicli.parse_version("1.3.1"), {"body": "Fixes.\n\n## Security\n- CVE"})]
    assert U.update_severity(cur, aicli.parse_version("1.3.1"), rels) == "warning"
    assert U.update_severity(cur, aicli.parse_version("1.3.2"), [(rels[0][0], {})]) == "info"
    assert U.update_severity(cur, aicli.parse_version("2.0.0"), []) == "warning"
    assert U.checksum_for(b"%s  a.zip\n%s  a.zip\n" % (b"0" * 64, b"1" * 64), "a.zip") is None


def test_tool_defined_update_is_a_tree_error() -> None:
    a = notes_app(Upstream())
    cmd = click.Command("update", callback=lambda: None)
    a.root.add_command(cmd)
    a.schema("own_update", SchemaDef(shape="object", fields=["status"]))
    meta = Meta(summary="Update", effects=[OBSERVE], output="own_update", examples=["notes update"])
    a.register(cmd, meta)
    a.install_builtins()
    assert "`update` is aicli's built-in; set App.update (CLI-070)" in a.check_tree()
    a = notes_app(Upstream())
    a.update = UpdateConfig(repo="not a repo", workflow="release")
    a.install_builtins()
    problems = a.check_tree()
    assert 'App.update.repo "not a repo" is not a GitHub "owner/name"' in problems, problems
    assert 'App.update.workflow "release" is not a workflow file name like release.yml' in (
        problems
    )


def test_doctor_refreshes_the_update_notice(box: Box) -> None:
    box.gh.json(RELEASES + "/latest", release("2.0.0"))
    r = box.run("doctor")
    assert r.exit == 0, r
    notices = r.env["meta"].get("notices") or []
    assert notices and notices[0]["latest_version"] == "2.0.0", r.env["meta"]
    assert notices[0]["severity"] == "warning", notices
    box.gh.routes[RELEASES + "/latest"] = "drop"  # failures are ignored
    assert box.run("doctor").exit == 0


def test_conformance_of_a_tool_with_update_and_settings(box: Box) -> None:
    """R01-R05 hold with the built-in update and settings declared and the suite
    stays offline: the reference validates, doctor skips its notice refresh and reports its
    config check, context carries the profile key, and --profile is bound."""
    from test_conformance import TEST_SAMPLES

    def build(u: Upstream) -> App:
        a = notes_app(u)
        a.update = UpdateConfig(repo=REPO, npm_package=NPM, api_base=box.gh.base)
        a.settings = [aicli.Setting("host", default="https://notes.example")]
        return a

    u = Upstream()
    export = os.path.join(box.root, "export")
    os.makedirs(export)
    h = aicli.Harness(
        new_app=lambda env: build(u),
        writes=u.writes,
        reset=u.reset,
        samples={"note export": ["--output", os.path.join(export, "notes.tsv")], **TEST_SAMPLES},
        scope={
            "scope_id": "scp_conformance",
            "tool": "notes",
            "granted_by": "test",
            "granted_at": "2026-01-01T00:00:00Z",
            "expires_at": None,
            "effects": ["change", "deliver", "artifact", "persist"],
            "dangerous": True,
            "destinations": ["bob@example.com"],
            "outputs": [export],
            "roots": [export],  # the sample's file is overwritten after G04 created it
        },
    )
    aicli.run_conformance(h)
    assert box.gh.requests == [], "the suite never uses the network (doctor's refresh)"


def test_reference_of_a_tool_with_update_validates(box: Box) -> None:
    r = box.run("reference")
    V.validate_reference_full(r.data())
    entry = next(c for c in r.data()["commands"] if c["path"] == "update")
    assert entry["effects"] == ["observe"] and entry["output"] == "aicli_update", entry
    assert entry["summary"] == (
        "Update the tool and its Skills to the latest release, or to --target-version"
    )
    assert entry["examples"] == [
        "notes update --check",
        "notes update",
        "notes update --target-version 2.1.0",
    ], entry
    for code in aicli.contract_gen.UPDATE_ERROR_CODES:
        assert code in entry["error_codes"], entry["error_codes"]
    one = box.run("reference", "--command", "update")
    V.validate_reference_command(one.data())
    assert {p["name"] for p in one.data()["command"]["params"]} == {
        "check",
        "target-version",
        "allow-downgrade",
    }


# ---------------------------------------------------------------- decision 46 (section E)


def executable(path: Any, body: bytes = b"#!/bin/sh\necho planted\n") -> str:
    write(str(path), body)
    if os.name != "nt":
        os.chmod(str(path), 0o755)
    return str(path)


def test_e1_programs_are_never_found_in_the_current_directory(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cloned repository's npm.bat in the current directory never runs: only
    absolute PATH entries are searched, never "", "." or a relative entry."""
    repo = tmp_path / "repo"
    for name in ("npm", "npm.bat", "npm.cmd", "npm.exe"):
        executable(repo / name)
    monkeypatch.chdir(repo)
    relative = os.pathsep.join(["", ".", "repo", "." + os.sep])
    assert U.find_program("npm", relative) is None
    with pytest.raises(FileNotFoundError):
        U._default_run(["npm", "--version"], 5, relative)
    real = executable(tmp_path / "bin" / ("npm.cmd" if os.name == "nt" else "npm"))
    found = U.find_program("npm", os.pathsep.join([".", str(tmp_path / "bin")]))
    assert found is not None and os.path.normcase(found) == os.path.normcase(real), found


def test_e2_prerelease_tags_are_never_installed_implicitly(box: Box) -> None:
    box.h.env["NOTES_DEBUG"] = "1"
    box.gh.json(LIST, [release("2.0.0-rc.1"), release("1.3.1")])  # prerelease flag false
    r = box.run("update", "--check")
    assert r.exit == 0 and r.data()["target_version"] == "1.3.1", r
    assert "debug: update: skipped release tag v2.0.0-rc.1 (a prerelease)\n" in r.stderr
    # the notice refresh skips it too
    box.gh.json(RELEASES + "/latest", release("2.0.0-rc.1"))
    notices = box.run("doctor").env["meta"].get("notices") or []
    assert not any(n.get("latest_version") == "2.0.0-rc.1" for n in notices), notices
    # only --target-version reaches a prerelease
    box.gh.json(RELEASES + "/tags/v2.0.0-rc.1", release("2.0.0-rc.1"))
    r = box.run("update", "--check", "--target-version", "2.0.0-rc.1")
    assert r.exit == 0 and r.data()["target_version"] == "2.0.0-rc.1", r


def skill_md_of(name: str, version: str, note: str = "") -> str:
    return f'---\nname: {name}\nmetadata:\n  version: "{version}"\n---\n{note}\n'


def test_e3_shared_skills_directory_keeps_other_tools(box: Box) -> None:
    """<exe dir>/skills can hold several tools' Skills: replace and sync touch only
    the names this tool's release ships."""
    other = os.path.join(box.bin, "skills", "other-cli", "SKILL.md")
    write(other, skill_md_of("other-cli", "0.9.0", "OLD COPY"))
    user_other = os.path.join(box.skill_roots[0], "other-cli", "SKILL.md")
    write(user_other, skill_md_of("other-cli", "2.0.0", "USER COPY"))
    box.gh.json(LIST, [publish(box.gh, "1.3.1")])
    r = box.run("update")
    assert r.exit == 0 and r.data()["skill_sync_status"] == "synced", r
    assert b"OLD COPY" in read(other), "another tool's Skill beside the binary stays"
    assert b"USER COPY" in read(user_other), "another tool's installed Skill is not synced"
    assert box.skill(0) == "1.3.1"


def domain_skill_md(name: str, version: str, requires: str) -> str:
    return (
        f'---\nname: {name}\nmetadata:\n  version: "{version}"\n'
        f'  requires.skills: "{requires}"\n---\n'
    )


def test_e3_up_to_date_run_syncs_only_this_tools_skills(box: Box) -> None:
    """A run at the current version syncs this tool's Skills only: the entry Skill
    and the domain Skills whose requires.skills names it, whatever their version."""
    write(
        os.path.join(box.bin, "skills", "other-cli", "SKILL.md"), skill_md_of("other-cli", "0.9.0")
    )
    user_other = os.path.join(box.skill_roots[0], "other-cli", "SKILL.md")
    write(user_other, skill_md_of("other-cli", "2.0.0", "USER COPY"))
    write(os.path.join(box.skill_roots[0], "notes", "SKILL.md"), skill_md("1.2.0"))
    for name, requires in (("notes-extra", "notes"), ("other-extra", "other-cli")):
        write(
            os.path.join(box.bin, "skills", name, "SKILL.md"),
            domain_skill_md(name, "1.3.0", requires),
        )
        write(
            os.path.join(box.skill_roots[0], name, "SKILL.md"),
            domain_skill_md(name, "1.2.0", requires),
        )
    box.gh.json(LIST, [release("1.3.0")])
    r = box.run("update")
    assert r.exit == 0 and r.data()["status"] == "up_to_date", r
    assert r.data()["skill_sync_status"] == "synced", r
    assert box.skill(0) == "1.3.0" and b"USER COPY" in read(user_other)

    def installed(name: str) -> str:
        with open(os.path.join(box.skill_roots[0], name, "SKILL.md"), encoding="utf-8") as fh:
            return aicli.builtins.skill_versions(fh.read())[0]

    assert installed("notes-extra") == "1.3.0", "a domain Skill of this tool is synced"
    assert installed("other-extra") == "1.2.0", "another tool's domain Skill is left alone"


def test_e3_release_without_skills_leaves_the_directory_alone(box: Box) -> None:
    osname, arch = U.release_platform()
    name = U.asset_names("notes", "1.3.1", osname, arch)[0]
    buf = io.BytesIO()
    if name.endswith(".zip"):
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(exe_name(), b"new binary")
    else:
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            info = tarfile.TarInfo(exe_name())
            info.size, info.mode = len(b"new binary"), 0o755
            tf.addfile(info, io.BytesIO(b"new binary"))
    data = buf.getvalue()
    box.gh.file(f"/dl/v1.3.1/{name}", data)
    box.gh.file(
        "/dl/v1.3.1/checksums.txt", f"{hashlib.sha256(data).hexdigest()}  {name}\n".encode()
    )
    box.gh.file("/dl/v1.3.1/checksums.txt.sigstore.json", b"{}")
    assets = [name, "checksums.txt", "checksums.txt.sigstore.json"]
    rel = release(
        "1.3.1",
        assets=[
            {"name": n, "browser_download_url": f"{box.gh.base}/dl/v1.3.1/{n}"} for n in assets
        ],
    )
    box.gh.json(LIST, [rel])
    r = box.run("update")
    assert r.exit == 0 and read(box.exe) == b"new binary", r
    assert os.listdir(os.path.join(box.bin, "skills")) == ["notes"], "skills/ is left alone"
    assert box.skill(0) == "1.3.0", "nothing shipped, nothing synced"


def test_e5_allow_downgrade_needs_target_version(box: Box) -> None:
    box.new_version = "1.2.0"
    box.gh.json(LIST, [publish(box.gh, "1.2.0")])
    r = box.run("update", "--allow-downgrade")
    assert r.exit == 0 and r.data()["status"] == "up_to_date", r
    assert read(box.exe) == b"old binary" and box.gh.downloads() == []


def test_e6_pnpm11_store_and_the_installed_tool_found_again(box: Box, tmp_path: Any) -> None:
    f = U.install_method_for
    assert f(
        "/home/me/.local/share/pnpm/store/v11/links/@/notes/1.3.1/ab/node_modules/n/x", True
    ) == ("pnpm")
    assert (
        f(r"C:\Users\me\pnpm\store\v11\links\@\notes\1.3.1\ab\node_modules\n\n.exe", True) == "pnpm"
    )
    shim = executable(tmp_path / "pnpm-home" / ("notes.cmd" if os.name == "nt" else "notes"))
    box.h.env["PATH"] = str(tmp_path / "pnpm-home")
    box.method = "pnpm"
    box.gh.json(LIST, [release("1.3.1")])
    r = box.run("update")
    assert r.exit == 0 and r.data()["status"] == "updated", r
    assert box.runs[0] == ["pnpm", "add", "-g", "notes-cli-example@1.3.1"], box.runs
    assert os.path.normcase(box.runs[1][0]) == os.path.normcase(shim), box.runs
    assert box.runs[1][1:] == ["--version"], box.runs


def test_e7_an_environment_without_pip_is_unsupported(box: Box) -> None:
    box.method = "pip"
    box.cfg["pypi_package"] = "notes-cli-example"

    def no_pip(argv: list[str]) -> tuple[int, bytes]:
        box.runs.append(list(argv))
        return (1, b"No module named pip") if argv[1:] == ["-m", "pip", "--version"] else (0, b"")

    box.cfg["run"] = no_pip
    box.gh.json(LIST, [release("1.3.1")])
    r = box.run("update")
    assert r.exit == 2 and r.code() == "E_UNSUPPORTED", r
    d = r.details()
    assert d["stage"] == "replace" and d["dispatch_state"] == "not_started", d
    assert d["next_action"] == (
        "This Python environment has no pip; upgrade with `pipx upgrade notes-cli-example` or "
        "`uv tool upgrade notes-cli-example`."
    )
    assert box.runs == [[sys.executable, "-m", "pip", "--version"]], box.runs


def test_e8_any_colon_in_an_archive_entry_is_unsafe(tmp_path: Any) -> None:
    for i, name in enumerate(("skills/a:b/SKILL.md", "x:y", "C:evil", "skills/a\x01b")):
        p = str(tmp_path / f"a{i}.tar.gz")
        with tarfile.open(p, "w:gz") as tf:
            info = tarfile.TarInfo(name)
            info.size = 1
            tf.addfile(info, io.BytesIO(b"x"))
        with pytest.raises(U._UnsafeArchive):
            U.extract_release(p, str(tmp_path / f"out{i}"), "notes")


def test_e10_a_failed_rollback_is_reported_truthfully(
    box: Box, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(U, "_IS_WINDOWS", True)  # the swap that moves the running binary aside
    orig = U.rename_retry

    def rename(src: str, dst: str) -> None:
        if dst == box.exe:
            raise PermissionError(13, "Access is denied")
        orig(src, dst)

    monkeypatch.setattr(U, "rename_retry", rename)
    box.gh.json(LIST, [publish(box.gh, "1.3.1")])
    r = box.run("update")
    assert r.exit == 1 and r.code() == "E_IO", r
    d = r.details()
    assert d["stage"] == "replace" and d["dispatch_state"] == "unknown", d
    old = box.exe + ".old"
    assert r.env["error"]["message"] == (
        "The update failed and the old version could not be put back; notes may be missing."
    )
    assert d["next_action"] == f"Rename {old} back to {box.exe}, then run notes --version.", d
    assert read(old) == b"old binary" and not os.path.exists(box.exe)
    kept = [n for n in box.leftovers() if n.startswith(".notes-update-")]
    assert kept, "the new binary is kept, not cleaned up"
    assert read(os.path.join(box.bin, kept[0], "extract", exe_name())) == b"new binary"


def test_p14_update_refuses_a_development_build(box: Box) -> None:
    """update on a development build (FIXES-RC1 P14) is E_UNSUPPORTED at discover,
    with --check too, and contacts nothing; doctor does not compare the Skill with
    it."""
    box.gh.json(LIST, [publish(box.gh, "1.3.1")])
    build = box.h.build

    def dev(u: Upstream) -> App:
        a = build(u)
        a.version = "dev"
        return a

    box.h.build = dev
    for args in (("update",), ("update", "--check")):
        r = box.run(*args)
        assert r.code() == "E_UNSUPPORTED" and r.details()["stage"] == "discover", r
        assert r.details()["current_version"] == "1.2.4-dev", r
        assert r.env["error"]["message"] == (
            "notes 1.2.4-dev is a development build; install a release to update."
        )
        assert r.details()["next_action"] == "Install notes with the install command in its README."
    assert box.gh.requests == [] and read(box.exe) == b"old binary"
    checks = box.run("doctor").data()["checks"]
    version = next(c for c in checks if c["check"] == "version")
    skill = os.path.join(box.skill_roots[0], "notes", "SKILL.md")
    assert version["status"] == "warn" and version["message"] == (
        f"notes 1.2.4-dev is a development build; the Skill at {skill} cannot be compared."
    ), version


def test_e11_an_unusable_ca_file_is_config_at_discover(box: Box) -> None:
    box.h.env["NOTES_CA_FILE"] = os.path.join(box.root, "missing-ca.pem")
    r = box.run("update", "--check")
    assert r.exit == 4 and r.code() == "E_CONFIG" and r.details()["stage"] == "discover", r
    assert box.gh.requests == []


def test_e13_archive_caps_and_repeated_names(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def tar(name: str, entries: list[tuple[str, bytes]]) -> str:
        p = str(tmp_path / name)
        with tarfile.open(p, "w:gz") as tf:
            for n, b in entries:
                info = tarfile.TarInfo(n)
                info.size = len(b)
                tf.addfile(info, io.BytesIO(b))
        return p

    twice = tar(
        "twice.tar.gz", [("skills/notes/SKILL.md", b"a"), ("skills/./notes/SKILL.md", b"b")]
    )
    with pytest.raises(U._UnsafeArchive):
        U.extract_release(twice, str(tmp_path / "o1"), "notes")
    zipped = str(tmp_path / "twice.zip")
    with zipfile.ZipFile(zipped, "w") as zf, pytest.warns(UserWarning):
        zf.writestr("skills/notes/SKILL.md", b"a")
        zf.writestr("skills/notes/SKILL.md", b"b")
    with pytest.raises(U._UnsafeArchive):
        U.extract_release(zipped, str(tmp_path / "o2"), "notes")
    monkeypatch.setattr(U, "MAX_ENTRIES", 3)
    many = tar("many.tar.gz", [(f"f{i}", b"x") for i in range(4)])
    with pytest.raises(U._UnsafeArchive):
        U.extract_release(many, str(tmp_path / "o3"), "notes")
    monkeypatch.setattr(U, "MAX_ENTRIES", 10_000)
    monkeypatch.setattr(U, "MAX_TOTAL", 10)
    big = tar("big.tar.gz", [("skills/notes/SKILL.md", b"x" * 20)])
    with pytest.raises(U._UnsafeArchive):
        U.extract_release(big, str(tmp_path / "o4"), "notes")


def test_e13_downloads_and_redirects_must_be_https(box: Box) -> None:
    """A release asset that is not https is E_UNSUPPORTED at download, before
    anything is downloaded; a redirect to a URL that is not https is E_NETWORK."""
    rel = publish(box.gh, "1.3.1")
    asset = rel["assets"][0]
    asset["browser_download_url"] = "http://" + asset["browser_download_url"].removeprefix(
        "https://"
    )
    box.gh.json(LIST, [rel])
    r = box.run("update")
    assert r.code() == "E_UNSUPPORTED" and r.details()["stage"] == "download", r
    assert r.env["error"]["message"] == (
        "Release v1.3.1 names a download that is not https, so it was not installed."
    )
    assert r.details()["next_action"] == (
        "Install notes with the package-manager command in its README instead, and report the "
        "release to the tool's maintainers."
    )
    assert box.gh.downloads() == [] and read(box.exe) == b"old binary"
    box.gh.routes[LIST] = (302, {"Location": "http://releases.example.invalid/list"}, b"")
    r = box.run("update", "--check")
    assert r.code() == "E_NETWORK" and r.details()["stage"] == "discover", r
    assert r.env["error"]["message"] == (
        "The release source redirected to a URL that is not https, so nothing was downloaded."
    )
    assert r.details()["next_action"] == (
        "Check the network or HTTPS_PROXY (a network's sign-in page redirects like this), then "
        "run `notes update` again."
    )


def test_e13_the_download_deadline(box: Box, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(U, "DOWNLOAD_DEADLINE", -1.0)  # every read is past it
    box.gh.json(LIST, [release("1.3.1")])
    r = box.run("update", "--check")
    assert r.code() == "E_TIMEOUT" and r.env["error"]["message"] == "The release check timed out."


def test_u1_a_plain_text_version_is_read(box: Box) -> None:
    """The post-install check reads a plain-text version (a 1.x release prints
    "gitlab-cli version 1.2.17"); the JSON envelope keeps priority. A binary that
    runs but reports no version is told apart from one that does not run."""
    for out, want in {
        b"gitlab-cli version 1.2.17\n": "1.2.17",
        b"v1.2.17\r\n": "1.2.17",
        b"1.2.17": "1.2.17",
        b"starting\nnotes version 1.3.1-rc.1+b2\n": "1.3.1-rc.1+b2",
        b'notes version 9.9.9\n{"ok":true,"data":{"version":"1.3.1"}}\n': "1.3.1",
        b"notes version 1.3\n": None,
        b"version 01.2.3\n": None,
        b"1.2.17 (abc)\n": None,
        b"": None,
    }.items():
        assert U._reported_version(out) == want, out
    printed = [b""]

    def plain(argv: list[str]) -> tuple[int, bytes]:
        box.runs.append(list(argv))
        return 0, printed[0]

    box.cfg["run"] = plain
    box.gh.json(LIST, [publish(box.gh, "1.3.1")])
    for out, code, message in [
        (
            b"notes version v1.3.2\n",
            "E_INTEGRITY",
            "The downloaded binary reports version 1.3.2, not 1.3.1, so it was not installed.",
        ),
        (
            b"notes (no version)\n",
            "E_IO",
            "The downloaded binary did not report its version, so it was not installed.",
        ),
    ]:
        printed[0] = out
        r = box.run("update")
        assert r.code() == code and r.details()["stage"] == "replace", r
        assert r.env["error"]["message"] == message, r
        assert read(box.exe) == b"old binary"
    assert r.details()["next_action"] == (
        "Install notes with the package-manager command in its README instead."
    ), r
    printed[0] = b"notes version 1.3.1\n"
    r = box.run("update")
    assert r.exit == 0 and r.data()["status"] == "updated", r
    assert read(box.exe) == b"new binary"
