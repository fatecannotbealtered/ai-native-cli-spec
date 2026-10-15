"""contract_gen.py against contract.json (port of contract_test.go): gen-contract.js
--check guards the bytes, this guards the meaning."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aicli import contract_gen as cg
from aicli.token import TOKEN_LIFETIME

CONTRACT = Path(__file__).resolve().parents[3] / "common" / "contract" / "contract.json"


@pytest.fixture(scope="module")
def contract() -> dict[str, Any]:
    if not CONTRACT.exists():
        pytest.skip("contract.json not available here")
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def test_generated_contract_matches_contract_json(contract: dict[str, Any]) -> None:
    assert contract["spec_version"] == cg.SPEC_VERSION
    assert contract["envelope"]["schema_version_value"] == cg.SCHEMA_VERSION
    core = contract["error_codes"]["core"]
    assert set(core) == set(cg.CORE_CODES)
    for name, spec in core.items():
        got = cg.CORE_CODES[name]
        assert (got.exit, got.retryable) == (spec["exit"], spec["retryable"]), name
    assert sorted(contract["error_details"]["problem"]) == sorted(cg.PROBLEM_VALUES)
    assert sorted(contract["error_details"]["conflict"]) == sorted(cg.CONFLICT_VALUES)
    assert sorted(contract["control_flags"]["excluded_from_binding"]) == sorted(
        cg.EXCLUDED_FROM_BINDING
    )
    assert contract["token"]["format"] == cg.TOKEN_PATTERN
    assert TOKEN_LIFETIME.total_seconds() <= contract["token"]["max_ttl_seconds"]


def test_contract_values_the_library_relies_on(contract: dict[str, Any]) -> None:
    # decision 14: upstream_conflict; decision 19: windows-unverified
    assert "upstream_conflict" in cg.CONFLICT_VALUES
    assert "windows-unverified" in cg.STATE_PROTECTION_VALUES
    assert len(contract["receipt_rules"]["rules"]) == cg.RECEIPT_RULE_COUNT
    assert cg.HTTP_STATUS_MAP["connection_failure_before_send"] == "E_NETWORK"
    assert cg.ERROR_BUDGET_CHARS == 2000 and cg.SUCCESS_BUDGET_CHARS == 8000
