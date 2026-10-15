"""The fake upstream: a tiny notes service with HTTP-like errors. Tests use it in
memory and read writes() to prove the write gate sent nothing; the command keeps it
in a JSON file so a dry-run and its confirm see the same notes."""

from __future__ import annotations

import datetime as dt
import json
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Note:
    """One note as the service returns it."""

    id: str
    ref: str
    title: str
    body: str
    tags: list[str] = field(default_factory=list)
    version: int = 1
    updated_at: str = ""

    def copy(self) -> Note:
        return Note(
            self.id, self.ref, self.title, self.body, list(self.tags), self.version, self.updated_at
        )


class APIError(Exception):
    """The service's error: an HTTP status and the service's own message. Status 0
    means the request never left the client."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"{status} {message}")
        self.status = status
        self.message = message


SEED = [
    ("Groceries", "Milk, eggs, coffee.", "home"),
    ("Release plan", "Cut 2.0.0 on Friday.", "work"),
    ("Reading list", "The Design of Everyday Things.", "home"),
]


def _rfc3339(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class MemStore:
    """An in-memory notes service that counts the write requests it receives."""

    def __init__(self) -> None:
        self.mu = threading.Lock()
        self.now: Callable[[], dt.datetime] = lambda: dt.datetime(
            2026, 10, 12, 9, 0, 0, tzinfo=dt.timezone.utc
        )
        # fail, when set, decides the outcome of a write before it is applied (tests)
        self.fail: Callable[[str, str], BaseException | None] | None = None
        self.save: Callable[[MemStore], None] | None = None
        self.reset()

    def reset(self) -> None:
        """Restore the seed notes."""
        with self.mu:
            self.notes: dict[str, Note] = {}
            for i, (title, body, tag) in enumerate(SEED):
                nid = str(i + 1)
                self.notes[nid] = Note(
                    nid, "notes/" + nid, title, body, [tag], 1, "2026-10-01T08:00:00Z"
                )
            self.next = 4
            self._writes = 0
            self.keys: dict[str, str] = {}  # idempotency key -> note id

    def writes(self) -> int:
        """The number of write requests that reached the service."""
        with self.mu:
            return self._writes

    def list(self, tag: str) -> list[Note]:
        with self.mu:
            out = [n.copy() for n in self.notes.values() if not tag or tag in n.tags]
        return sorted(out, key=lambda n: int(n.id))

    def get(self, nid: str) -> Note:
        with self.mu:
            n = self.notes.get(nid)
            if n is None:
                raise APIError(404, "note not found")
            return n.copy()

    def _write(self, op: str, nid: str, fn: Callable[[], Any]) -> Any:
        """One write request: the failure hook first (it may claim the request
        never left, or was refused), then the change, then persistence."""
        with self.mu:
            fail = self.fail
        if fail is not None:
            err = fail(op, nid)
            if err is not None:
                if not (isinstance(err, APIError) and err.status == 0):
                    with self.mu:
                        self._writes += 1
                raise err
        with self.mu:
            self._writes += 1
            out = fn()
            save = self.save
        if save is not None:
            save(self)
        return out

    def create(self, title: str, body: str, tags: list[str], key: str) -> Note:
        def do() -> Note:
            if key and key in self.keys:
                return self.notes[self.keys[key]].copy()
            nid = str(self.next)
            self.next += 1
            n = Note(nid, "notes/" + nid, title, body, list(tags), 1, _rfc3339(self.now()))
            self.notes[nid] = n
            if key:
                self.keys[key] = nid
            return n.copy()

        return self._write("create", "", do)

    def update(
        self,
        nid: str,
        title: str | None,
        body: str | None,
        add_tags: list[str],
        remove_tags: list[str],
    ) -> Note:
        def do() -> Note:
            n = self.notes.get(nid)
            if n is None:
                raise APIError(404, "note not found")
            if title is not None:
                n.title = title
            if body is not None:
                n.body = body
            for t in add_tags:
                if t not in n.tags:
                    n.tags.append(t)
            n.tags = [t for t in n.tags if t not in remove_tags]
            n.version += 1
            n.updated_at = _rfc3339(self.now())
            return n.copy()

        return self._write("update", nid, do)

    def delete(self, nid: str) -> None:
        def do() -> None:
            if nid not in self.notes:
                raise APIError(404, "note not found")
            del self.notes[nid]

        self._write("delete", nid, do)

    def share(self, nid: str, to: str, token: str) -> dt.datetime:
        at = self.now()

        def do() -> None:
            if nid not in self.notes:
                raise APIError(404, "note not found")
            if token == "revoked-token":
                raise APIError(403, "sharing token revoked")

        self._write("share", nid, do)
        return at

    def add_tag(self, nid: str, tag: str) -> Note:
        return self.update(nid, None, None, [tag], [])


def file_store(path: str) -> MemStore:
    """A MemStore kept in a JSON file between runs of the command (the same layout
    as the Go example's, so either can read the other's file)."""
    s = MemStore()
    try:
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
    except (OSError, ValueError):
        saved = None
    if isinstance(saved, dict) and isinstance(saved.get("notes"), dict):
        s.notes = {k: Note(**v) for k, v in saved["notes"].items()}
        s.next = int(saved.get("next") or 0)
        if isinstance(saved.get("Keys"), dict):
            s.keys = dict(saved["Keys"])

    def save(m: MemStore) -> None:
        with m.mu:
            doc = {
                "Keys": m.keys,
                "next": m.next,
                "notes": {k: asdict(v) for k, v in sorted(m.notes.items())},
            }
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=2)
        except OSError:
            pass

    s.save = save
    return s
