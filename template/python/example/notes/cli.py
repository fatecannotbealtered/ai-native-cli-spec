"""The example "notes" tool: a minimal AI-native CLI on the aicli library (click).

It shows how a tool declares its commands (register, schema), reads (ok, list),
writes (write, write_batch), file and secret inputs, a setting (the service host),
and maps upstream errors. Everything else (envelope, errors, reference, help, the
confirm gate, scope, journal, profiles, built-ins and the update built-in) comes
from the library. A tool vendors the library as <pkg>/_aicli and imports it with
`from ._aicli import ...`; this example lives next to the library in the spec
repository and imports it as `aicli`."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from importlib import resources
from typing import Any

import click

import aicli
from aicli import (
    CHANGE,
    DELIVER,
    EXPERIMENTAL,
    OBSERVE,
    App,
    BatchItem,
    BatchSpec,
    Block,
    FieldChange,
    Identity,
    Meta,
    Op,
    Outcome,
    Page,
    Preview,
    Result,
    SchemaDef,
    Setting,
    Target,
    UpdateConfig,
    WriteSpec,
)

from . import __version__
from .store import APIError, MemStore

# The fields a note has in every output.
NOTE_FIELDS = ["id", "ref", "title", "body", "tags", "version", "updated_at"]

# The tests behind each command's maturity (VER-005): the example runs against a
# fake upstream only, so every command is experimental.
EVIDENCE = "simulated:template/python/example/tests/test_notes.py"

# The npm wrapper package the tool is published as (also used by pnpm and yarn).
NPM_PACKAGE = "notes-cli-example"


def changelog() -> str:
    """The CHANGELOG.md shipped inside the package."""
    return resources.files(__package__).joinpath("CHANGELOG.md").read_text(encoding="utf-8")


def note_id(s: str) -> str:
    """What an agent has, as an id: "5", "notes/5" or "https://notes.example/notes/5" (CLI-048)."""
    s = (s or "").strip()
    i = s.rfind("/notes/")
    if i >= 0:
        s = s[i + len("/notes/") :]
    s = s.removeprefix("notes/")
    return s.strip("/")


def valid_id(nid: str) -> None:
    """An id is a decimal integer (as Go's strconv.Atoi reads it)."""
    if not (re.fullmatch(r"[+-]?[0-9]+", nid) and -(2**63) <= int(nid) < 2**63):
        e = aicli.validation(
            "--id", "a note id like 5, a ref like notes/5, or a note URL", "That is not a note id."
        )
        e.details["next_action"] = "Run `notes note list` to find the note."
        raise e


def resolve_id(raw: Any) -> str:
    """The note id (positional or --id) in any form the agent has, bound in its
    canonical form, so `note get notes/5` and `note get 5` are one request."""
    nid = note_id(raw or "")
    valid_id(nid)
    aicli.set_resolved(None, "id", nid)
    return nid


def read_error(err: BaseException, what: str) -> BaseException:
    """A service error on a read, mapped by HTTP status, never by message (CLI-016)."""
    if isinstance(err, APIError):
        e = aicli.from_http(err.status, what, err.message)
        if err.status == 404:
            e.details["next_action"] = "Run `notes note list` to find the note."
        return e
    return err


def write_error(err: BaseException, what: str) -> BaseException:
    """A failed write for the gate: a request that never left the client is
    not_dispatched, a definite 4xx refusal is rejected, and anything else (5xx,
    timeouts, resets) stays as it is, so the gate reports an unknown outcome
    instead of a retryable error (CLI-015)."""
    if isinstance(err, APIError):
        if err.status == 0:
            return aicli.not_dispatched(aicli.from_http(0, what, err.message))
        if 400 <= err.status < 500:
            return aicli.rejected(aicli.from_http(err.status, what, err.message))
    return err


def new_app(svc: MemStore, env: dict[str, str] | None = None) -> App:
    """The CLI around svc. env overrides process environment variables (the
    conformance suite passes the state directory and scope file this way)."""

    def getenv(k: str) -> str:
        if env is not None and k in env:
            return env[k]
        return os.environ.get(k, "")

    def identity() -> Identity:
        # The account and host come from local configuration only. The host is a
        # setting: NOTES_HOST, else the active profile of the config file, else the
        # default (CLI-100).
        source, account = "env", getenv("NOTES_ACCOUNT")
        if not account:
            account, source = "demo", "default"
        host, _ = app.setting("host")
        return Identity(account, host, {"credentials_source": source})

    root = click.Group("notes", short_help="Read and change notes; built for AI agents")
    app = App(
        name="notes",
        version=__version__,
        risk_tier="T2",
        env_prefix="NOTES",
        root=root,
        changelog=changelog(),
        env=getenv,
        identity=identity,
        # The built-in update (CLI-070): releases of this repository, or the npm
        # package when that is how the tool was installed.
        update=UpdateConfig(repo="fatecannotbealtered/notes-cli-example", npm_package=NPM_PACKAGE),
        settings=[
            Setting("host", env="HOST", default="memory://notes", description="The notes service")
        ],
        # Accept the forms agents type first, or answer them precisely (CLI-022).
        # Lossless renames are accepted: flag_aliases here, verb aliases in
        # Meta.aliases. flag_synonyms only suggest, for guesses that need more than
        # a rename (--all has no value; --limit needs one). verb_synonyms suggest
        # verbs that are close but not the same command (search is a list with a query).
        short_flags={"t": "title"},
        flag_aliases={
            "label": "tag",
            "labels": "tag",
            "tags": "tag",
            "text": "body",
            "message": "body",
        },
        flag_synonyms={"all": ["limit"]},
        verb_synonyms={"search": ["list"], "find": ["list"]},
    )
    app.schema("note", SchemaDef(shape="object", fields=NOTE_FIELDS, untrusted=["title", "body"]))
    app.schema(
        "note_page",
        SchemaDef(
            shape="page",
            fields=NOTE_FIELDS,
            default_fields=["ref", "title", "tags", "updated_at"],
            untrusted=["title", "body"],
        ),
    )
    app.schema("share_result", SchemaDef(shape="object", fields=["ref", "to", "shared_at"]))
    app.schema("tag_result", SchemaDef(shape="object", fields=["ref", "tags"]))
    app.schema("delete_result", SchemaDef(shape="object", fields=["ref", "deleted"]))

    note = click.Group("note")
    app.group(note, "Work with notes", aliases=["notes"])
    root.add_command(note)

    add_list(app, note, svc)
    add_get(app, note, svc)
    add_create(app, note, svc)
    add_update(app, note, svc)
    add_delete(app, note, svc)
    add_share(app, note, svc, getenv)
    add_bulk_tag(app, note, svc)
    return app


def add_list(app: App, parent: click.Group, svc: MemStore) -> None:
    @parent.command("list")
    @click.option("--tag", default="", help="Only notes with this tag")
    @click.option("--limit", type=int, default=20, help="Notes per page (1-100)")
    @click.option("--offset", type=int, default=0, help="Skip this many notes (from next_offset)")
    def list_cmd(tag: str, limit: int, offset: int) -> None:
        if limit < 1 or limit > 100:
            e = aicli.usage("invalid_value", "--limit", "--limit must be between 1 and 100.")
            e.details["expected"] = "1-100"
            raise e
        if offset < 0:
            e = aicli.usage("invalid_value", "--offset", "--offset must not be negative.")
            e.details["expected"] = "0 or more, from next_offset"
            raise e
        try:
            notes = svc.list(tag)
        except APIError as err:
            raise read_error(err, "The notes could not be listed.") from None
        start, end = min(offset, len(notes)), min(offset + limit, len(notes))
        # scope names what the list covers and only the filters actually applied
        scope: dict[str, Any] = {"covers": "every note this account can read"}
        if tag:
            scope["tag"] = tag
        page = Page(
            items=notes[start:end],
            has_more=end < len(notes),
            sort="id_asc",
            scope=scope,
            total=len(notes),
        )
        if page.has_more:
            page.next_offset = end
        app.list(None, page)

    app.register(
        list_cmd,
        Meta(
            summary="List notes",
            effects=[OBSERVE],
            output="note_page",
            aliases=["ls"],
            maturity=EXPERIMENTAL,
            evidence=[EVIDENCE],
            examples=["notes note list --tag work", "notes note list --fields +body"],
        ),
    )


def add_get(app: App, parent: click.Group, svc: MemStore) -> None:
    @parent.command("get")
    @click.option("--id", "nid", required=True, help="Note id, ref (notes/5) or URL")
    def get_cmd(nid: str) -> None:
        i = resolve_id(nid)
        try:
            n = svc.get(i)
        except APIError as err:
            raise read_error(err, "No note has that id.") from None
        app.ok(None, n)

    app.register(
        get_cmd,
        Meta(
            summary="Show one note",
            effects=[OBSERVE],
            output="note",
            positional="id",
            aliases=["view", "show"],
            error_codes=["E_NOT_FOUND", "E_VALIDATION"],
            maturity=EXPERIMENTAL,
            evidence=[EVIDENCE],
            examples=["notes note get 1", "notes note get notes/1"],
        ),
    )


def add_create(app: App, parent: click.Group, svc: MemStore) -> None:
    @parent.command("create")
    @click.option("--title", required=True, help="Title")
    @click.option("--body", default="", help="Body text")
    @click.option("--body-file", help="Read the body from a file (- for stdin)")
    @click.option("--tag", multiple=True, help="Tags (comma-separated or repeated)")
    @click.option(
        "--idempotency-key",
        help="Repeat-safe key: the same request with the same key returns the first result",
    )
    def create_cmd(
        title: str, body: str, body_file: Any, tag: tuple[str, ...], idempotency_key: Any
    ) -> None:
        # File inputs are read through the library, so the token binds these bytes.
        b = app.read_file_input(None, "body-file")
        if b is not None:
            body = b.decode("utf-8", "replace")
        tags = list(tag)
        if not title.strip():
            raise aicli.validation("--title", "a non-empty title", "The title is empty.")

        def preview() -> Preview:
            return Preview(
                action="Create a note",
                can_apply=True,
                changes=[
                    FieldChange("title", after=title),
                    FieldChange("body", after=body),
                    FieldChange("tags", after=tags),
                ],
            )

        def apply(op: Op) -> Result:
            try:
                n = svc.create(title, body, tags, op.idempotency_key)
            except APIError as err:
                raise write_error(err, "The note was not created.") from None
            # Report what the service now holds; the gate compares it with the
            # preview. The new note's ref and id are observed too, so operation
            # show finds them after a lost reply (CLI-059).
            return Result(
                observed={
                    "title": n.title,
                    "body": n.body,
                    "tags": n.tags,
                    "ref": n.ref,
                    "id": n.id,
                },
                data=n,
                untrusted=["title", "body"],
            )

        app.write(
            None,
            WriteSpec(
                target=Target("notes", "collection", "all notes"), preview=preview, apply=apply
            ),
        )

    app.register(
        create_cmd,
        Meta(
            summary="Create a note",
            effects=[CHANGE],
            output="note",
            file_inputs={"body-file": "body"},
            aliases=["add", "new"],
            error_codes=["E_VALIDATION"],
            maturity=EXPERIMENTAL,
            evidence=[EVIDENCE],
            examples=[
                "notes note create --title Groceries --body-file list.txt --dry-run",
                "notes note create --title Groceries --body-file list.txt --confirm <token>",
            ],
        ),
    )


def add_update(app: App, parent: click.Group, svc: MemStore) -> None:
    @parent.command("update")
    @click.option("--id", "nid", required=True, help="Note id, ref (notes/5) or URL")
    @click.option("--title", help="New title")
    @click.option("--body", help="New body")
    @click.option("--body-file", help="Read the new body from a file (- for stdin)")
    @click.option("--add-tag", multiple=True, help="Tags to add")
    @click.option("--remove-tag", multiple=True, help="Tags to remove")
    def update_cmd(
        nid: str, title: Any, body: Any, body_file: Any, add_tag: Any, remove_tag: Any
    ) -> None:
        i = resolve_id(nid)
        new_title = title if app.changed(None, "title") else None
        new_body = body if app.changed(None, "body") else None
        b = app.read_file_input(None, "body-file")
        if b is not None:
            new_body = b.decode("utf-8", "replace")
        add, remove = list(add_tag), list(remove_tag)

        def preview() -> Preview:
            try:
                n = svc.get(i)
            except APIError as err:
                raise read_error(err, "No note has that id.") from None
            changes = []
            if new_title is not None:
                changes.append(FieldChange("title", before=n.title, after=new_title))
            if new_body is not None:
                changes.append(FieldChange("body", before=n.body, after=new_body))
            if add or remove:
                changes.append(
                    FieldChange("tags", before=n.tags, add=add or None, remove=remove or None)
                )
            pv = Preview(
                action="Update note " + i,
                changes=changes,
                can_apply=True,
                base_version=str(n.version),
                base_source="version",
                untrusted=["changes[].before"],
            )
            if not changes:
                pv.blocked_by = [
                    Block(
                        "no_change", "Give --title, --body, --body-file, --add-tag or --remove-tag."
                    )
                ]
            return pv

        def apply(op: Op) -> Result:
            try:
                n = svc.update(i, new_title, new_body, add, remove)
            except APIError as err:
                raise write_error(err, "The note was not updated.") from None
            observed: dict[str, Any] = {"tags": n.tags}
            if new_title is not None:
                observed["title"] = n.title
            if new_body is not None:
                observed["body"] = n.body
            return Result(observed=observed, data=n, untrusted=["title", "body"])

        app.write(
            None, WriteSpec(target=Target("notes/" + i, "note"), preview=preview, apply=apply)
        )

    app.register(
        update_cmd,
        Meta(
            summary="Change a note's title, body or tags",
            effects=[CHANGE],
            output="note",
            positional="id",
            file_inputs={"body-file": "body"},
            aliases=["edit"],
            error_codes=["E_NOT_FOUND", "E_VALIDATION"],
            maturity=EXPERIMENTAL,
            evidence=[EVIDENCE],
            # An update with nothing to change is a usage error before the gate, not
            # a blocked preview (exit 5 would ask for a token that can never exist).
            one_required=[["title", "body", "body-file", "add-tag", "remove-tag"]],
            examples=[
                'notes note update 1 --title "Weekly groceries" --dry-run',
                'notes note update 1 --title "Weekly groceries" --confirm <token>',
            ],
        ),
    )


def add_delete(app: App, parent: click.Group, svc: MemStore) -> None:
    @parent.command("delete")
    @click.option("--id", "nid", required=True, help="Note id, ref (notes/5) or URL")
    def delete_cmd(nid: str) -> None:
        i = resolve_id(nid)

        def preview() -> Preview:
            try:
                n = svc.get(i)
            except APIError as err:
                raise read_error(err, "No note has that id.") from None
            return Preview(
                action=f"Delete note {i} permanently",
                can_apply=True,
                base_version=str(n.version),
                base_source="version",
                changes=[FieldChange("note", before=n.title, note="cannot be undone")],
                untrusted=["changes[].before"],
            )

        def apply(op: Op) -> Result:
            try:
                svc.delete(i)
            except APIError as err:
                raise write_error(err, "The note was not deleted.") from None
            # Read back: the note must be gone; observed says what the read showed.
            try:
                svc.get(i)
            except APIError:
                return Result(
                    outcome=Outcome(verification="passed"),
                    observed={"exists": False},
                    data={"ref": "notes/" + i, "deleted": True},
                )
            return Result(
                outcome=Outcome(verification="failed"),
                observed={"exists": True},
                mismatched=["exists"],
            )

        app.write(
            None, WriteSpec(target=Target("notes/" + i, "note"), preview=preview, apply=apply)
        )

    app.register(
        delete_cmd,
        Meta(
            summary="Delete a note permanently",
            effects=[CHANGE],
            dangerous=True,
            output="delete_result",
            positional="id",
            aliases=["remove", "rm"],
            error_codes=["E_NOT_FOUND"],
            maturity=EXPERIMENTAL,
            evidence=[EVIDENCE],
            examples=[
                "notes note delete 3 --dangerous --dry-run",
                "notes note delete 3 --dangerous --confirm <token>",
            ],
        ),
    )


def add_share(app: App, parent: click.Group, svc: MemStore, getenv: Callable[[str], str]) -> None:
    @parent.command("share")
    @click.option("--id", "nid", required=True, help="Note id, ref (notes/5) or URL")
    @click.option("--to", required=True, help="Recipient email address")
    @click.option(
        "--token",
        help="Sharing-service token for one-off use; set NOTES_SHARE_TOKEN instead (never echoed)",
    )
    def share_cmd(nid: str, to: str, token: Any) -> None:
        i = resolve_id(nid)
        # The sharing token comes from NOTES_SHARE_TOKEN (SEC-040); --token is for
        # one-off use. An input resolved from the environment is bound like a flag
        # (CLI-054): set_resolved records it, and as a secret it is bound by a keyed
        # digest and shown as ***.
        secret = token or ""
        if not app.changed(None, "token"):
            secret = getenv("NOTES_SHARE_TOKEN")
            if secret:
                aicli.set_resolved(None, "token", secret)
        if "@" not in to:
            raise aicli.validation(
                "--to", "an email address", "The recipient is not an email address."
            )

        def preview() -> Preview:
            try:
                n = svc.get(i)
            except APIError as err:
                raise read_error(err, "No note has that id.") from None
            # Destinations are checked against the scope's destinations (SEC-012).
            return Preview(
                action=f"Email note {i} to {to}",
                can_apply=True,
                destinations=[to],
                changes=[
                    FieldChange("recipients", add=[to], note="sends the title and body: " + n.title)
                ],
                untrusted=["changes[].note"],
            )

        def apply(op: Op) -> Result:
            try:
                at = svc.share(i, to, secret)
            except APIError as err:
                if err.status == 403:
                    # The upstream refused this token: say what it needs (CLI-014).
                    e = aicli.from_http(
                        403,
                        "The sharing service refused the token, so the note was not sent.",
                        err.message,
                    )
                    e.details["required"] = "a sharing token with share permission"
                    e.details["next_action"] = (
                        "Ask the user for a valid sharing token (NOTES_SHARE_TOKEN); do not retry "
                        "with other credentials."
                    )
                    raise aicli.rejected(e) from None
                raise write_error(err, "The note was not sent.") from None
            return Result(
                data={"ref": "notes/" + i, "to": to, "shared_at": at.strftime("%Y-%m-%dT%H:%M:%SZ")}
            )

        app.write(
            None, WriteSpec(target=Target("notes/" + i, "note"), preview=preview, apply=apply)
        )

    app.register(
        share_cmd,
        Meta(
            summary="Email a note to someone",
            effects=[DELIVER],
            output="share_result",
            positional="id",
            secrets=["token"],
            aliases=["send"],
            error_codes=["E_NOT_FOUND", "E_VALIDATION"],
            maturity=EXPERIMENTAL,
            evidence=[EVIDENCE],
            examples=[
                "notes note share 1 --to bob@example.com --dangerous --dry-run",
                "notes note share 1 --to bob@example.com --dangerous --confirm <token>",
            ],
        ),
    )


def add_bulk_tag(app: App, parent: click.Group, svc: MemStore) -> None:
    @parent.command("bulk-tag")
    @click.option("--ids", multiple=True, help="Note ids or refs (comma-separated or repeated)")
    @click.option("--tag", required=True, help="Tag to add")
    def bulk_tag_cmd(ids: Any, tag: str) -> None:
        resolved = []
        for raw in ids:
            nid = note_id(raw)
            valid_id(nid)
            resolved.append(nid)
        if resolved:
            aicli.set_resolved(None, "ids", *resolved)
        items = []
        for nid in resolved:

            def preview(nid: str = nid) -> Preview:
                try:
                    n = svc.get(nid)
                except APIError as err:
                    raise read_error(err, "No note has that id.") from None
                return Preview(
                    action="Tag notes " + tag,
                    can_apply=True,
                    base_version=str(n.version),
                    base_source="version",
                    changes=[FieldChange("tags", add=[tag])],
                )

            def apply(op: Op, nid: str = nid) -> Result:
                try:
                    n = svc.add_tag(nid, tag)
                except APIError as err:
                    raise write_error(err, "The tag was not added.") from None
                return Result(observed={"tags": n.tags}, data={"ref": n.ref, "tags": n.tags})

            items.append(BatchItem(Target("notes/" + nid, "note"), preview, apply))
        app.write_batch(None, BatchSpec(items=items))

    app.register(
        bulk_tag_cmd,
        Meta(
            summary="Add one tag to several notes",
            effects=[CHANGE],
            output="tag_result",
            batch_flag="ids",
            error_codes=["E_VALIDATION"],
            maturity=EXPERIMENTAL,
            evidence=[EVIDENCE],
            examples=[
                "notes note bulk-tag --ids 1,2 --tag urgent --dry-run",
                "notes note bulk-tag --ids 1,2 --tag urgent --confirm <token>",
            ],
        ),
    )
