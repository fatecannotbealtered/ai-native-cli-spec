# Vendored from ai-native-cli-spec template/python/aicli.
# Do not edit in tool repos; run scripts/sync-spec.js.
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sean Guo

"""NDJSON streams (CLI-006) and the --output file helper (CLI-042), decision 20.

A stream writes one envelope per line, each with type "item", and ends with one
line of type "summary": ok true with what the stream covered, or ok false with the
error that ended it. The exit code matches the summary line.

write_output writes a large result to a local file through the write gate: the
file is an artifact (new) or persist (overwritten) effect, the scope's outputs or
roots must allow its path (SEC-012), and the response carries the path and size
instead of the content."""

from __future__ import annotations

import contextlib
import hashlib
import os
import stat
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ._util import Ordered, go_json, to_generic, to_generic_data
from .contract_gen import CORE_CODES, SCHEMA_VERSION
from .envelope import (
    clean_paths,
    error_root,
    fit_error_budget,
    prefix_paths,
    present_paths,
    schema_untrusted,
    write_text,
    write_text_error,
)
from .errors import Error, conflict_error, internal_error, io_error, usage
from .gate import (
    Block,
    FieldChange,
    Op,
    Preview,
    Target,
    WriteSpec,
    create_only_exists,
    not_dispatched,
    output_exists,
)
from .journal import rename_retry
from .receipt import Result
from .registry import SchemaDef

if TYPE_CHECKING:
    from ._tree import Node
    from .app import App
    from .envelope import RunState
    from .registry import Meta


def stream_line(kind: str, data: Any, meta: Ordered) -> bytes:
    """One NDJSON line: an envelope plus type item or summary."""
    env = Ordered(ok=True, schema_version=SCHEMA_VERSION, type=kind, data=data, meta=meta)
    return (go_json(env) + "\n").encode("utf-8")


class Stream:
    """The NDJSON stream of the running read command (App.stream). item writes a
    line at once; the summary line is written last, when the command returns."""

    def __init__(self, app: App, st: RunState, node: Node) -> None:
        self._app = app
        self._st = st
        self._node = node
        self.items = 0
        self.summary_data: Any = None
        self.summary_untrusted: list[str] = []
        self.summary_set = False
        self.write_err: BaseException | None = None

    def item(self, data: Any, *untrusted: str) -> None:
        """Write one line of type item now. untrusted lists paths inside data that
        carry external content, relative to data; the item schema (Meta.stream_item,
        else the command's output schema) adds its untrusted fields (Q23), and
        --fields applies as for an object. With --format raw the item is written as
        bytes only: bytes or a str as they are, anything else as its compact JSON
        value. Raises E_IO when stdout cannot be written (a closed pipe): the stream
        stops."""
        a, st = self._app, self._st
        try:
            gen = to_generic_data(data)
        except (TypeError, ValueError):
            raise internal_error("A stream item cannot be encoded as JSON.") from None
        m = a.meta_for(self._node)
        schema, own = a.item_schema_of(m)
        # Meta.default_fields belong to the output schema, not to the items
        gen = a.project(st, m if own else None, schema, "object", gen)
        paths = clean_paths(
            present_paths(
                {"data": gen},
                prefix_paths("data", list(untrusted)) + schema_untrusted(schema, "object"),
            )
        )
        self.items += 1
        out = a._stdout()
        if st.format == "text":
            self._write(out, write_text(gen).encode("utf-8", "replace"))
            return
        if st.format == "raw":
            if isinstance(data, (bytes, bytearray)):
                self._write(out, bytes(data))
                return
            if isinstance(data, str):
                self._write(out, data.encode("utf-8"))
                return
            try:
                raw_line = (go_json(gen) + "\n").encode("utf-8")
            except (TypeError, ValueError):
                raise internal_error("A stream item cannot be encoded as JSON.") from None
            self._write(out, raw_line)
            return
        meta = Ordered(duration_ms=int((time.monotonic() - st.start_real) * 1000))
        if paths:
            meta["untrusted"] = paths
        try:
            line = stream_line("item", gen, meta)
        except (TypeError, ValueError):
            raise internal_error("A stream item cannot be encoded as JSON.") from None
        self._write(out, line)

    def _write(self, out: Any, b: bytes) -> None:
        try:
            self._app._write(out, b)
        except OSError as exc:
            self.write_err = exc
            raise io_error("The stream could not be written to stdout.", "", exc) from None

    def count(self) -> int:
        """The number of item lines written so far."""
        return self.items

    def summary(self, data: Any, *untrusted: str) -> None:
        """Record the summary line, written as the last line when the command
        returns: what the stream covered (counts, and the completeness keys of
        CLI-041 when it stopped early). untrusted is relative to data. A command
        that raises an Error ends with an error summary instead."""
        self.summary_data = data
        self.summary_untrusted = list(untrusted)
        self.summary_set = True


@dataclass
class OutputSpec:
    """A result written to a local file through the gate (App.write_output,
    CLI-042). path is the file the user named with --output. produce makes the
    content when the write is confirmed; an exception from it means nothing was
    written. summary adds fields to data.result beside path, bytes and sha256.
    timeout is the lease in seconds (default 120)."""

    path: str
    produce: Callable[[], bytes | str] | None
    summary: dict[str, Any] | None = None
    timeout: float = 0.0


def file_digest(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def link_new(tmp: str, path: str) -> None:
    """Put the file tmp at path without replacing anything: a hard link, or, where
    the file system has none, an exclusive create and a copy. Raises
    FileExistsError when path exists."""
    try:
        os.link(tmp, path)
        return
    except FileExistsError:
        raise
    except OSError:
        pass
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    try:
        with open(tmp, "rb") as src:
            for chunk in iter(lambda: src.read(1 << 16), b""):
                view = memoryview(chunk)
                while view:
                    n = os.write(fd, view)
                    view = view[n:]
    except BaseException:
        os.close(fd)
        with contextlib.suppress(OSError):
            os.remove(path)
        raise
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class OutputFile:
    """A local output file written in pieces inside a write's apply
    (App.open_output, Q8): a binary file object onto a temporary file beside the
    target. commit moves the file into place and abort removes it. As a context
    manager it aborts when the block ends without commit."""

    def __init__(self, app: App, node: Node, path: str, fd: int, tmp: str, overwrite: bool) -> None:
        self._app = app
        self._node = node
        self._path = path
        self._tmp = tmp
        self._fh = os.fdopen(fd, "wb")
        self._overwrite = overwrite
        self._size = 0
        self._sum = hashlib.sha256()
        self._done = False

    def __enter__(self) -> OutputFile:
        return self

    def __exit__(self, *exc: object) -> None:
        self.abort()

    def write(self, data: bytes | bytearray | memoryview) -> int:
        """Write bytes to the temporary file; returns how many were written."""
        if self._done:
            raise ValueError("the output file was already committed or aborted")
        view = memoryview(data).cast("B")
        n = self._fh.write(view)
        n = len(view) if n is None else n
        self._size += n
        self._sum.update(view[:n])
        return n

    @property
    def path(self) -> str:
        """The absolute path the file is committed to."""
        return self._path

    @property
    def size(self) -> int:
        """The number of bytes written so far."""
        return self._size

    def sha256(self) -> str:
        """The hex SHA-256 of the bytes written so far."""
        return self._sum.hexdigest()

    def abort(self) -> None:
        """Remove the temporary file; the target is left as it was. It is safe to
        call after commit."""
        if self._done:
            return
        self._done = True
        with contextlib.suppress(OSError):
            self._fh.close()
        with contextlib.suppress(OSError):
            os.remove(self._tmp)

    def commit(self) -> None:
        """Move the written file into place (Q8): new files only, unless the file
        was opened with overwrite and the command declares persist, and with the
        scope's outputs (new) or roots (existing) checked at this moment. Raises the
        Error; nothing was written then and the temporary file is gone."""
        if self._done:
            raise internal_error("The output file was already committed or aborted.")
        try:
            self._commit()
        finally:
            self.abort()

    def _commit(self) -> None:
        a, path, tmp = self._app, self._path, self._tmp
        try:
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._fh.close()
        except OSError as exc:
            raise self._write_failed(exc) from None
        m = a.meta_for(self._node)
        overwrite = self._overwrite and m is not None and m.has_effect("persist")
        exists = output_exists(path)
        e = a._existing_output_allowed(m, path, exists, overwrite)
        if e is not None:
            raise e
        if not exists:
            e = a._new_output_allowed(m, path)
            if e is not None:
                raise e
        if exists:
            try:
                rename_retry(tmp, path)
            except OSError as exc:
                raise self._write_failed(exc) from None
            self._done = True
            return
        try:
            link_new(tmp, path)
        except FileExistsError as exc:
            # The file appeared since the check: the same rules apply to it.
            e = a._existing_output_allowed(m, path, True, overwrite)
            if e is not None:
                raise e from None
            try:
                rename_retry(tmp, path)
            except OSError:
                raise self._write_failed(exc) from None
            self._done = True
        except OSError as exc:
            raise self._write_failed(exc) from None

    def _write_failed(self, exc: BaseException) -> Error:
        return io_error(
            "The output file could not be written; nothing was saved.",
            f"Check that {os.path.dirname(self._path)} is writable.",
            exc,
        )


def write_output_file(path: str, content: bytes, overwrite: bool) -> None:
    """content to path atomically: a synced temporary file in the same directory,
    then a hard link for a new file (it fails if the file appeared meanwhile) or a
    rename over an existing one."""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".aicli-output-", dir=d)
    try:
        try:
            view = memoryview(content)
            while view:
                n = os.write(fd, view)
                view = view[n:]
            os.fsync(fd)
        finally:
            os.close(fd)
        if overwrite:
            rename_retry(tmp, path)
            return
        try:
            os.link(tmp, path)
            return
        except FileExistsError:
            raise
        except OSError:
            pass
        # No hard links here: create the file exclusively instead.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        fd2 = os.open(path, flags, 0o644)
        try:
            view = memoryview(content)
            while view:
                n = os.write(fd2, view)
                view = view[n:]
            os.fsync(fd2)
        finally:
            os.close(fd2)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


class StreamMixin:
    def stream(self: App, cmd: Any) -> Stream:
        """Start an NDJSON stream for the running read command (CLI-006). Declare
        it in the command's summary and examples. A write command cannot stream;
        it reports through App.write or App.write_batch."""
        st, node = self._run_for(cmd)
        m = self.meta_for(node)
        if m is not None and m.is_write():
            raise internal_error(
                "A write command cannot stream; it reports through App.write or App.write_batch."
            )
        if st.stream is None:
            st.stream = Stream(self, st, node)
        return st.stream

    def item_schema_of(self: App, m: Meta | None) -> tuple[SchemaDef, bool]:
        """The schema of a stream's items (Q23): Meta.stream_item when it is set,
        else the command's output schema (own reports the latter)."""
        if m is not None and m.stream_item:
            return self._reg.schemas.get(m.stream_item, SchemaDef()), False
        return self.schema_of(m), True

    def _emit_stream(self: App, st: RunState, res: Any) -> int:
        """The summary line of a stream (the last line) and its exit code. A raw
        stream (--format raw) is the items' bytes only (Q23): no summary line; an
        error that ends it goes to stderr as text, and the exit code reports it."""
        s: Stream = st.stream
        stdout, stderr = self._stdout(), self._stderr()
        err = res.err
        if st.format == "raw":
            if err is None:
                return 0
            obj, code, err_untrusted = self.normalize_error(
                err if isinstance(err, Error) else internal_error(str(err))
            )
            meta = self._meta(st, clean_paths(err_untrusted))
            obj = fit_error_budget(obj, meta, self.tool_name())
            self._write(stderr, write_text_error(obj).encode("utf-8", "replace"))
            self._text_notices(st, stderr)
            return code
        if err is None:
            # The summary is what summary() recorded, else what the command reported
            # with ok(), else the number of items.
            data: Any = {"count": s.items}
            untrusted: list[str] = []
            if s.summary_set:
                try:
                    data, untrusted = (
                        to_generic_data(s.summary_data),
                        prefix_paths("data", s.summary_untrusted),
                    )
                except (TypeError, ValueError):
                    err = internal_error("The stream summary cannot be encoded as JSON.")
            elif st.out.set:
                data, untrusted = res.data, list(res.untrusted)
            if err is None:
                if st.format == "text":
                    self._write(stdout, write_text(data).encode("utf-8", "replace"))
                    self._text_notices(st, stderr)
                    return 0
                try:
                    paths = clean_paths(present_paths({"data": data}, untrusted))
                    self._write(stdout, stream_line("summary", data, self._meta(st, paths)))
                    return 0
                except (TypeError, ValueError):
                    err = internal_error("The stream summary cannot be encoded as JSON.")
        obj, code, err_untrusted = self.normalize_error(
            err if isinstance(err, Error) else internal_error(str(err))
        )
        meta = self._meta(st, clean_paths(present_paths(error_root(obj), err_untrusted)))
        obj = fit_error_budget(obj, meta, self.tool_name())
        kept = clean_paths(present_paths(error_root(obj), meta.get("untrusted", [])))
        if kept:
            meta["untrusted"] = kept
        else:
            meta.pop("untrusted", None)
        if st.format == "text":
            self._write(stderr, write_text_error(obj).encode("utf-8", "replace"))
            self._text_notices(st, stderr)
            return code
        try:
            line = go_json(
                Ordered(
                    ok=False, schema_version=SCHEMA_VERSION, type="summary", error=obj, meta=meta
                )
            )
        except (TypeError, ValueError):
            fallback = Ordered(
                code="E_UNKNOWN",
                message="The error could not be encoded as JSON.",
                retryable=False,
                details={
                    "dispatch_state": "not_started",
                    "next_action": "Report this bug in the tool.",
                },
            )
            line = go_json(
                Ordered(
                    ok=False,
                    schema_version=SCHEMA_VERSION,
                    type="summary",
                    error=fallback,
                    meta=Ordered(duration_ms=meta["duration_ms"]),
                )
            )
            code = CORE_CODES["E_UNKNOWN"].exit
        self._write(stdout, (line + "\n").encode("utf-8"))
        return code

    def write_output(self: App, cmd: Any, spec: OutputSpec) -> None:
        """Write a result to spec.path through the write gate (CLI-042, decision 20).
        The command declares the artifact effect (new files), persist (overwriting
        existing ones) or both. The path is bound into the token, checked against
        the scope's outputs and roots after symlink resolution, and the file is
        written atomically; its current content is the preview's base version, so a
        file that changes between the dry-run and the confirm is state_drift. The
        response's data.result carries path, bytes and sha256 (plus spec.summary)."""
        _, node = self._run_for(cmd)
        m = self.meta_for(node)
        if m is None or not m.has_effect("artifact", "persist"):
            raise internal_error(
                "App.write_output needs a command that declares the artifact or persist effect."
            )
        if spec.produce is None:
            raise internal_error("App.write_output needs OutputSpec.produce.")
        if not spec.path:
            e = usage("missing_parameter", "--output", "Give the file to write with --output.")
            e.details["next_action"] = "Add --output <file> and run the command again."
            raise e
        try:
            path = os.path.abspath(spec.path)
        except (OSError, ValueError):
            raise usage(
                "invalid_value", "--output", "The --output path is not a valid path."
            ) from None
        creates, overwrites = m.has_effect("artifact"), m.has_effect("persist")
        produce = spec.produce

        def state() -> tuple[bool, str]:
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                return False, "absent"
            except OSError:
                return True, "unreadable"
            if stat.S_ISDIR(st.st_mode):
                return True, "directory"
            if not stat.S_ISREG(st.st_mode):
                return True, "not_a_regular_file"
            try:
                return True, "sha256:" + file_digest(path)
            except OSError:
                return True, "unreadable"

        def appeared() -> BaseException:
            return not_dispatched(
                conflict_error(
                    "state_drift",
                    "The output file appeared or changed since the preview; nothing was written.",
                    "Run the command again with --dry-run and check the new preview.",
                )
            )

        # the file's state the last preview saw: at confirm, the re-check that runs
        # just before apply
        planned = [""]

        def preview() -> Preview:
            exists, version = state()
            planned[0] = version
            blocks = []
            if version == "directory":
                blocks.append(Block("is_directory", "The output path is a directory; name a file."))
            elif version in ("not_a_regular_file", "unreadable"):
                blocks.append(
                    Block(
                        "not_a_file", "The output path exists but is not a readable regular file."
                    )
                )
            elif exists and not overwrites:
                blocks.append(
                    Block(
                        "exists", "The output file exists and this command only creates new files."
                    )
                )
            elif not exists and not creates:
                blocks.append(
                    Block(
                        "missing",
                        "The output file does not exist and this command only overwrites files.",
                    )
                )
            action, note, source = "Write " + path, "creates a new file", "file"
            if exists:
                action, note, source = (
                    "Overwrite " + path,
                    "overwrites the existing file",
                    "file_sha256",
                )
            return Preview(
                action=action,
                changes=[FieldChange(field="path", after=path, note=note)],
                base_version=version,
                base_source=source,
                can_apply=not blocks,
                blocked_by=blocks,
                outputs=[path],
            )

        def apply(op: Op) -> Result:
            try:
                content = produce()
            except BaseException as exc:  # noqa: BLE001 - nothing was written
                raise not_dispatched(exc) from None
            data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
            # Checked again at write time (C1, Q9): a file that appeared since the
            # confirm's checks is an overwrite, which only a persist command may do
            # and the scope's roots must allow.
            exists, version = state()
            e = self._existing_output_allowed(m, path, exists, overwrites)
            if e is not None:
                raise not_dispatched(e)
            if version != planned[0]:
                raise appeared()
            try:
                write_output_file(path, data, exists)
            except FileExistsError:
                e = self._existing_output_allowed(m, path, True, overwrites)
                if e is not None:
                    raise not_dispatched(e) from None
                raise appeared() from None
            except OSError as exc:
                raise not_dispatched(
                    io_error(
                        "The output file could not be written; nothing was saved.",
                        f"Check that {os.path.dirname(path)} is writable.",
                        exc,
                    )
                ) from None
            result: dict[str, Any] = {
                "path": path,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            for k, v in to_generic(spec.summary or {}).items():
                result.setdefault(k, v)
            observed: dict[str, Any] = {"path": None}
            try:
                if stat.S_ISREG(os.stat(path).st_mode):
                    observed["path"], observed["bytes"] = path, os.stat(path).st_size
            except OSError:
                pass
            return Result(observed=observed, data=result)

        self.write(
            cmd,
            WriteSpec(
                target=Target(key=path, kind="file", label=os.path.basename(path)),
                preview=preview,
                apply=apply,
                timeout=spec.timeout,
            ),
        )

    def open_output(self: App, path: str, overwrite: bool = False) -> OutputFile:
        """Start a local output file inside the running write's apply (Q8, CLI-042),
        for content too large to hold in memory (an artifacts archive): a temporary
        file in the target's directory, mode 0600 on POSIX. commit moves it into
        place, never over an existing file unless overwrite is true and the command
        declares persist (otherwise Q9's E_VALIDATION), with the scope checked as
        write_output checks it at write time: links and junctions resolved, the
        scope's outputs for a new file and its roots for an existing one. abort, or
        a failed commit, removes the temporary file; raise commit's Error wrapped in
        not_dispatched, since nothing was written. Use it as a context manager
        (leaving the block without commit aborts), and name the path in
        Preview.outputs so the gate checks it at the dry-run and the confirm.
        write_output stays for small results built in memory."""
        st = self._run
        if st is None or st.cmd is None:
            raise internal_error(
                "App.open_output must be called from a write's apply, inside App.execute."
            )
        m = self.meta_for(st.cmd)
        if m is None or not m.has_effect("artifact", "persist"):
            raise internal_error(
                "App.open_output needs a command that declares the artifact or persist effect."
            )
        if not isinstance(path, str) or not path.strip():
            e = usage("missing_parameter", "--output", "Give the file to write with --output.")
            e.details["next_action"] = "Add --output <file> and run the command again."
            raise e
        try:
            target = os.path.abspath(path)
        except (OSError, ValueError):
            raise usage(
                "invalid_value", "--output", "The --output path is not a valid path."
            ) from None
        d = os.path.dirname(target)
        try:
            fd, tmp = tempfile.mkstemp(prefix=".aicli-output-", dir=d)
        except (OSError, ValueError) as exc:
            raise io_error(
                "The output file could not be started; nothing was written.",
                f"Check that {d} exists and is writable.",
                exc,
            ) from None
        return OutputFile(self, st.cmd, target, fd, tmp, bool(overwrite))

    def _existing_output_allowed(
        self: App, m: Meta | None, path: str, exists: bool, overwrites: bool
    ) -> Error | None:
        """The write-time check of an output file that exists (C1, Q9): a command
        that only creates files refuses it with Q9's E_VALIDATION, and an overwrite
        needs the scope's roots. A file that does not exist passes."""
        if not exists:
            return None
        if not overwrites:
            return create_only_exists(path)
        return self._overwrite_refusal(m, path)

    def _new_output_allowed(self: App, m: Meta | None, path: str) -> Error | None:
        """The write-time scope check of a new output file (Q8): links resolved, a
        junction refused (C2), the directory inside the scope's outputs."""
        from .scope import ScopeNeed

        try:
            state = self.state_dir_path()
        except Error as e:
            return e
        need = ScopeNeed(outputs=[path])
        if m is not None:
            need.effects, need.dangerous = m.effect_strings(), m.is_dangerous()
        try:
            self.check_scope(self.load_scope(state), need, None)
        except Error as e:
            return e
        return None

    def _overwrite_refusal(self: App, m: Meta | None, path: str) -> Error | None:
        """write_output's check at write time (C1): an existing file may be
        overwritten only under the scope's roots, after links are resolved. None
        when it may; otherwise E_FORBIDDEN with details.scope, nothing written."""
        from .scope import ScopeNeed, output_state, resolved_within

        try:
            sc = self.load_scope(self.ensure_state_dir())
        except Error as e:
            return e
        roots = sc.file.roots if sc.status == "granted" and sc.file is not None else []
        try:
            target, _ = output_state(path)
            if resolved_within(target, roots):
                return None
        except (OSError, ValueError):
            pass
        need = ScopeNeed(outputs=[path])
        if m is not None:
            need.effects, need.dangerous = m.effect_strings(), m.is_dangerous()
        status = "insufficient" if sc.status == "granted" else sc.status
        e = self.scope_refusal(sc, status, need, {"roots": (path,)})
        e.message = (
            "The output file already exists and the scope does not allow overwriting it; nothing "
            "was written."
        )
        e.details["next_action"] = (
            "Ask the user to add the directory to the scope's roots, or choose a new file name."
        )
        return e
