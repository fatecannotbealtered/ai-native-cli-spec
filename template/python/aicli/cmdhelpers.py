# Vendored from ai-native-cli-spec template/python/aicli.
# Do not edit in tool repos; run scripts/sync-spec.js.
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sean Guo

"""Helpers a command calls from its own code: the run's output format (Q14),
whether the response keeps a field (Q17), and CLI-028's durations and times
(Q15)."""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any

from ._util import go_quote, parse_time, utc
from .app import _CURRENT


def output_format(ctx: Any = None) -> str:
    """The running command's output format as resolved for this run (Q14): json,
    text or raw. A command that answers with bytes in raw mode and an object
    otherwise (Meta.raw_output) asks it before App.ok. ctx is the command's click
    context or command (None: the one running); outside App.execute it is json."""
    app = _CURRENT.get()
    if app is None:
        return "json"
    return app.output_format(ctx)


def wants_field(ctx: Any, name: str) -> bool:
    """Whether the running command's response keeps the field name (Q17), by the
    rules of --fields (CLI-040): all keeps every field; a,b keeps those; +a keeps
    the default fields and a; without --fields the default fields
    (Meta.default_fields, else the output schema's) are kept, or every field when
    there are none; ref is always kept. A command asks it to skip a read whose only
    use is a field the response will not show. Outside App.execute it is True."""
    app = _CURRENT.get()
    if app is None:
        return True
    return app.wants_field(ctx, name)


# A CLI-028 duration: a whole number and one unit.
_DURATION = re.compile(r"([0-9]+)(ms|s|m|h|d)")
_UNITS_NS = {
    "ms": 1_000_000,
    "s": 1_000_000_000,
    "m": 60_000_000_000,
    "h": 3_600_000_000_000,
    "d": 86_400_000_000_000,
}
_MAX_NS = (1 << 63) - 1
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def parse_duration(s: str) -> _dt.timedelta:
    """A duration as CLI-028 writes it (Q15): a whole number and one unit, ms, s,
    m, h or d (500ms, 30s, 5m, 2h, 7d), for --wait and any other flag that takes
    one. Raises ValueError."""
    m = _DURATION.fullmatch(s.strip()) if isinstance(s, str) else None
    if m is None:
        raise ValueError(
            f"{go_quote(str(s))} is not a duration: use a whole number and one unit, ms, s, m, "
            "h or d (500ms, 30s, 5m, 2h, 7d)"
        )
    ns = int(m.group(1)) * _UNITS_NS[m.group(2)]
    if ns > _MAX_NS:
        raise ValueError(f"{go_quote(s)} is too long a duration")
    return _dt.timedelta(microseconds=ns // 1000)


def parse_time_arg(s: str, now: _dt.datetime | None = None) -> _dt.datetime:
    """A time as CLI-028 writes it (Q15), for --since and --until: an ISO 8601
    date (2026-06-01, midnight UTC), an ISO 8601 date and time
    (2026-06-01T08:00:00Z; an offset is converted to UTC), or a duration ago (7d is
    now minus 7 days). The result is an aware UTC datetime, to echo in a list's
    scope. now defaults to the current time. Raises ValueError."""
    t = s.strip() if isinstance(s, str) else ""
    if _DURATION.fullmatch(t):
        d = parse_duration(t)
        base = utc(now) if now is not None else _dt.datetime.now(_dt.timezone.utc)
        return base - d
    if _DATE.fullmatch(t):
        try:
            day = _dt.date.fromisoformat(t)
        except ValueError:
            day = None
        if day is not None:
            return _dt.datetime(day.year, day.month, day.day, tzinfo=_dt.timezone.utc)
    v = parse_time(t)
    if v is not None:
        return v
    raise ValueError(
        f"{go_quote(str(s))} is not a time: use an ISO 8601 date (2026-06-01), a date and time "
        "(2026-06-01T08:00:00Z) or a time ago (7d, 24h, 30m)"
    )
