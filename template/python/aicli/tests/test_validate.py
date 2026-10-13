"""validate_envelope (port of validate_test.go)."""

from __future__ import annotations

import pytest

import aicli
from aicli import ContractViolation, validate_envelope, validate_stream

GOOD = [
    '{"ok":true,"schema_version":"2.0","data":{},"meta":{"duration_ms":0}}',
    '{"ok":true,"schema_version":"2.0","data":{"items":[],"count":0,"has_more":false},'
    '"meta":{"duration_ms":3,"untrusted":["data.items[].title"]}}',
    '{"ok":false,"schema_version":"2.0","error":{"code":"E_NOT_FOUND","message":"Gone.",'
    '"retryable":false,"details":{"dispatch_state":"not_started","next_action":"List '
    'them."}},"meta":{"duration_ms":1}}',
    '{"ok":false,"schema_version":"2.0","error":{"code":"E_X_TOOL","message":"Tool '
    'code.","retryable":false,"details":{"dispatch_state":"not_started","next_action":"Do '
    'x."}},"meta":{"duration_ms":1}}',
    '{"ok":true,"schema_version":"2.0","data":{"id":"5","note_ids":["1"],'
    '"updated_at":"2026-10-12T08:00:00.123Z","closed_at":null,'
    '"error_codes":{"E_USAGE":{"exit":2}}},"meta":{"duration_ms":0}}',
]


@pytest.mark.parametrize("raw", GOOD)
def test_validate_envelope_accepts_library_output(raw: str) -> None:
    validate_envelope((raw + "\n").encode())


OK = '{"ok":true,"schema_version":"2.0","data":{},"meta":{"duration_ms":0}}'
BASE = '"dispatch_state":"not_started","next_action":"n"'
APPLIED = (
    '{"execution":"completed","change":"applied","delivery":"not_applicable",'
    '"persistence":"not_applicable","verification":"passed"}'
)
UNKNOWN = (
    '{"execution":"unknown","change":"unknown","delivery":"not_applicable",'
    '"persistence":"not_applicable","verification":"not_run"}'
)


def err_env(code: str, retry: str, details: str) -> str:
    return (
        '{"ok":false,"schema_version":"2.0","error":{"code":"'
        + code
        + '","message":"m","retryable":'
        + retry
        + ',"details":'
        + details
        + '},"meta":{"duration_ms":0}}'
    )


def receipt(outcome: str) -> str:
    return (
        '{"operation_id":"op_aaaaaaaaaaaaaaaaaaaaaaaaaa","command":"x","target":{"key":"k"},'
        '"effects":["change"],"outcome":' + outcome + ',"observed":{},"replayed":false}'
    )


BAD = {
    "no newline": OK,
    "bom": "\ufeff" + OK + "\n",
    "two documents": OK + "\n" + OK + "\n",
    "crlf": OK + "\r\n",
    "extra key": '{"ok":true,"schema_version":"2.0","data":{},"meta":{"duration_ms":0},'
    '"extra":1}\n',
    "missing meta": '{"ok":true,"schema_version":"2.0","data":{}}\n',
    "old schema": '{"ok":true,"schema_version":"1.0","data":{},"meta":{"duration_ms":0}}\n',
    "meta extra": '{"ok":true,"schema_version":"2.0","data":{},"meta":{"duration_ms":0,"x":1}}\n',
    "negative duration": '{"ok":true,"schema_version":"2.0","data":{},"meta":{"duration_ms":-1}}\n',
    "bad untrusted path": '{"ok":true,"schema_version":"2.0","data":{},"meta":{"duration_ms":0,'
    '"untrusted":["items[].title"]}}\n',
    "duplicate key": '{"ok":true,"ok":true,"schema_version":"2.0","data":{},'
    '"meta":{"duration_ms":0}}\n',
    "error with data": '{"ok":false,"schema_version":"2.0","data":{},"meta":{"duration_ms":0}}\n',
    "retryable mismatch": err_env("E_NETWORK", "false", "{" + BASE + "}") + "\n",
    "no dispatch_state": err_env("E_NOT_FOUND", "false", '{"next_action":"n"}') + "\n",
    "bad dispatch_state": err_env(
        "E_NOT_FOUND", "false", '{"dispatch_state":"maybe","next_action":"n"}'
    )
    + "\n",
    "no next_action": err_env("E_NOT_FOUND", "false", '{"dispatch_state":"not_started"}') + "\n",
    "usage without problem": err_env("E_USAGE", "false", "{" + BASE + "}") + "\n",
    "unknown problem": err_env("E_USAGE", "false", "{" + BASE + ',"problem":"oops"}') + "\n",
    "mismatch no param": err_env(
        "E_CONFLICT", "false", "{" + BASE + ',"conflict":"token_mismatch"}'
    )
    + "\n",
    "unresolved no active": err_env(
        "E_CONFLICT", "false", "{" + BASE + ',"conflict":"unresolved_operation"}'
    )
    + "\n",
    "unknown no lookup": err_env(
        "E_OUTCOME_UNKNOWN",
        "false",
        "{" + BASE + ',"operation_id":"op_aaaaaaaaaaaaaaaaaaaaaaaaaa"}',
    )
    + "\n",
    "confirm no token": err_env(
        "E_CONFIRMATION_REQUIRED", "false", "{" + BASE + ',"problem":"confirmation_required"}'
    )
    + "\n",
    "partial no receipt": err_env("E_PARTIAL", "false", "{" + BASE + "}") + "\n",
    "verify wrong receipt": err_env(
        "E_VERIFY_FAILED", "false", "{" + BASE + ',"receipt":' + receipt(APPLIED) + "}"
    )
    + "\n",
    "human bad action": err_env(
        "E_HUMAN_REQUIRED", "false", "{" + BASE + ',"action":"dance","resume":"x"}'
    )
    + "\n",
    "bad code": err_env("NOT_FOUND", "false", "{" + BASE + "}") + "\n",
    "ok but unknown receipt": '{"ok":true,"schema_version":"2.0",'
    '"data":{"operation_id":"op_aaaaaaaaaaaaaaaaaaaaaaaaaa","receipt":'
    + receipt(UNKNOWN)
    + ',"result":{}},"meta":{"duration_ms":0}}\n',
    "page without continuation": '{"ok":true,"schema_version":"2.0","data":{"items":[],"count":0,'
    '"has_more":true},"meta":{"duration_ms":0}}\n',
    "truncated without reason": '{"ok":true,"schema_version":"2.0","data":{"items":[],"count":0,'
    '"has_more":false,"truncated":true},"meta":{"duration_ms":0}}\n',
    "non-canonical token": '{"ok":true,"schema_version":"2.0","data":{"preview":{},'
    '"confirm_token":"CT2_X","expires_at":"2026-01-01T00:00:00Z",'
    '"operation_id":"op_aaaaaaaaaaaaaaaaaaaaaaaaaa"},"meta":{"duration_ms":0}}\n',
    "over budget": err_env("E_NOT_FOUND", "false", "{" + BASE + ',"x":"' + "y" * 2100 + '"}')
    + "\n",
    "notice bad severity": '{"ok":true,"schema_version":"2.0","data":{},"meta":{"duration_ms":0,'
    '"notices":[{"type":"x","severity":"loud","message":"m"}]}}\n',
    "camel case key": '{"ok":true,"schema_version":"2.0","data":{"createdBy":"x"},'
    '"meta":{"duration_ms":0}}\n',
    "numeric id": '{"ok":true,"schema_version":"2.0","data":{"id":5},"meta":{"duration_ms":0}}\n',
    "numeric ids": '{"ok":true,"schema_version":"2.0","data":{"note_ids":["1",2]},'
    '"meta":{"duration_ms":0}}\n',
    "local time": '{"ok":true,"schema_version":"2.0",'
    '"data":{"updated_at":"2026-10-12T08:00:00+08:00"},"meta":{"duration_ms":0}}\n',
    "one candidate": err_env("E_AMBIGUOUS", "false", "{" + BASE + ',"candidates":[{"ref":"a"}]}')
    + "\n",
    "negative retry": err_env("E_RATE_LIMITED", "true", "{" + BASE + ',"retry_after_ms":-5}')
    + "\n",
    "lookup other operation": err_env(
        "E_OUTCOME_UNKNOWN",
        "false",
        "{"
        + BASE
        + ',"operation_id":"op_aaaaaaaaaaaaaaaaaaaaaaaaaa","lookup":"x operation show --id '
        'op_bbbbbbbbbbbbbbbbbbbbbbbbbb"}',
    )
    + "\n",
    "invalid utf-8": '{"ok":true,"schema_version":"2.0","data":{"a":"\xff"},'
    '"meta":{"duration_ms":0}}\n',
    "not an object": "[]\n",
    "nan": '{"ok":true,"schema_version":"2.0","data":{"a":NaN},"meta":{"duration_ms":0}}\n',
}


@pytest.mark.parametrize("name", sorted(BAD))
def test_validate_envelope_rejects_contract_violations(name: str) -> None:
    raw = BAD[name]
    data = raw.encode("latin-1") if name == "invalid utf-8" else raw.encode("utf-8")
    with pytest.raises(ContractViolation) as info:
        validate_envelope(data)
    assert info.value.problems, name


def test_violations_list_every_problem() -> None:
    raw = '{"ok":true,"schema_version":"1.0","data":{"createdBy":"x"},"meta":{"duration_ms":-1}}\n'
    with pytest.raises(ContractViolation) as info:
        validate_envelope(raw.encode())
    assert len(info.value.problems) >= 2, info.value.problems
    assert isinstance(info.value, ValueError)


def test_validate_stream() -> None:
    line = (
        '{"ok":true,"schema_version":"2.0","type":"item","data":{"id":"1"},'
        '"meta":{"duration_ms":0}}\n'
    )
    end = (
        '{"ok":true,"schema_version":"2.0","type":"summary","data":{"count":1},'
        '"meta":{"duration_ms":1}}\n'
    )
    validate_stream((line + end).encode())
    with pytest.raises(ContractViolation):
        validate_stream(line.replace('"item"', '"row"').encode())
    with pytest.raises(ContractViolation):
        validate_stream(b"")
    assert aicli.validate_stream is validate_stream
