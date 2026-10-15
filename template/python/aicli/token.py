# Vendored from ai-native-cli-spec template/python/aicli.
# Do not edit in tool repos; run scripts/sync-spec.js.
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sean Guo

"""Confirm tokens and operation ids (CLI-055, CLI-058).

Token format: "ct2_" + the 26-char base32 part of the operation id + "_" + 32
lowercase hex chars = the first 16 bytes of
HMAC-SHA256(key, "aicli-confirm/2\\x00" + canonical claims). Only this exact form
is accepted; single use is keyed by the operation id, so no re-encoding can
replay.
"""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import hmac
import os
from dataclasses import dataclass, field
from typing import Any

from ._util import Ordered, canonical_json

TOKEN_PREFIX = "ct2_"
TOKEN_ID_LEN = 26
TOKEN_MAC_LEN = 32
TOKEN_LEN = len(TOKEN_PREFIX) + TOKEN_ID_LEN + 1 + TOKEN_MAC_LEN
MAC_DOMAIN = b"aicli-confirm/2\x00"
TOKEN_LIFETIME = _dt.timedelta(minutes=10)

_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz234567")
_HEX_CHARS = frozenset("0123456789abcdef")


@dataclass
class Claims:
    """What a token binds (BLUEPRINT 5.3). Stored in the operation record, never
    in the token; the token carries only their MAC."""

    v: int = 2
    tool: str = ""
    tool_version: str = ""
    op: str = ""
    cmd: str = ""
    inv: str = ""
    inputs: dict[str, str] = field(default_factory=dict)
    account: str = ""
    host: str = ""
    target: str = ""
    scope: str = ""
    base: str = ""
    idem: str = ""
    exp: str = ""

    def canonical(self) -> bytes:
        """The exact byte string the MAC covers."""
        return canonical_json(
            {
                "v": self.v,
                "tool": self.tool,
                "tool_version": self.tool_version,
                "op": self.op,
                "cmd": self.cmd,
                "inv": self.inv,
                "inputs": dict(self.inputs or {}),
                "account": self.account,
                "host": self.host,
                "target": self.target,
                "scope": self.scope,
                "base": self.base,
                "idem": self.idem,
                "exp": self.exp,
            }
        )

    def mac(self, key: bytes) -> bytes:
        return hmac.new(key, MAC_DOMAIN + self.canonical(), hashlib.sha256).digest()[
            : TOKEN_MAC_LEN // 2
        ]

    def to_json(self) -> Ordered:
        """The claims as the operation record stores them (struct order)."""
        o = Ordered()
        o["v"] = self.v
        o["tool"] = self.tool
        o["tool_version"] = self.tool_version
        o["op"] = self.op
        o["cmd"] = self.cmd
        o["inv"] = self.inv
        o["inputs"] = dict(self.inputs or {})
        o["account"] = self.account
        o["host"] = self.host
        o["target"] = self.target
        o["scope"] = self.scope
        o["base"] = self.base
        o["idem"] = self.idem
        o["exp"] = self.exp
        return o

    @classmethod
    def from_json(cls, v: Any) -> Claims:
        """Claims from a stored record; raises ValueError on a wrong shape."""
        if not isinstance(v, dict):
            raise ValueError("claims are not an object")
        c = cls()
        ver = v.get("v", 0)
        if isinstance(ver, bool) or not isinstance(ver, int):
            raise ValueError("claims.v is not an integer")
        c.v = ver
        for name in (
            "tool",
            "tool_version",
            "op",
            "cmd",
            "inv",
            "account",
            "host",
            "target",
            "scope",
            "base",
            "idem",
            "exp",
        ):
            val = v.get(name, "")
            if val is None:
                val = ""
            if not isinstance(val, str):
                raise ValueError(f"claims.{name} is not a string")
            setattr(c, name, val)
        inputs = v.get("inputs")
        if inputs is None:
            inputs = {}
        if not isinstance(inputs, dict) or not all(isinstance(x, str) for x in inputs.values()):
            raise ValueError("claims.inputs is not a map of strings")
        c.inputs = dict(inputs)
        return c


def new_operation_id(batch: bool = False) -> str:
    """ "op_" (or "opb_" for a batch) + 26 lowercase base32 chars of 16 random bytes."""
    s = base64.b32encode(os.urandom(16)).decode("ascii").rstrip("=").lower()
    return ("opb_" if batch else "op_") + s


def id_part(op_id: str) -> str:
    """The id without its op_/opb_ prefix."""
    i = op_id.find("_")
    return op_id[i + 1 :] if i >= 0 else op_id


def is_id_part(s: str) -> bool:
    return len(s) == TOKEN_ID_LEN and all(c in _ID_CHARS for c in s)


def valid_operation_id(op_id: Any) -> bool:
    """Whether op_id has the exact op_/opb_ format."""
    if not isinstance(op_id, str):
        return False
    if op_id.startswith("op_"):
        return is_id_part(op_id[3:])
    if op_id.startswith("opb_"):
        return is_id_part(op_id[4:])
    return False


def mint_token(key: bytes, c: Claims) -> str:
    """The confirm token for claims."""
    return TOKEN_PREFIX + id_part(c.op) + "_" + c.mac(key).hex()


def parse_token(s: Any) -> tuple[str, str] | None:
    """The id part and MAC of a token in its canonical form, else None. Upper
    case, padding, whitespace, extra or missing characters are all rejected."""
    if not isinstance(s, str) or len(s) != TOKEN_LEN or not s.startswith(TOKEN_PREFIX):
        return None
    if s[len(TOKEN_PREFIX) + TOKEN_ID_LEN] != "_":
        return None
    part = s[len(TOKEN_PREFIX) : len(TOKEN_PREFIX) + TOKEN_ID_LEN]
    mac_hex = s[len(TOKEN_PREFIX) + TOKEN_ID_LEN + 1 :]
    if not is_id_part(part) or not all(c in _HEX_CHARS for c in mac_hex):
        return None
    return part, mac_hex


def verify_mac(key: bytes, c: Claims, mac_hex: str) -> bool:
    """Check a token MAC against stored claims in constant time."""
    try:
        got = bytes.fromhex(mac_hex)
    except ValueError:
        return False
    if len(got) != TOKEN_MAC_LEN // 2:
        return False
    try:
        want = c.mac(key)
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(got, want)
