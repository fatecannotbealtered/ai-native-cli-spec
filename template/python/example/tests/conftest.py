"""pytest setup for the notes example: the library (template/python) and the example
package (template/python/example) go on sys.path; every test records the commands
it runs into one coverage file (NOTES_COVERAGE_FILE, VER-003) for the R06 check at
the end of test_notes.py."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
for _p in (str(_HERE.parent), str(_HERE.parents[2]), str(_HERE.parents[1])):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from notes_coverage import (  # noqa: E402 - after the path setup
    COVERAGE_DIR,
    COVERAGE_FILE,
    ENVELOPES,
    STREAMS,
)


@pytest.fixture(autouse=True)
def _record_coverage(monkeypatch):  # noqa: ANN001, ANN202 - pytest fixture
    monkeypatch.setenv("NOTES_COVERAGE_FILE", COVERAGE_FILE)
    # keep what the conformance suite validates (R04) for the dump as well
    from aicli import conformance

    env, stream = conformance.validate_envelope, conformance.validate_stream

    def record_env(raw):  # noqa: ANN001, ANN202
        ENVELOPES.append(bytes(raw))
        return env(raw)

    def record_stream(raw):  # noqa: ANN001, ANN202
        STREAMS.append(bytes(raw))
        return stream(raw)

    monkeypatch.setattr(conformance, "validate_envelope", record_env)
    monkeypatch.setattr(conformance, "validate_stream", record_stream)


def pytest_configure(config):  # noqa: ANN001, ANN201 - pytest hook
    config.addinivalue_line("markers", "slow: spawns processes")


def pytest_sessionfinish(session, exitstatus):  # noqa: ANN001, ANN201 - pytest hook
    out = os.environ.get("AICLI_DUMP_ENVELOPES")
    if not out or not (ENVELOPES or STREAMS):
        return
    d = Path(out) / "example"
    d.mkdir(parents=True, exist_ok=True)
    for i, raw in enumerate(ENVELOPES):
        (d / f"envelope-{i:05d}.json").write_bytes(raw)
    for i, raw in enumerate(STREAMS):
        (d / f"stream-{i:05d}.ndjson").write_bytes(raw)


def pytest_unconfigure(config):  # noqa: ANN001, ANN201 - pytest hook
    shutil.rmtree(COVERAGE_DIR, ignore_errors=True)
