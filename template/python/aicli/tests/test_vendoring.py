"""The library works vendored as <pkg>/_aicli (template/common/scripts/spec-files.js):
only relative imports, no tests or caches shipped, and a tool built on the copy runs
in a fresh interpreter that cannot see this checkout."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[1]
EXCLUDED_DIRS = {"testdata", "tests", "test", "__pycache__", "build", "dist", "node_modules"}
EXCLUDED_FILES = {
    "MANIFEST.json",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "conftest.py",
}


def library_files() -> list[str]:
    """The files spec-files.js isLibraryFile() vendors."""
    out = []
    for root, dirs, files in os.walk(LIB):
        rel_root = Path(root).relative_to(LIB)
        dirs[:] = [
            d
            for d in dirs
            if not d.startswith(".") and d not in EXCLUDED_DIRS and not d.endswith(".egg-info")
        ]
        for name in files:
            rel = (rel_root / name).as_posix()
            if any(part.startswith(".") for part in rel.split("/")) or name in EXCLUDED_FILES:
                continue
            if (
                re.fullmatch(r"test_.*\.py", name)
                or name.endswith("_test.py")
                or re.search(r"\.py[cod]$", name)
            ):
                continue
            out.append(rel)
    return sorted(out)


def test_vendored_file_set() -> None:
    files = library_files()
    assert "__init__.py" in files and "contract_gen.py" in files and "LICENSE" in files, files
    assert not [f for f in files if f.startswith("tests/")], files
    for rel in files:
        if not rel.endswith(".py"):
            continue
        text = (LIB / rel).read_text(encoding="utf-8")
        assert text.startswith("# Vendored from ai-native-cli-spec template/python/aicli.\n"), rel
        assert "# SPDX-License-Identifier: MIT\n" in text.split('"""', 1)[0], rel
        # relative imports only: the copy lives at <pkg>/_aicli
        assert not re.search(r"^\s*(?:from|import) aicli\b", text, re.MULTILINE), rel


TOOL = """
import io, json, os, sys
import click
from ._aicli import App, Meta, OBSERVE, SchemaDef

root = click.Group("demo")
app = App(name="demo", version="0.1.0", risk_tier="T0", root=root, skill_dirs=[])
app.schema("thing", SchemaDef(shape="object", fields=["id", "name"]))

@root.command("show")
@click.option("--id", "tid", help="Thing id")
def show(tid):
    app.ok(None, {"id": tid or "1", "name": "first"})

app.register(show, Meta(summary="Show a thing", effects=[OBSERVE], output="thing", positional="id",
                        examples=["demo show 1"]))
"""

RUN = """
import io, json, os, sys
sys.path.insert(0, sys.argv[1])
assert "aicli" not in sys.modules
from mytool.cli import app
out = io.BytesIO()
app.stdout = app.stderr = out
app.state_dir = sys.argv[2]
code = app.execute(["show", "7"])
env = json.loads(out.getvalue())
assert code == 0 and env["data"] == {"id": "7", "name": "first"}, out.getvalue()
from mytool._aicli import validate_envelope
validate_envelope(out.getvalue())
try:
    import aicli  # the checkout's copy must not be reachable
except ImportError:
    print("ok")
else:
    print("aicli importable from", aicli.__file__)
"""


@pytest.mark.slow
def test_vendored_copy_runs_on_its_own(tmp_path: Path) -> None:
    pkg = tmp_path / "src" / "mytool"
    for rel in library_files():
        dst = pkg / "_aicli" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(LIB / rel, dst)
    (pkg / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
    (pkg / "cli.py").write_text(TOOL, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    out = subprocess.run(
        [sys.executable, "-I", "-c", RUN, str(tmp_path / "src"), str(tmp_path / "state")],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=env,
        timeout=120,
        check=False,
    )
    assert out.returncode == 0 and out.stdout.strip() == "ok", (out.stdout, out.stderr)
