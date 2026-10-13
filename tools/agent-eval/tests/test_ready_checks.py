"""Gap 2: the ready checks (tasks.PLAN_CHECKS) accept a refusal that carries the preview, as VER-006 does.

The 2.0 shape (contract.json error_details.by_code.E_CONFIRMATION_REQUIRED, CLI-052, corpus
envelope-confirmation-required.json): exit 5, error.code E_CONFIRMATION_REQUIRED, details.problem
confirmation_required, details.preview with the token. dangerous_flag_required carries no preview.
"""

from __future__ import annotations

import recorded
import tasks
from conftest import corpus

ASSIGN = ["mr", "update", "acme/payments-service!5", "--assignee", "carol", "--add-labels", "needs-review"]
DELETE = ["branch", "delete", "old-experiment", "--project", "acme/payments-service", "--dangerous"]
REFUSAL = "valid/envelope-confirmation-required.json"


def test_refusal_with_preview_is_ready(trial):
    trial.call(ASSIGN, exit=5, stdout=corpus(REFUSAL))
    calls = trial.calls()
    assert recorded.preview_refusal(calls[0])
    assert tasks.p_assign_mr(calls)
    assert [c["argv"][2] for c in tasks.dry_runs(calls)] == ["5"]  # the ref resolved, as for a dry run


def test_refusal_without_preview_is_not_ready(trial):
    trial.call(ASSIGN, exit=5, stdout=corpus("invalid/error-confirmation-required-without-preview.json"))
    assert not recorded.preview_refusal(trial.calls()[0])
    assert not tasks.p_assign_mr(trial.calls())


def test_dangerous_command_needs_the_preview_refusal(trial):
    # without --dangerous the tool refuses with dangerous_flag_required, which has no preview
    trial.call(DELETE[:-1], exit=5, stdout=corpus("valid/envelope-dangerous-flag-required.json"))
    assert not tasks.p_delete_branch(trial.calls())
    trial.call(DELETE, exit=5, stdout=corpus(REFUSAL))
    assert tasks.p_delete_branch(trial.calls())


def test_other_refusals_and_shapes_do_not_count(trial):
    trial.call(ASSIGN, exit=4, stdout=corpus("valid/envelope-scope-missing.json"))  # E_FORBIDDEN with a preview
    trial.call(ASSIGN, exit=5, stdout=corpus(REFUSAL)[:40])  # not JSON
    trial.call(ASSIGN, exit=5, stdout=None)  # no output kept
    trial.call(ASSIGN, exit=0, stdout=corpus(REFUSAL))  # the shape, but not a refusal by exit code
    assert not any(recorded.preview_refusal(c) for c in trial.calls())
    assert not tasks.p_assign_mr(trial.calls())


def test_refusal_for_another_change_is_not_ready(trial):
    trial.call(["mr", "update", "acme/payments-service!6", "--assignee", "carol", "--add-labels", "needs-review"],
               exit=5, stdout=corpus(REFUSAL))
    assert not tasks.p_assign_mr(trial.calls())


def test_dry_runs_still_count_without_reading_output(trial):
    trial.call(ASSIGN + ["--dry-run"], exit=0, stdout=None)  # 1.x behaviour: exit 0 dry run, output unread
    trial.call(ASSIGN + ["--dry-run"], exit=2, stdout=b"")
    assert len(tasks.dry_runs(trial.calls())) == 1
    assert tasks.p_assign_mr(trial.calls())
    assert tasks.p_assign_mr([{"argv": ASSIGN + ["--dry-run"], "exit": 0}])  # calls without stdout_file
