"""The validators against the contract corpus (decision 15): every valid example of
template/common/contract/examples passes and every invalid one fails."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aicli import validate as V
from aicli._util import decode_generic, go_json

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
        reason = V.validate_scope_file(raw)
        if reason:
            raise V.ContractViolation([reason])
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


ENTRIES = _index()


@pytest.mark.skipif(not ENTRIES, reason="contract examples not available here")
@pytest.mark.parametrize("entry", ENTRIES, ids=[e["file"] for e in ENTRIES])
def test_corpus_example(entry: dict[str, Any]) -> None:
    raw = _raw(entry)
    parse = (entry.get("violation") or {}).get("kind") == "parse"
    if entry["expect"] == "valid":
        check(entry["schema"], raw, parse)
        return
    with pytest.raises(V.ContractViolation):
        check(entry["schema"], raw, parse)
