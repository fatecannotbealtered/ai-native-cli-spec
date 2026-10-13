"""Tokens, operation ids and canonical JSON (port of token_test.go), with the Go
library's known answers: the same inputs must give the same bytes in both
libraries."""

from __future__ import annotations

import re

from aicli._util import (
    Ordered,
    RawNumber,
    canonical_json,
    claim_string,
    go_float,
    go_json,
)
from aicli.binding import Binding, secret_digest
from aicli.contract_gen import BATCH_ID_PATTERN, OPERATION_ID_PATTERN, TOKEN_PATTERN
from aicli.envelope import Notice
from aicli.gate import digest_list
from aicli.journal import Record, RecordError, Resolution, TargetRef
from aicli.token import (
    TOKEN_LEN,
    Claims,
    id_part,
    mint_token,
    new_operation_id,
    parse_token,
    valid_operation_id,
    verify_mac,
)


def claims() -> Claims:
    return Claims(
        v=2,
        tool="notes",
        tool_version="1.2.3",
        op="op_aaaaaaaaaaaaaaaaaaaaaaaaaa",
        cmd="note update",
        inv="sha256:00",
        inputs={"id": "sha256:01", "title": "sha256:02"},
        account="tester",
        host="https://notes.example",
        target="notes/1",
        scope="sha256:03",
        base="4",
        idem="",
        exp="2026-10-12T08:10:00Z",
    )


def key() -> bytes:
    return bytes(range(32))


def test_operation_ids_match_the_contract() -> None:
    single, batch = re.compile(OPERATION_ID_PATTERN), re.compile(BATCH_ID_PATTERN)
    seen = set()
    for _ in range(200):
        op, bop = new_operation_id(False), new_operation_id(True)
        assert single.fullmatch(op) and valid_operation_id(op), op
        assert batch.fullmatch(bop) and valid_operation_id(bop), bop
        assert op not in seen and bop not in seen
        seen.update((op, bop))
    for bad in [
        "",
        "op_",
        "op_AAAAAAAAAAAAAAAAAAAAAAAAAA",
        "op_aaaaaaaaaaaaaaaaaaaaaaaaa",
        "op_aaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "op_aaaaaaaaaaaaaaaaaaaaaaaaa1",
        "op_../../../../etc/passwd/aa",
        "opx_aaaaaaaaaaaaaaaaaaaaaaaaaa",
        "op_aaaaaaaaaaaaaaaaaaaaaaaaa=",
    ]:
        assert not valid_operation_id(bad), bad


def test_token_round_trip_and_format() -> None:
    c = claims()
    tok = mint_token(key(), c)
    assert re.fullmatch(TOKEN_PATTERN, tok)
    parsed = parse_token(tok)
    assert parsed is not None
    part, mac = parsed
    assert part == id_part(c.op) and verify_mac(key(), c, mac)


def test_token_known_answer() -> None:
    """The Go library's TestTokenKnownAnswer: same claims, same bytes, same token."""
    body = claims().canonical()
    want = (
        '{"account":"tester","base":"4","cmd":"note '
        'update","exp":"2026-10-12T08:10:00Z","host":"https://notes.example",'
        '"idem":"","inputs":{"id":"sha256:01","title":"sha256:02"},"inv":"sha256:00",'
        '"op":"op_aaaaaaaaaaaaaaaaaaaaaaaaaa",'
        '"scope":"sha256:03","target":"notes/1","tool":"notes","tool_version":"1.2.3","v":2}'
    )
    assert body.decode("utf-8") == want
    tok = mint_token(key(), claims())
    assert len(tok) == TOKEN_LEN
    assert tok == "ct2_aaaaaaaaaaaaaaaaaaaaaaaaaa_89fccd712b36dd3fa335f0af9c8efb1b"


def test_parse_token_accepts_only_the_canonical_form() -> None:
    tok = mint_token(key(), claims())
    assert parse_token(tok) is not None
    bad = {
        "upper case": tok.upper(),
        "upper-case id": tok[:4] + tok[4:30].upper() + tok[30:],
        "upper-case mac": tok[:31] + tok[31:].upper(),
        "padding": tok + "=",
        "double padding": tok + "==",
        "truncated": tok[:-1],
        "extra hex": tok + "a",
        "leading space": " " + tok,
        "trailing space": tok + " ",
        "trailing newline": tok + "\n",
        "no prefix": tok[4:],
        "old prefix": "ct_" + tok[4:],
        "v1 prefix": "ct1_" + tok[4:],
        "dash separator": tok[:30] + "-" + tok[31:],
        "base32 digit 0": tok[:4] + "0" + tok[5:],
        "base32 digit 1": tok[:4] + "1" + tok[5:],
        "base32 digit 8": tok[:4] + "8" + tok[5:],
        "non-hex mac": tok[:31] + "g" + tok[32:],
        "empty": "",
        "base64 style": "ct2_" + "A" * 26 + "_" + "0" * 32,
        "op id in token": "ct2_op_" + tok[4:],
        "unicode lookalike": tok.replace("a", "\u0430", 1),
        "url-encoded": tok.replace("_", "%5F", 1),
        "full-width digit": tok[:31] + "\uff10" + tok[32:],
        "null byte appended": tok + "\x00",
    }
    for name, s in bad.items():
        assert parse_token(s) is None, name


def test_token_mac_binds_every_claim() -> None:
    k = key()
    tok = mint_token(k, claims())
    parsed = parse_token(tok)
    assert parsed is not None
    mac = parsed[1]
    mutations = {
        "v": ("v", 3),
        "tool": ("tool", "other"),
        "tool_version": ("tool_version", "1.2.4"),
        "op": ("op", "op_bbbbbbbbbbbbbbbbbbbbbbbbbb"),
        "cmd": ("cmd", "note delete"),
        "inv": ("inv", "sha256:99"),
        "inputs value": ("inputs", {"id": "sha256:01", "title": "sha256:09"}),
        "inputs key": ("inputs", {"id": "sha256:01", "body": "sha256:02"}),
        "inputs extra": ("inputs", {"id": "sha256:01", "title": "sha256:02", "tag": "sha256:0a"}),
        "account": ("account", "mallory"),
        "host": ("host", "https://evil.example"),
        "target": ("target", "notes/2"),
        "scope": ("scope", ""),
        "base": ("base", "5"),
        "idem": ("idem", "k"),
        "exp": ("exp", "2026-10-12T09:10:00Z"),
    }
    for name, (attr, value) in mutations.items():
        c = claims()
        setattr(c, attr, value)
        assert not verify_mac(k, c, mac), name
    other = bytearray(k)
    other[0] ^= 1
    assert not verify_mac(bytes(other), claims(), mac)
    assert not verify_mac(k, claims(), mac[:30])
    assert not verify_mac(k, claims(), mac + "00")
    assert not verify_mac(k, claims(), "zz" + mac[2:])


def test_canonical_json() -> None:
    got = canonical_json(
        {"b": 'x"y\\z\n\x01\u00e9<>&', "a": ["2", "1"], "c": {"z": "1", "y": "2"}, "d": 2}
    )
    assert (
        got.decode("utf-8")
        == '{"a":["2","1"],"b":"x\\"y\\\\z\\u000a\\u0001\u00e9<>&","c":{"y":"2","z":"1"},"d":2}'
    )
    import pytest

    with pytest.raises(ValueError):
        canonical_json("\udcff")
    with pytest.raises(TypeError):
        canonical_json(1.5)
    assert claim_string("\udcff") != claim_string("\udcfe")
    assert claim_string("\udcff").startswith("bytes-sha256:")


# ---------------------------------------------------------------- known answers from the Go library

GO_RECORD = (
    '{"v":2,"operation_id":"op_aaaaaaaaaaaaaaaaaaaaaaaaaa","kind":"single","batch_id":null,'
    '"command":"note update",'
    '"status":"planned","target":{"key":"notes/1","kind":"note"},"effects":["change"],'
    '"dangerous":false,'
    '"scope_id":"scp_test","created_at":"2026-10-12T08:00:00Z","expires_at":"2026-10-12T08:10:00Z",'
    '"dispatched_at":null,"lease_until":null,"finished_at":null,"claims":{"v":2,"tool":"notes",'
    '"tool_version":"1.2.3",'
    '"op":"op_aaaaaaaaaaaaaaaaaaaaaaaaaa","cmd":"note '
    'update","inv":"sha256:00","inputs":{"id":"sha256:01",'
    '"title":"sha256:02"},"account":"tester","host":"https://notes.example","target":"notes/1",'
    '"scope":"sha256:03",'
    '"base":"4","idem":"","exp":"2026-10-12T08:10:00Z"},"preview":{"a":[1,2.50],"u":" '
    '\u00e9\\u2028",'
    '"z":"\\u003cb\\u003e\\u0026\\u003c/b\\u003e"},"receipt":null,"error":null,"resolution":null,'
    '"items":null}'
)

GO_RECORD2 = (
    '{"v":2,"operation_id":"opb_aaaaaaaaaaaaaaaaaaaaaaaaaa","kind":"batch","batch_id":null,'
    '"command":"note bulk-tag",'
    '"status":"applied","target":null,"effects":["change"],"dangerous":false,"scope_id":null,'
    '"created_at":"2026-10-12T08:00:00Z","expires_at":"2026-10-12T08:10:00Z","dispatched_at":null,'
    '"lease_until":null,"finished_at":null,"claims":null,"preview":null,"receipt":null,'
    '"error":{"code":"E_PARTIAL","message":"m"},"resolution":{"outcome":"applied","evidence":"e",'
    '"resolved_at":"2026-10-12T08:20:00Z"},"items":["op_bbbbbbbbbbbbbbbbbbbbbbbbbb"],'
    '"replay_of":"op_cccccccccccccccccccccccccc"}'
)


def test_go_known_answers_for_digests() -> None:
    k = key()
    assert secret_digest(k, "token", b"s3cret") == (
        "hmac-sha256:05b577b79b8c459a365016943f2a9b38cab1c632d7cef83521b819fb75f19ad2"
    )
    assert secret_digest(k, "t\u00f6k", "v\u00e4lue\x00".encode()) == (
        "hmac-sha256:f6727f28974cf25ebdfaa9002481bc619970c2fd5ef03aa2bb3d0e709dfa1e23"
    )
    canon = canonical_json(
        {
            "b": 'x"y\\z\n\x01\u00e9<>& \x7f\u2028',
            "a": ["2", "1"],
            "c": {"z": "1", "y": "2"},
            "d": 2,
            "e": None,
            "f": True,
        }
    )
    assert canon.decode("utf-8") == (
        '{"a":["2","1"],"b":"x\\"y\\\\z\\u000a\\u0001\u00e9<>& '
        '\x7f\u2028","c":{"y":"2","z":"1"},"d":2,"e":null,"f":true}'
    )
    assert digest_list(["notes/1", "notes/2"]) == (
        "sha256:d3e88e473668206fa69a0542cf5b9deba7a970c9086676dbd9661cdc10fe5143"
    )
    assert claim_string("\udcff\udcfe") == (
        "bytes-sha256:b3d510ef04275ca8e698e5b3cbb0ece3949ef9252f0cdc839e9ee347409a2209"
    )
    b = Binding(command="x")
    b.add("title", "New", "New", "--title")
    b.add("id", "1", "1", "--id")
    b.add("tag", '["a","b"]', None, "--tag")
    assert canonical_json(b.digests).decode() == (
        '{"id":"sha256:6b86b273ff34fce19d6b804eff5a3f5747ada4eaa22f1d49c01e52ddb7875b4b",'
        '"tag":"sha256:0473ef2dc0d324ab659d3580c1134e9d812035905c4781fdd6d529b0c6860e13",'
        '"title":"sha256:18fdd549b2ed367ac0c74cbec1214644728515b30edbcb78e7d322757a7c8359"}'
    )
    import hashlib

    assert "sha256:" + hashlib.sha256(canonical_json(b.digests)).hexdigest() == (
        "sha256:92f39b43ec12b217f8f3d4097d2dd77fb406cf8ac1e67ce9ea8ab077d51eee84"
    )


def test_go_known_answers_for_records() -> None:
    r = Record(
        operation_id="op_aaaaaaaaaaaaaaaaaaaaaaaaaa",
        kind="single",
        command="note update",
        status="planned",
        target=TargetRef("notes/1", "note"),
        effects=["change"],
        scope_id="scp_test",
        created_at="2026-10-12T08:00:00Z",
        expires_at="2026-10-12T08:10:00Z",
        claims=claims(),
        preview={"z": "<b>&</b>", "a": [RawNumber("1"), RawNumber("2.50")], "u": " \u00e9\u2028"},
    )
    assert r.encode().decode("utf-8") == GO_RECORD + "\n"
    r2 = Record(
        operation_id="opb_aaaaaaaaaaaaaaaaaaaaaaaaaa",
        kind="batch",
        command="note bulk-tag",
        status="applied",
        effects=["change"],
        created_at="2026-10-12T08:00:00Z",
        expires_at="2026-10-12T08:10:00Z",
        items=["op_bbbbbbbbbbbbbbbbbbbbbbbbbb"],
        error=RecordError("E_PARTIAL", "m"),
        resolution=Resolution("applied", "e", "2026-10-12T08:20:00Z"),
        replay_of="op_cccccccccccccccccccccccccc",
    )
    assert r2.encode().decode("utf-8") == GO_RECORD2 + "\n"
    # a stored record decodes and re-encodes to the same bytes
    from aicli.journal import decode_record

    assert decode_record(GO_RECORD.encode()).encode().decode("utf-8") == GO_RECORD + "\n"


def test_go_known_answers_for_envelopes() -> None:
    meta = Ordered(
        duration_ms=3, notices=[Notice("t", "info", "m").to_json()], untrusted=["data.a"]
    )
    env = Ordered(
        ok=True,
        schema_version="2.0",
        data={"b": 1.5, "a": "<x> \u2028", "c": RawNumber("1e+21"), "n": None},
        meta=meta,
    )
    assert go_json(env) == (
        '{"ok":true,"schema_version":"2.0","data":{"a":"<x> \\u2028","b":1.5,"c":1e+21,"n":null},'
        '"meta":{"duration_ms":3,"notices":[{"type":"t","severity":"info","message":"m"}],'
        '"untrusted":["data.a"]}}'
    )
    envp = Ordered(
        ok=True,
        schema_version="2.0",
        data={"b": [], "a": {}, "c": ["x", {"k": "v"}]},
        meta=Ordered(duration_ms=3),
    )
    assert go_json(envp, pretty=True) + "\n" == (
        '{\n  "ok": true,\n  "schema_version": "2.0",\n  "data": {\n    "a": {},\n    "b": [],\n   '
        ' "c": [\n      "x",\n      {\n        "k": "v"\n      }\n    ]\n  },\n  "meta": {\n    '
        '"duration_ms": 3\n  }\n}\n'
    )
    floats = {
        1e21: "1e+21",
        1e20: "100000000000000000000",
        123456789.0: "123456789",
        0.000001: "0.000001",
        0.0000001: "1e-7",
        1.5: "1.5",
        100.0: "100",
        1e6: "1000000",
        12345678.9: "12345678.9",
        5e-324: "5e-324",
    }
    for f, want in floats.items():
        assert go_float(f) == want, f
