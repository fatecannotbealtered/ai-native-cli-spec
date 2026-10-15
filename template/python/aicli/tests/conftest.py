"""pytest setup for the aicli library tests (not vendored into tools).

The library lives next to this directory (template/python/aicli); the tests import
it as the package `aicli`, so its parent directory goes on sys.path.

Every envelope the tests produce is validated with validate_envelope as it is made.
To check them with check-contract.py as well (VER-002), set AICLI_DUMP_ENVELOPES to a
directory: the session writes each envelope there as a .json file and each NDJSON
stream as a .ndjson file."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_PYTHON_DIR = str(Path(__file__).resolve().parents[2])
if _PYTHON_DIR not in sys.path:
    sys.path.insert(0, _PYTHON_DIR)


def pytest_configure(config):  # noqa: ANN001, ANN201 - pytest hook
    config.addinivalue_line("markers", "slow: spawns processes")


def pytest_sessionfinish(session, exitstatus):  # noqa: ANN001, ANN201 - pytest hook
    out = os.environ.get("AICLI_DUMP_ENVELOPES")
    mod = sys.modules.get("aicli_testapp")
    if not out or mod is None:
        return
    d = Path(out) / "library"
    d.mkdir(parents=True, exist_ok=True)
    for i, raw in enumerate(mod.ENVELOPES):
        (d / f"envelope-{i:05d}.json").write_bytes(raw)
    for i, raw in enumerate(mod.STREAMS):
        (d / f"stream-{i:05d}.ndjson").write_bytes(raw)


@pytest.fixture(autouse=True)
def _record_conformance_output(monkeypatch):  # noqa: ANN001, ANN202 - pytest fixture
    """Keep what the conformance suite validates (R04) for the dump as well."""
    import aicli_testapp

    from aicli import conformance

    env, stream = conformance.validate_envelope, conformance.validate_stream

    def record_env(raw):  # noqa: ANN001, ANN202
        aicli_testapp.ENVELOPES.append(bytes(raw))
        return env(raw)

    def record_stream(raw):  # noqa: ANN001, ANN202
        aicli_testapp.STREAMS.append(bytes(raw))
        return stream(raw)

    monkeypatch.setattr(conformance, "validate_envelope", record_env)
    monkeypatch.setattr(conformance, "validate_stream", record_stream)


@pytest.fixture
def h(tmp_path):  # noqa: ANN001, ANN201 - pytest fixture
    """A fresh harness: the test tool over its own upstream, state dir and scope file."""
    from aicli_testapp import THarness

    return THarness(str(tmp_path))
