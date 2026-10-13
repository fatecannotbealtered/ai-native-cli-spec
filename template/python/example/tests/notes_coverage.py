"""Where the notes example tests record the commands they run (NOTES_COVERAGE_FILE,
VER-003; test_notes.py checks it at the end, R06) and the envelopes they saw (dumped
for check-contract.py when AICLI_DUMP_ENVELOPES is set), and the local stand-in for
the GitHub API the update built-in talks to (doctor refreshes the update notice), so
the tests never use the network."""

from __future__ import annotations

import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

COVERAGE_DIR = tempfile.mkdtemp(prefix="notes-coverage-")
COVERAGE_FILE = str(Path(COVERAGE_DIR) / "coverage.jsonl")

# every envelope the example tests produced (each also passed validate_envelope)
ENVELOPES: list[bytes] = []
STREAMS: list[bytes] = []


class _NoReleases(BaseHTTPRequestHandler):
    """A GitHub API that knows no release (404 for every request)."""

    def do_GET(self) -> None:
        body = b'{"message":"Not Found"}'
        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


_SERVER = ThreadingHTTPServer(("127.0.0.1", 0), _NoReleases)
threading.Thread(target=_SERVER.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
FAKE_API = f"http://127.0.0.1:{_SERVER.server_address[1]}"
