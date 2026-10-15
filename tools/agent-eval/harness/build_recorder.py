"""Build the recorder (harness/recorder) into <work_dir>/bin/<tool>[.exe].

The path of the recorder's private config (<work_dir>/.secrets/recorder.json, written by seed.py) is compiled
in, so it appears in neither the agent's environment nor its workspace. Rebuild after changing work_dir.

Usage: python harness/build_recorder.py [--go <go command>]
"""

from __future__ import annotations

import argparse
import subprocess
import sys

import tool
from config import CFG, EXE, HARNESS_DIR


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--go", default="go", help="the go command (default: go on PATH)")
    a = ap.parse_args()
    private = CFG.secrets_dir / "recorder.json"
    if '"' in str(private):
        print(f"cannot compile in {private}: the path contains a double quote", file=sys.stderr)
        return 2
    out = CFG.bin_dir / (tool.NAME + EXE)
    CFG.bin_dir.mkdir(parents=True, exist_ok=True)
    cmd = [a.go, "build", "-trimpath", "-ldflags", f'-X "main.secretsPath={private}"', "-o", str(out), "."]
    r = subprocess.run(cmd, cwd=HARNESS_DIR / "recorder")
    if r.returncode != 0:
        return r.returncode
    print(f"recorder built: {out} (private config: {private})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
