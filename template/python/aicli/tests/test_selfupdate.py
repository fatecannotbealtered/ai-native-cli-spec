"""Self-update helpers (port of selfupdate_test.go)."""

from __future__ import annotations

import pytest

import aicli
from aicli import (
    Version,
    check_update_target,
    compare_versions,
    package_manager_args,
    parse_version,
)

GOOD = {
    "1.2.3": Version(1, 2, 3),
    "v1.2.3": Version(1, 2, 3),
    "0.0.0": Version(0, 0, 0),
    "1.0.0-rc.1": Version(1, 0, 0, "rc.1"),
    "1.0.0-alpha-1.x+b.42": Version(1, 0, 0, "alpha-1.x", "b.42"),
    "10.20.30": Version(10, 20, 30),
}
BAD = [
    "",
    "1",
    "1.2",
    "1.2.3.4",
    "01.2.3",
    "1.02.3",
    "1.2.03",
    "V1.2.3",
    "vv1.2.3",
    " 1.2.3",
    "1.2.3 ",
    "1.2.3-",
    "1.2.3+",
    "1.2.3-01",
    "1.2.3-a..b",
    "1.0.12&calc",
    "1.2.3|whoami",
    "1.2.3;rm -rf /",
    "1.2.3`x`",
    "1.2.3$(x)",
    "1.2.3\n",
    "1.2.3%PATH%",
    "^1.2.3",
    "~1.2.3",
    "latest",
    "1.2.x",
    "99999999999999999999.0.0",
    "1.2.3-" + "a" * 200,
    "\uff11.2.3",  # a full-width digit
    "1.2.3-\u0430",  # a Cyrillic letter
]


def test_parse_version_is_strict() -> None:
    for s, want in GOOD.items():
        assert parse_version(s) == want, s
    for s in BAD:
        with pytest.raises(ValueError):
            parse_version(s)


def test_compare_versions_follows_semver_precedence() -> None:
    order = [
        "1.0.0-alpha",
        "1.0.0-alpha.1",
        "1.0.0-alpha.beta",
        "1.0.0-beta",
        "1.0.0-beta.2",
        "1.0.0-beta.11",
        "1.0.0-rc.1",
        "1.0.0",
        "1.0.1",
        "1.1.0",
        "2.0.0",
    ]
    for i, a in enumerate(order):
        for j, b in enumerate(order):
            want = (i > j) - (i < j)
            assert compare_versions(parse_version(a), parse_version(b)) == want, (a, b)
    assert compare_versions(parse_version("1.0.0+build.1"), parse_version("1.0.0+build.2")) == 0


CASES = [
    ("npm", "@acme/notes-cli", ["npm", "install", "-g", "@acme/notes-cli@1.4.0"]),
    ("pnpm", "notes-cli", ["pnpm", "add", "-g", "notes-cli@1.4.0"]),
    ("yarn", "notes-cli", ["yarn", "global", "add", "notes-cli@1.4.0"]),
    (
        "go",
        "github.com/acme/notes-cli/cmd/notes",
        ["go", "install", "github.com/acme/notes-cli/cmd/notes@v1.4.0"],
    ),
    ("pip", "notes-cli", ["pip", "install", "--upgrade", "notes-cli==1.4.0"]),
    ("brew", "acme/tap/notes-cli", ["brew", "upgrade", "acme/tap/notes-cli"]),
]


def test_package_manager_args() -> None:
    v = parse_version("v1.4.0+meta")
    for manager, pkg, want in CASES:
        assert package_manager_args(manager, pkg, v) == want, (manager, pkg)
    bad = [
        ("npm", "notes&calc"),
        ("npm", "Notes"),
        ("npm", "--global"),
        ("npm", "notes cli"),
        ("npm", "notes|x"),
        ("go", "notes"),
        ("go", "github.com/acme/../evil"),
        ("go", "github.com/acme/x;rm"),
        ("pip", "notes cli"),
        ("pip", "-r requirements.txt"),
        ("brew", "Notes"),
        ("brew", "x;y"),
        ("apt", "notes"),
        ("", "notes"),
        ("npm", "%PATH%"),
        ("npm", "notes^"),
    ]
    for manager, pkg in bad:
        with pytest.raises(ValueError):
            package_manager_args(manager, pkg, v)
    pre = parse_version("1.4.0-rc.1")
    with pytest.raises(ValueError):
        package_manager_args("pip", "notes-cli", pre)
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@/._~+=-")
    for manager, pkg, _ in CASES:
        if manager == "pip":
            continue
        for a in package_manager_args(manager, pkg, pre):
            assert set(a) <= allowed, a


def test_check_update_target() -> None:
    with pytest.raises(aicli.Error) as info:
        check_update_target("1.2.3", "1.0.12&calc", False)
    assert info.value.code == "E_USAGE"
    with pytest.raises(aicli.Error) as info:
        check_update_target("1.2.3", "1.2.0", False)
    assert info.value.details.get("parameter") == "--target-version"
    assert str(check_update_target("1.2.3", "1.2.0", True)) == "1.2.0"
    assert str(check_update_target("1.2.3", "v1.3.0", False)) == "1.3.0"
    check_update_target("dev", "0.1.0", False)  # a development build accepts any valid target
