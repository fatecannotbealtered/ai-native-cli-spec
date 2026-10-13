"""Gap 3: every recorded stdout of a 2.0 arm is validated against the contract (VER-002) with aicli's validator,
and the violations are reported per trial without failing the collection."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import collect
import recorded
from config import CFG
from conftest import EVAL_DIR, corpus

STREAM = (b'{"ok":true,"schema_version":"2.0","type":"item","data":{"time":"2026-10-12T08:00:01Z","line":"Running '
          b'tests"},"meta":{"duration_ms":40,"untrusted":["data.line"]}}\n'
          b'{"ok":true,"schema_version":"2.0","type":"summary","data":{"count":1,"has_more":false},'
          b'"meta":{"duration_ms":41}}\n')


def test_valid_outputs_pass(trial):
    update = ["mr", "update", "acme/web!5", "--assignee", "carol"]
    trial.call(update + ["--dry-run"], 0, corpus("valid/envelope-dry-run.json"))
    trial.call(update, 5, corpus("valid/envelope-confirmation-required.json"))
    trial.call(["mr", "get", "acme/web!99"], 3, corpus("valid/envelope-error-not-found.json"))
    trial.call(["job", "log", "88231", "--follow"], 0, STREAM)
    assert recorded.check_envelopes(trial.calls()) == {"checked": 4, "invalid": 0, "skipped": 0, "problems": []}


def test_broken_envelope_is_reported_with_the_call(trial):
    broken = json.loads(corpus("valid/envelope-dry-run.json"))
    del broken["meta"]
    trial.call(["project", "get", "acme/web"], 0, corpus("valid/envelope-mr-get.json"))
    trial.call(["mr", "update", "acme/web!5", "--dry-run"], 0, json.dumps(broken).encode() + b"\n")
    trial.call(["mr", "get", "acme/web!5"], 0, corpus("valid/envelope-mr-get.json", schema_version="1.0"))
    trial.call(["issue", "list"], 0, b"")
    trial.call(["job", "log", "1", "--follow"], 0, STREAM.split(b"\n")[0] + b"\n")  # a stream without its summary
    out = recorded.check_envelopes(trial.calls())
    assert (out["checked"], out["invalid"]) == (5, 4)
    first, second, third, fourth = out["problems"]
    assert first.startswith("1002-1 `mr update acme/web!5 --dry-run` (exit 0): ") and 'missing key "meta"' in first
    assert "schema_version" in second
    assert "stdout is empty" in third
    assert "summary" in fourth


def test_exit_code_must_match_the_envelope(trial):
    trial.call(["mr", "get", "acme/web!99"], 1, corpus("valid/envelope-error-not-found.json"))
    trial.call(["mr", "get", "acme/web!5"], 2, corpus("valid/envelope-mr-get.json"))
    trial.call(["job", "log", "1", "--follow"], 7, STREAM)  # the summary line says ok
    out = recorded.check_envelopes(trial.calls())
    assert out["invalid"] == 3
    assert "exit 1, but E_NOT_FOUND means exit 3" in out["problems"][0]
    assert "exit 2, but a success envelope means exit 0" in out["problems"][1]
    assert "exit 7, but a success envelope means exit 0" in out["problems"][2]


def test_problem_list_is_capped(trial):
    for i in range(8):
        trial.call(["issue", "get", str(i)], 0, b"not json\n")
    out = recorded.check_envelopes(trial.calls())
    assert out["invalid"] == 8 and len(out["problems"]) == recorded.PROBLEMS_KEPT


def test_help_and_text_output_are_skipped(trial):
    trial.call(["mr", "--help"], 0, b"Usage: gitlab-cli mr <command>\n")
    trial.call(["help", "mr"], 0, b"Usage: gitlab-cli mr <command>\n")
    trial.call(["mr", "get", "acme/web!5", "--format", "text"], 0, b"!5 Add retry (opened)\n")
    trial.call(["job", "log", "1", "--format=raw"], 0, b'{"a log line that is JSON": true}\n')
    trial.call(["mr", "get", "acme/web!5", "--format", "text"], 3, corpus("valid/envelope-error-not-found.json"))
    trial.call(["mr", "get", "x"], 127, b"", error="exec: not found")
    out = recorded.check_envelopes(trial.calls())
    assert (out["skipped"], out["checked"], out["invalid"]) == (5, 1, 0)  # a text-mode error is still an envelope


def test_reported_for_2_0_arms_only(trial):
    trial.call(["mr", "get", "acme/web!5"], 0, b'{"ok":true}\n')
    _, fields_2, _ = collect.spec_checks({"spec": "2.0"}, trial.calls())
    assert fields_2["envelopes"]["invalid"] == 1
    _, fields_1, _ = collect.spec_checks({"spec": "1.x"}, trial.calls())
    assert fields_1["envelopes"] is None
    _, fields_old, _ = collect.spec_checks({}, trial.calls())  # a plan.json written before the 2.0 features
    assert fields_old["envelopes"] is None and fields_old["spec"] == "1.x"


def test_a_missing_validator_is_reported_not_raised(trial, tmp_path, monkeypatch):
    trial.call(["mr", "get", "acme/web!5"], 0, corpus("valid/envelope-mr-get.json"))
    monkeypatch.setattr(recorded, "_validator", None)
    monkeypatch.setitem(CFG._values, "aicli_dir", tmp_path / "nowhere")
    out = recorded.check_envelopes(trial.calls())
    assert out["checked"] == 0 and "validator unavailable" in out["error"] and "aicli_dir" in out["error"]


def test_validator_loads_without_click():
    """aicli's package __init__ imports click; the harness loads validate.py without it (standard library only)."""
    code = ("import sys\n"
            "class Block:\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name.split('.')[0] == 'click': raise ImportError('click is not installed')\n"
            "sys.meta_path.insert(0, Block())\n"
            f"sys.path.insert(0, {str(EVAL_DIR / 'harness')!r})\n"
            "import recorded\n"
            "v = recorded.validator()\n"
            "print(v.__name__, 'click' in sys.modules)\n")
    env = {**os.environ, "EVAL_CONFIG": os.environ["EVAL_CONFIG"]}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["_agent_eval_aicli.validate", "False"]
