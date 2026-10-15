# Vendored from ai-native-cli-spec template/python/aicli.
# Do not edit in tool repos; run scripts/sync-spec.js.
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sean Guo

"""Lists (CLI-041): Page and App.list."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ._util import to_generic, to_generic_data
from .envelope import Result_, prefix_paths, schema_untrusted
from .errors import internal_error

if TYPE_CHECKING:
    from .app import App


@dataclass
class Page:
    """One page of a list (CLI-041). Every list says whether it is complete:
    has_more with exactly one of next_cursor, next_offset or continuation_reason
    (and none of them without has_more); truncated with truncated_reason, and a
    truncated list has more; scope echoes what the list covers (filters actually
    applied, and a narrower-than-expected scope such as direct members only); sort
    names the order; total, when the upstream reports it, is the number of items
    the whole list has, so a count question needs no paging (CLI-044). untrusted
    lists paths relative to the page data, for example "items[].title"; the
    output schema's untrusted fields are added automatically."""

    items: Any = None
    count: int = 0
    has_more: bool = False
    next_cursor: str = ""
    next_offset: int | None = None
    continuation_reason: str = ""
    truncated: bool = False
    truncated_reason: str = ""
    scope: dict[str, Any] | None = None
    sort: str = ""
    total: int | None = None
    untrusted: list[str] = field(default_factory=list)


class QueryMixin:
    def list(self: App, cmd: Any, p: Page) -> None:
        """Report a page of items. count is the number of items (0 means "count
        them"); a page that breaks the completeness rules is a bug in the tool and
        is reported as E_UNKNOWN. Default fields and --fields apply to each item;
        the pagination keys are always kept."""
        st, node = self._run_for(cmd)
        m = self.meta_for(node)
        if m is not None and m.is_write():
            raise internal_error(
                "A write command reported a list without the write gate; it must use App.write."
            )
        try:
            items = to_generic(p.items)
        except (TypeError, ValueError):
            raise internal_error("The list items cannot be encoded as JSON.") from None
        if not isinstance(items, list):
            if items is not None:
                raise internal_error("Page.items must be a list.")
            items = []
        count = p.count or len(items)
        if count != len(items):
            raise internal_error("Page.count must equal the number of items in the page (CLI-041).")
        continuations = 0
        data: dict[str, Any] = {"items": items, "count": count, "has_more": p.has_more}
        if p.next_cursor:
            data["next_cursor"] = p.next_cursor
            continuations += 1
        if p.next_offset is not None:
            if p.next_offset < 0:
                raise internal_error("Page.next_offset must not be negative (CLI-041).")
            data["next_offset"] = p.next_offset
            continuations += 1
        if p.continuation_reason.strip():
            data["continuation_reason"] = p.continuation_reason
            continuations += 1
        if p.has_more and continuations != 1:
            raise internal_error(
                "A list with has_more must give exactly one of next_cursor, next_offset or "
                "continuation_reason (CLI-041)."
            )
        if not p.has_more and continuations > 0:
            raise internal_error(
                "A list without has_more gives no next_cursor, next_offset or continuation_reason "
                "(CLI-041)."
            )
        if p.truncated:
            if not p.truncated_reason.strip():
                raise internal_error("A truncated list must give truncated_reason (CLI-041).")
            if not p.has_more:
                raise internal_error(
                    "A truncated list has more items: set has_more and say how to continue "
                    "(CLI-041)."
                )
            data["truncated"] = True
            data["truncated_reason"] = p.truncated_reason
        elif p.truncated_reason.strip():
            raise internal_error("Page.truncated_reason is only for a truncated list (CLI-041).")
        if p.total is not None:
            if p.total < count:
                raise internal_error(
                    "Page.total counts the whole list, so it is at least the page's count "
                    "(CLI-041)."
                )
            data["total"] = p.total
        if p.scope:
            data["scope"] = p.scope
        if p.sort:
            data["sort"] = p.sort
        try:
            gen = to_generic_data(data)
        except (TypeError, ValueError):
            raise internal_error("The list cannot be encoded as JSON.") from None
        schema = self.schema_of(m)
        gen = self.project(st, m, schema, "page", gen)
        paths = prefix_paths("data", p.untrusted) + schema_untrusted(schema, "page")
        st.out = Result_(set=True, data=gen, raw=data, untrusted=paths)
