# Vendored from ai-native-cli-spec template/python/aicli.
# Do not edit in tool repos; run scripts/sync-spec.js.
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Sean Guo

"""Shared helpers: JSON bytes identical to Go's encoding/json, canonical JSON, times, strings.

The Go library writes envelopes with encoding/json (map keys sorted, struct fields
in declaration order, minimal escaping) and records with json.Marshal (which also
escapes <, > and &). This module reproduces both byte for byte, so a Python tool
and a Go tool emit the same JSON for the same values.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime as _dt
import decimal
import enum
import hashlib
import json
import math
import re
from collections.abc import Mapping
from typing import Any

UTC = _dt.timezone.utc

# ---------------------------------------------------------------- generic values


class Ordered(dict):
    """A JSON object whose keys keep their insertion order (a Go struct)."""


class RawNumber(str):
    """A JSON number kept as its text (Go's json.Number): written verbatim."""


def to_generic(value: Any) -> Any:
    """Convert a Python value to its generic JSON form: dict, list, str, int,
    float, bool or None. Dataclasses become objects of their fields, tuples
    become lists, bytes become base64 text and datetimes RFC 3339 text, as Go's
    encoding/json renders them. Unsupported values raise TypeError, non-finite
    floats ValueError."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, enum.Enum):
        return to_generic(value.value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError("JSON cannot encode NaN or infinity")
        return value
    if isinstance(value, Mapping):
        out = Ordered() if isinstance(value, Ordered) else {}
        for k, v in value.items():
            if isinstance(k, bool) or not isinstance(k, (str, int)):
                raise TypeError(f"JSON object keys must be strings, not {type(k).__name__}")
            out[k if isinstance(k, str) else str(k)] = to_generic(v)
        return out
    if isinstance(value, (list, tuple)):
        return [to_generic(v) for v in value]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_generic(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, (bytes, bytearray)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, _dt.datetime):
        return rfc3339_nano(value)
    if isinstance(value, _dt.date):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} is not JSON serialisable")


def to_generic_data(value: Any) -> Any:
    """to_generic for response data, which is never null."""
    g = to_generic(value)
    return {} if g is None else g


# ---------------------------------------------------------------- Go-compatible JSON

_HEX = "0123456789abcdef"
_SHORT_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _go_string(s: str, escape_html: bool) -> str:
    out = ['"']
    for ch in s:
        c = ord(ch)
        if c < 0x80:
            if ch in _SHORT_ESCAPES:
                out.append(_SHORT_ESCAPES[ch])
            elif c < 0x20:
                out.append("\\u00" + _HEX[c >> 4] + _HEX[c & 0xF])
            elif escape_html and ch in "<>&":
                out.append("\\u00" + _HEX[c >> 4] + _HEX[c & 0xF])
            else:
                out.append(ch)
        elif 0xD800 <= c <= 0xDFFF:
            out.append("\\ufffd")  # Go replaces bytes that are not valid UTF-8
        elif c in (0x2028, 0x2029):
            out.append("\\u202" + _HEX[c & 0xF])
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def go_float(f: float) -> str:
    """A float as Go's encoding/json writes it (ES6 number formatting)."""
    if math.isnan(f) or math.isinf(f):
        raise ValueError("JSON cannot encode NaN or infinity")
    if f == 0:
        return "-0" if math.copysign(1.0, f) < 0 else "0"
    sign, digits, exp = decimal.Decimal(repr(f)).as_tuple()
    ds = list(digits)
    while len(ds) > 1 and ds[-1] == 0:
        ds.pop()
        exp += 1
    text = "".join(str(d) for d in ds)
    n = len(text)
    point = exp + n  # digits before the decimal point
    a = abs(f)
    if a < 1e-6 or a >= 1e21:
        mant = text[0] + ("." + text[1:] if n > 1 else "")
        e = point - 1
        out = f"{mant}e{'-' if e < 0 else '+'}{abs(e):02d}"
        if len(out) >= 4 and out[-4] == "e" and out[-3] == "-" and out[-2] == "0":
            out = out[:-2] + out[-1]
    elif point <= 0:
        out = "0." + "0" * (-point) + text
    elif point >= n:
        out = text + "0" * (point - n)
    else:
        out = text[:point] + "." + text[point:]
    return ("-" if sign else "") + out


def _encode(v: Any, out: list[str], indent: str | None, level: int, escape_html: bool) -> None:
    if v is None:
        out.append("null")
    elif v is True:
        out.append("true")
    elif v is False:
        out.append("false")
    elif isinstance(v, RawNumber):
        out.append(str.__str__(v))
    elif isinstance(v, str):
        out.append(_go_string(v, escape_html))
    elif isinstance(v, int):
        out.append(str(int(v)))
    elif isinstance(v, float):
        out.append(go_float(v))
    elif isinstance(v, Mapping):
        keys = list(v.keys()) if isinstance(v, Ordered) else sorted(v.keys())
        if not keys:
            out.append("{}")
            return
        out.append("{")
        for i, k in enumerate(keys):
            if i:
                out.append(",")
            if indent is not None:
                out.append("\n" + indent * (level + 1))
            out.append(_go_string(k, escape_html))
            out.append(":" if indent is None else ": ")
            _encode(v[k], out, indent, level + 1, escape_html)
        if indent is not None:
            out.append("\n" + indent * level)
        out.append("}")
    elif isinstance(v, (list, tuple)):
        if not v:
            out.append("[]")
            return
        out.append("[")
        for i, item in enumerate(v):
            if i:
                out.append(",")
            if indent is not None:
                out.append("\n" + indent * (level + 1))
            _encode(item, out, indent, level + 1, escape_html)
        if indent is not None:
            out.append("\n" + indent * level)
        out.append("]")
    else:
        _encode(to_generic(v), out, indent, level, escape_html)


def go_json(value: Any, *, pretty: bool = False, escape_html: bool = False) -> str:
    """JSON text as Go writes it: json.Marshal when escape_html, an Encoder with
    SetEscapeHTML(false) (and SetIndent("", "  ") when pretty) otherwise. No
    trailing newline."""
    out: list[str] = []
    _encode(value, out, "  " if pretty else None, 0, escape_html)
    return "".join(out)


def json_equal(a: Any, b: Any) -> bool:
    """Equality of two generic JSON values as Go's reflect.DeepEqual sees them
    after a JSON round trip with json.Number (the number text decides)."""
    try:
        return go_json(to_generic(a)) == go_json(to_generic(b))
    except (TypeError, ValueError):
        return False


class StrictJSONError(ValueError):
    """A document that strict parsing rejects."""


def _reject_constant(name: str) -> Any:
    raise StrictJSONError(f"{name} is not JSON")


def loads_strict(text: str, *, duplicates: bool = False) -> Any:
    """Parse one JSON document; NaN and Infinity are rejected and, with
    duplicates False, a key that appears twice in one object is an error."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        obj: dict[str, Any] = {}
        for k, v in items:
            if k in obj and not duplicates:
                raise StrictJSONError(f"the key {go_quote(truncate_runes(k, 40))} appears twice")
            obj[k] = v
        return obj

    return json.loads(text, object_pairs_hook=pairs, parse_constant=_reject_constant)


def decode_generic(text: str | bytes) -> Any:
    """Decode stored JSON the way the Go library does (json.Decoder.UseNumber):
    numbers keep their text, a repeated key keeps its last value."""
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8")
    return json.loads(
        text, parse_int=RawNumber, parse_float=RawNumber, parse_constant=_reject_constant
    )


# ---------------------------------------------------------------- canonical JSON


def canonical_json(value: Any) -> bytes:
    """Sorted object keys, no whitespace and minimal string escaping: '"' and '\\'
    are escaped, control characters below U+0020 become \\u00xx (lowercase hex),
    everything else is literal UTF-8. Supported values: None, bool, str (valid
    UTF-8 only), int, lists, tuples and objects with string keys. The Go library
    encodes claims the same way (binding.go canonicalJSON)."""
    out: list[str] = []
    _canonical(value, out)
    return "".join(out).encode("utf-8")


def _canonical(v: Any, out: list[str]) -> None:
    if v is None:
        out.append("null")
    elif v is True:
        out.append("true")
    elif v is False:
        out.append("false")
    elif isinstance(v, str):
        _canonical_string(v, out)
    elif isinstance(v, int):
        out.append(str(int(v)))
    elif isinstance(v, (list, tuple)):
        out.append("[")
        for i, item in enumerate(v):
            if i:
                out.append(",")
            _canonical(item, out)
        out.append("]")
    elif isinstance(v, Mapping):
        out.append("{")
        for i, k in enumerate(sorted(v.keys())):
            if not isinstance(k, str):
                raise TypeError("canonical JSON: object keys must be strings")
            if i:
                out.append(",")
            _canonical_string(k, out)
            out.append(":")
            _canonical(v[k], out)
        out.append("}")
    else:
        raise TypeError(f"canonical JSON: unsupported type {type(v).__name__}")


def _canonical_string(s: str, out: list[str]) -> None:
    if not valid_utf8(s):
        raise ValueError("canonical JSON: string is not valid UTF-8")
    out.append('"')
    for ch in s:
        c = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif c < 0x20:
            out.append("\\u00" + _HEX[c >> 4] + _HEX[c & 0xF])
        else:
            out.append(ch)
    out.append('"')


def claim_string(s: str) -> str:
    """Valid UTF-8 as is; anything else as the digest of its raw bytes, so
    distinct inputs never collapse into one canonical value."""
    if valid_utf8(s):
        return s
    return "bytes-sha256:" + sha256_hex(raw_bytes(s))


def claim_strings(values: Any) -> list[str]:
    return [claim_string(s) for s in values]


# ---------------------------------------------------------------- times


def utc(t: _dt.datetime) -> _dt.datetime:
    """t in UTC; a naive time is taken as UTC."""
    if t.tzinfo is None:
        return t.replace(tzinfo=UTC)
    return t.astimezone(UTC)


def time_string(t: _dt.datetime) -> str:
    """t.UTC().Format(time.RFC3339): whole seconds and Z."""
    t = utc(t)
    return f"{t.year:04d}-{t.month:02d}-{t.day:02d}T{t.hour:02d}:{t.minute:02d}:{t.second:02d}Z"


def date_string(t: _dt.datetime) -> str:
    """t.Format("2006-01-02") in UTC."""
    t = utc(t)
    return f"{t.year:04d}-{t.month:02d}-{t.day:02d}"


def rfc3339_nano(t: _dt.datetime) -> str:
    """time.Time's JSON form: RFC 3339 with the fraction trimmed of zeros."""
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    base = f"{t.year:04d}-{t.month:02d}-{t.day:02d}T{t.hour:02d}:{t.minute:02d}:{t.second:02d}"
    if t.microsecond:
        base += ("." + f"{t.microsecond:06d}").rstrip("0")
    off = t.utcoffset() or _dt.timedelta(0)
    if off == _dt.timedelta(0):
        return base + "Z"
    total = int(off.total_seconds() // 60)
    sign = "-" if total < 0 else "+"
    total = abs(total)
    return f"{base}{sign}{total // 60:02d}:{total % 60:02d}"


_RFC3339 = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})[Tt]([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]+))?(?:([Zz])|([+-])([0-9]{2}):([0-9]{2}))"
)


def parse_time(s: Any) -> _dt.datetime | None:
    """time.Parse(time.RFC3339, s) as an aware UTC datetime, or None."""
    if not isinstance(s, str):
        return None
    m = _RFC3339.fullmatch(s)
    if not m:
        return None
    year, month, day, hour, minute, second = (int(m.group(i)) for i in range(1, 7))
    frac = m.group(7) or ""
    micro = int((frac[:6]).ljust(6, "0")) if frac else 0
    if hour > 23 or minute > 59 or second > 59:
        return None
    try:
        t = _dt.datetime(year, month, day, hour, minute, second, micro, tzinfo=UTC)
    except ValueError:
        return None
    if not m.group(8):
        oh, om = int(m.group(10)), int(m.group(11))
        if oh > 23 or om > 59:
            return None
        delta = _dt.timedelta(hours=oh, minutes=om)
        t = t - delta if m.group(9) == "+" else t + delta
    return t


# ---------------------------------------------------------------- strings


def truncate_runes(s: str, n: int) -> str:
    """s cut to at most n characters, marking the cut."""
    if n <= 0:
        return ""
    if len(s) <= n:
        return s
    return s[:n] + "…"


def valid_utf8(s: str) -> bool:
    """Whether s stands for valid UTF-8 (no lone surrogates, which is how Python
    carries command-line bytes that are not UTF-8)."""
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def raw_bytes(s: str) -> bytes:
    """The bytes s stands for: UTF-8, with surrogate-escaped bytes restored."""
    try:
        return s.encode("utf-8")
    except UnicodeEncodeError:
        pass
    try:
        return s.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        return s.encode("utf-8", "surrogatepass")


def sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def go_quote(s: str) -> str:
    """strconv.Quote for the strings the library quotes in messages."""
    out = ['"']
    for ch in s:
        c = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\a":
            out.append("\\a")
        elif ch == "\b":
            out.append("\\b")
        elif ch == "\f":
            out.append("\\f")
        elif ch == "\v":
            out.append("\\v")
        elif c < 0x20 or c == 0x7F:
            out.append(f"\\x{c:02x}")
        elif 0xD800 <= c <= 0xDFFF:
            out.append("\\ufffd")
        elif not ch.isprintable():
            out.append(f"\\u{c:04x}" if c <= 0xFFFF else f"\\U{c:08x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def snake(name: str) -> str:
    return name.replace("-", "_")


def pick(cond: bool, yes: str, no: str) -> str:
    return yes if cond else no


def go_bool_string(v: bool) -> str:
    return "true" if v else "false"


_GO_BOOLS = {
    "1": True,
    "t": True,
    "T": True,
    "TRUE": True,
    "true": True,
    "True": True,
    "0": False,
    "f": False,
    "F": False,
    "FALSE": False,
    "false": False,
    "False": False,
}


def parse_go_bool(s: str) -> bool:
    """strconv.ParseBool: raises ValueError for anything else."""
    if s in _GO_BOOLS:
        return _GO_BOOLS[s]
    raise ValueError(f"invalid boolean {s!r}")


_GO_INT = re.compile(r"[+-]?[0-9]+")


def parse_go_int(s: str) -> int:
    """strconv.ParseInt(s, 10, 64)."""
    if not _GO_INT.fullmatch(s):
        raise ValueError(f"invalid integer {s!r}")
    n = int(s, 10)
    if not -(1 << 63) <= n < (1 << 63):
        raise ValueError("integer out of range")
    return n


def go_value_string(v: Any) -> str:
    """A parsed flag value as pflag's Value.String() renders it."""
    if isinstance(v, bool):
        return go_bool_string(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return go_format_g(v)
    if isinstance(v, _dt.timedelta):
        return go_duration(v)
    if v is None:
        return ""
    if isinstance(v, enum.Enum):
        return go_value_string(v.value)
    return str(v)


def go_format_g(f: float) -> str:
    """strconv.FormatFloat(f, 'g', -1, 64)."""
    if math.isnan(f):
        return "NaN"
    if math.isinf(f):
        return "+Inf" if f > 0 else "-Inf"
    if f == 0:
        return "-0" if math.copysign(1.0, f) < 0 else "0"
    sign, digits, exp = decimal.Decimal(repr(f)).as_tuple()
    ds = list(digits)
    while len(ds) > 1 and ds[-1] == 0:
        ds.pop()
        exp += 1
    text = "".join(str(d) for d in ds)
    n = len(text)
    e = exp + n - 1
    if e < -4 or e >= 6:  # shortest %g decides with precision 6
        mant = text[0] + ("." + text[1:] if n > 1 else "")
        out = f"{mant}e{'-' if e < 0 else '+'}{abs(e):02d}"
    else:
        point = e + 1
        if point <= 0:
            out = "0." + "0" * (-point) + text
        elif point >= n:
            out = text + "0" * (point - n)
        else:
            out = text[:point] + "." + text[point:]
    return ("-" if sign else "") + out


def go_duration(d: _dt.timedelta) -> str:
    """time.Duration.String() for a timedelta (microsecond precision)."""
    total_us = d.days * 86_400_000_000 + d.seconds * 1_000_000 + d.microseconds
    if total_us == 0:
        return "0s"
    neg = total_us < 0
    us = abs(total_us)
    if us < 1_000_000:
        out = f"{us}µs" if us < 1000 else _trim_frac(us / 1000) + "ms"
    else:
        hours, rem = divmod(us, 3_600_000_000)
        minutes, rem = divmod(rem, 60_000_000)
        secs = rem / 1_000_000
        out = ""
        if hours:
            out += f"{hours}h"
        if hours or minutes:
            out += f"{minutes}m"
        out += _trim_frac(secs) + "s"
    return ("-" if neg else "") + out


def _trim_frac(x: float) -> str:
    s = f"{x:.6f}".rstrip("0").rstrip(".")
    return s or "0"


def go_value_text(v: Any) -> str:
    """fmt's %v for the JSON values help and text output print."""
    if v is None:
        return "<nil>"
    if isinstance(v, bool):
        return go_bool_string(v)
    if isinstance(v, RawNumber):
        return str.__str__(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return go_format_g(v)
    return str(v)


# ---------------------------------------------------------------- CSV (pflag string slices)


def read_csv_record(s: str) -> list[str]:
    """The first record of s read with Go's encoding/csv defaults, as pflag's
    string slices split their values ("a,b" -> ["a", "b"]; quotes group commas).
    Raises ValueError where Go's reader reports an error."""
    if s == "":
        return []
    lines = s.split("\n")
    i = 0
    # encoding/csv skips empty lines before the record
    while i < len(lines) and lines[i] in ("", "\r") and i < len(lines) - 1:
        i += 1
    line = lines[i]
    if line.endswith("\r"):
        line = line[:-1]
    fields: list[str] = []
    pos = 0
    while True:
        if pos < len(line) and line[pos] == '"':
            # quoted field, possibly spanning lines
            pos += 1
            buf: list[str] = []
            while True:
                j = line.find('"', pos)
                if j < 0:
                    buf.append(line[pos:])
                    i += 1
                    if i >= len(lines):
                        raise ValueError('extraneous or missing " in quoted-field')
                    buf.append("\n")
                    line = lines[i]
                    if line.endswith("\r") and i == len(lines) - 1:
                        line = line[:-1]
                    pos = 0
                    continue
                buf.append(line[pos:j])
                pos = j + 1
                if pos < len(line) and line[pos] == '"':
                    buf.append('"')
                    pos += 1
                    continue
                if pos == len(line):
                    fields.append("".join(buf))
                    return fields
                if line[pos] == ",":
                    fields.append("".join(buf))
                    pos += 1
                    break
                raise ValueError('extraneous or missing " in quoted-field')
            continue
        j = line.find(",", pos)
        field = line[pos:] if j < 0 else line[pos:j]
        if '"' in field:
            raise ValueError('bare " in non-quoted-field')
        fields.append(field)
        if j < 0:
            return fields
        pos = j + 1


def write_csv_record(values: list[str]) -> str:
    """Go's csv.Writer for one record without the line end (pflag's slice String())."""
    out = []
    for v in values:
        if v == "" or not (any(c in v for c in ',"\r\n') or v[0] in " \t"):
            out.append(v)
        else:
            out.append('"' + v.replace('"', '""') + '"')
    return ",".join(out)
