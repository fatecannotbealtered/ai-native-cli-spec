"""Test setup for the harness: a throwaway eval config (no upstream, no secrets, no model) and harness/ on sys.path.

The harness reads its configuration when config.py is first imported, so this module sets EVAL_CONFIG before
any test imports a harness module. Run: python -m pytest -q tools/agent-eval/tests
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

EVAL_DIR = Path(__file__).resolve().parent.parent
CORPUS = EVAL_DIR.parent.parent / "template" / "common" / "contract" / "examples"
WORK = Path(tempfile.mkdtemp(prefix="agent-eval-tests-"))

for _k in [k for k in os.environ if k.startswith("EVAL_")]:
    del os.environ[_k]
(WORK / "eval.config.json").write_text(
    json.dumps({"work_dir": "work", "upstream_url": "http://127.0.0.1:9"}), encoding="utf-8")
os.environ["EVAL_CONFIG"] = str(WORK / "eval.config.json")
sys.path.insert(0, str(EVAL_DIR / "harness"))


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(WORK, ignore_errors=True)


def corpus(name: str, **changes) -> bytes:
    """A contract corpus example as a tool prints it: compact JSON and one newline."""
    doc = json.loads((CORPUS / name).read_text(encoding="utf-8"))
    doc.update(changes)
    return (json.dumps(doc, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


class Trial:
    """A trial folder as the recorder leaves it: calls.jsonl and the raw outputs under calls/."""

    def __init__(self, root: Path):
        self.root = root
        (root / "calls").mkdir(parents=True, exist_ok=True)
        self.n = 0

    def call(self, argv: list, exit: int = 0, stdout: bytes | None = b"", **extra) -> None:
        self.n += 1
        cid = f"{1000 + self.n}-1"
        rec = {"id": cid, "start": "2026-10-14T08:00:00Z", "duration_ms": 5, "argv": argv, "exit": exit,
               "stdout_bytes": len(stdout or b""), "stderr_bytes": 0, "stdout_sha256": "", **extra}
        with open(self.root / "calls.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        if stdout is not None:
            (self.root / "calls" / f"{cid}.out").write_bytes(stdout)

    def calls(self) -> list:
        import recorded
        return recorded.read_calls(self.root)


@pytest.fixture
def trial(tmp_path) -> Trial:
    return Trial(tmp_path / "trial")


class Upstream:
    """A fake upstream on 127.0.0.1: answers GET with 200 and writes with 201 (or the status set for a path),
    and records every request it received, body included."""

    def __init__(self):
        self.seen: list = []
        self.status: dict = {}
        self.headers: dict = {}
        up = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _serve(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                path = self.path.split("?", 1)[0]
                up.seen.append({"method": self.command, "path": self.path, "body": body,
                                "host": self.headers.get("Host"),
                                "transfer_encoding": self.headers.get("Transfer-Encoding")})
                code = up.status.get(path, 200 if self.command in ("GET", "HEAD") else 201)
                data = json.dumps({"method": self.command, "path": self.path}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                for k, v in up.headers.items():
                    self.send_header(k, v.replace("{origin}", up.url))
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)

            do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = _serve

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def upstream():
    u = Upstream()
    yield u
    u.close()


@pytest.fixture(scope="session")
def eval_files():
    """arms.json with a 1.x arm A and a 2.0 arm N, their (fake) binaries and Skills, and a (fake) recorder."""
    from config import CFG, EXE
    import tool
    work = CFG.work_dir
    for arm in ("A", "N"):
        (work / "arms" / arm / "skill").mkdir(parents=True, exist_ok=True)
        (work / "arms" / arm / "skill" / "SKILL.md").write_text("---\nname: gitlab-cli\n---\n", encoding="utf-8")
        (work / "arms" / arm / "gitlab-cli").write_bytes(b"binary")
    arms = {"A": {"real_bin": "arms/A/gitlab-cli", "skill_dir": "arms/A/skill", "sha256": ""},
            "N": {"real_bin": "arms/N/gitlab-cli", "skill_dir": "arms/N/skill", "sha256": "", "spec": "2.0"}}
    (work / "arms.json").write_text(json.dumps(arms), encoding="utf-8")
    CFG.bin_dir.mkdir(parents=True, exist_ok=True)
    (CFG.bin_dir / (tool.NAME + EXE)).write_bytes(b"recorder")
    return work


def load_plan(batch: str) -> dict:
    from config import CFG
    return json.loads((CFG.runs_dir / batch / "plan.json").read_text(encoding="utf-8"))


def planned_trials(batch: str) -> dict:
    """(task, arm) -> the plan.json trial."""
    return {(t["task"], t["arm"]): t for lane in load_plan(batch)["lanes"] for t in lane["trials"]}
