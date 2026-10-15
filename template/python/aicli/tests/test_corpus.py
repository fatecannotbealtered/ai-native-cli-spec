"""The validators against the contract corpus (decision 15): every valid example of
template/common/contract/examples passes and every invalid one fails."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aicli import validate as V
from aicli._util import decode_generic, go_json
from aicli.conformance import over_success_budget

EXAMPLES = Path(__file__).resolve().parents[3] / "common" / "contract" / "examples"


def _index() -> list[dict[str, Any]]:
    p = EXAMPLES / "index.json"
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))["examples"]


def _raw(entry: dict[str, Any]) -> bytes:
    # a Windows checkout may carry CRLF line ends; the examples are LF
    return (EXAMPLES / entry["file"]).read_bytes().replace(b"\r\n", b"\n")


def _compact(raw: bytes) -> bytes:
    """One compact line, as the libraries print (numbers keep their text)."""
    return (go_json(decode_generic(raw), escape_html=False) + "\n").encode("utf-8")


# The library's validators for the corpus schemas that are not whole envelopes or
# built-in outputs: the parts envelopes are made of.
PARTS = {
    "preview": V._preview,
    "receipt": V._receipt,
    "batch": V._batch,
    "page": V._page,
    "notice": V._notice,
    "error-details": V._error_object,
    "changelog": V._changelog,
    "update": V._update,
}

BUILTINS = {
    "reference": V.validate_reference_full,
    "reference-index": V.validate_reference_index,
    "reference-command": V.validate_reference_command,
    "context": V.validate_context,
    "doctor": V.validate_doctor,
    "operation": V.validate_operation_view,
}


def check(schema: str, raw: bytes, parse: bool) -> None:
    """Raise ContractViolation when raw breaks schema."""
    if schema == "scope":
        # The structural check, as the Go corpus test does: the corpus holds paths for
        # any platform, while load_scope checks them against this one.
        v = V._V()
        value = V._parse(raw, v)
        v.raise_if_any()
        V._generic(v, value)
        V._scope_object(v, "scope", value)
        v.raise_if_any()
        return
    if schema == "envelope":
        V.validate_envelope(raw if parse else _compact(raw))
        return
    v = V._V()
    value = V._parse(raw, v)
    v.raise_if_any()
    if schema == "envelope#/$defs/stream_line":
        V._envelope(v, value, stream=True)
    elif schema in BUILTINS:
        BUILTINS[schema](value)
        return
    elif schema in PARTS:
        PARTS[schema](v, "", value)
        V._generic(v, value)
    else:
        raise AssertionError(f"no validator for schema {schema}")
    v.raise_if_any()


def budget_problems(entry: dict[str, Any], raw: bytes) -> list[str]:
    """The success budget of a default invocation, which conformance R04 checks and
    validate_envelope does not (Q24): the corpus's success_budget examples are
    checked with it."""
    if (entry.get("violation") or {}).get("check") != "success_budget":
        return []
    try:
        doc = json.loads(raw)
    except ValueError:
        return []
    size, over = over_success_budget(doc)
    return [f"the response is {size} characters, over the success budget (R04)"] if over else []


ENTRIES = _index()


@pytest.mark.skipif(not ENTRIES, reason="contract examples not available here")
@pytest.mark.parametrize("entry", ENTRIES, ids=[e["file"] for e in ENTRIES])
def test_corpus_example(entry: dict[str, Any]) -> None:
    raw = _raw(entry)
    parse = (entry.get("violation") or {}).get("kind") == "parse"
    if entry["expect"] == "valid":
        check(entry["schema"], raw, parse)
        assert not budget_problems(entry, raw)
        return
    if budget_problems(entry, raw):
        return  # over the success budget of a default invocation (R04, Q24)
    with pytest.raises(V.ContractViolation):
        check(entry["schema"], raw, parse)


@pytest.mark.skipif(not ENTRIES, reason="contract examples not available here")
def test_q24_validate_envelope_leaves_the_success_budget_to_r04() -> None:
    entries = [e for e in ENTRIES if (e.get("violation") or {}).get("check") == "success_budget"]
    assert entries
    for e in entries:
        raw = _compact(_raw(e))
        V.validate_envelope(raw)  # the budget is not this function's (Q24)
        assert budget_problems(e, raw), e["file"]
