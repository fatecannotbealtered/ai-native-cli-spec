"""A typer tool on the library: typer builds the click tree (typer.main.get_command),
aicli does the rest. The conformance suite and the gate's key behaviours run on it."""

from __future__ import annotations

import io
import json
import threading
from pathlib import Path
from typing import Any

import pytest

import aicli
from aicli import (
    CHANGE,
    OBSERVE,
    FieldChange,
    Meta,
    Op,
    Preview,
    Result,
    SchemaDef,
    Target,
    WriteSpec,
)

typer = pytest.importorskip("typer")


class Tasks:
    """The fake upstream: tasks with a done flag, counting write requests."""

    def __init__(self) -> None:
        self.mu = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self.mu:
            self.items = {
                "1": {"id": "1", "title": "Write the report", "done": False, "version": 1}
            }
            self.n = 0

    def writes(self) -> int:
        with self.mu:
            return self.n


def build(store: Tasks, env: dict[str, str] | None = None) -> aicli.App:
    t = typer.Typer(add_completion=False, help="Track tasks")
    holder: dict[str, aicli.App] = {}

    @t.command("list")
    def list_tasks(limit: int = typer.Option(20, help="Tasks to return")) -> None:
        items = sorted(store.items.values(), key=lambda x: x["id"])[:limit]
        holder["app"].list(None, aicli.Page(items=items, sort="id_asc"))

    @t.command("done")
    def done(task_id: str = typer.Option("", "--id", help="Task id")) -> None:
        a = holder["app"]

        def preview() -> Preview:
            task = store.items.get(task_id)
            if task is None:
                raise aicli.not_found("No task has that id.", "Run `tasks list` to find it.")
            return Preview(
                action=f"Mark task {task_id} done",
                can_apply=True,
                base_version=str(task["version"]),
                changes=[FieldChange("done", before=task["done"], after=True)],
            )

        def apply(op: Op) -> Result:
            with store.mu:
                store.n += 1
                task = store.items[task_id]
                task["done"], task["version"] = True, task["version"] + 1
                return Result(observed={"done": task["done"]}, data=dict(task))

        a.write(
            None, WriteSpec(target=Target("tasks/" + task_id, "task"), preview=preview, apply=apply)
        )

    root = typer.main.get_command(t)
    root.name = "tasks"
    app = aicli.App(name="tasks", version="0.1.0", risk_tier="T1", root=root, skill_dirs=[])
    if env is not None:
        app.env = lambda k: env.get(k, "")
    holder["app"] = app
    app.schema(
        "task",
        SchemaDef(shape="object", fields=["id", "title", "done", "version"], untrusted=["title"]),
    )
    app.schema(
        "task_page",
        SchemaDef(shape="page", fields=["id", "title", "done", "version"], untrusted=["title"]),
    )
    app.register(
        root.commands["list"],
        Meta(
            summary="List tasks",
            effects=[OBSERVE],
            output="task_page",
            examples=["tasks list --limit 5"],
        ),
    )
    app.register(
        root.commands["done"],
        Meta(
            summary="Mark a task done",
            effects=[CHANGE],
            output="task",
            positional="id",
            error_codes=["E_NOT_FOUND"],
            examples=["tasks done 1 --dry-run", "tasks done 1 --confirm <token>"],
        ),
    )
    return app


def test_typer_tool_passes_the_conformance_suite() -> None:
    store = Tasks()
    aicli.run_conformance(
        aicli.Harness(
            new_app=lambda env: build(store, env),
            writes=store.writes,
            reset=store.reset,
            samples={"done": ["1"]},
        )
    )


def run(store: Tasks, state: Path, scope: Path, *args: str) -> dict[str, Any]:
    app = build(store)
    app.state_dir = str(state)
    app.env = lambda k: str(scope) if k == "TASKS_SCOPE_FILE" else ""
    out = io.BytesIO()
    app.stdout = app.stderr = out
    code = app.execute(list(args))
    aicli.validate_envelope(out.getvalue())
    doc = json.loads(out.getvalue())
    doc["exit"] = code
    return doc


def test_typer_tool_goes_through_the_gate(tmp_path: Path) -> None:
    store = Tasks()
    scope = tmp_path / "scope.json"
    scope.write_text(
        json.dumps(
            {
                "scope_id": "scp_t",
                "tool": "tasks",
                "granted_by": "tester",
                "granted_at": "2026-01-01T00:00:00Z",
                "expires_at": None,
                "effects": ["change"],
                "dangerous": False,
            }
        ),
        encoding="utf-8",
    )
    state = tmp_path / "state"
    refused = run(store, state, scope, "done", "1")
    assert refused["exit"] == 5 and refused["error"]["code"] == "E_CONFIRMATION_REQUIRED", refused
    tok = refused["error"]["details"]["confirm_token"]
    other = run(store, state, scope, "done", "--id", "2", "--confirm", tok)
    assert other["error"]["details"]["conflict"] == "token_mismatch" and store.writes() == 0, other
    ok = run(store, state, scope, "done", "--id", "1", "--confirm", tok)
    assert ok["exit"] == 0 and ok["data"]["receipt"]["outcome"]["verification"] == "passed", ok
    assert store.writes() == 1
    again = run(store, state, scope, "done", "1", "--confirm", tok)
    assert again["error"]["details"]["conflict"] == "token_used", again
    listed = run(store, state, scope, "list", "--limit", "1")
    assert listed["exit"] == 0 and listed["data"]["items"][0]["done"] is True, listed
    unknown = run(store, state, scope, "dne", "1")
    assert unknown["error"]["details"]["did_you_mean"] == ["tasks done"], unknown
