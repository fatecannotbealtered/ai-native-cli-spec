"""python -m notes: run the example CLI against its file-backed fake upstream."""

from __future__ import annotations

import os
import sys
import tempfile

from .cli import new_app
from .store import file_store


def main() -> None:
    path = os.environ.get("NOTES_UPSTREAM_FILE") or os.path.join(
        tempfile.gettempdir(), "aicli-notes-example.json"
    )
    sys.exit(new_app(file_store(path)).execute(sys.argv[1:]))


if __name__ == "__main__":
    main()
