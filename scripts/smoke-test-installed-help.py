#!/usr/bin/env python3
"""Require the installed petcam -h output to match the public CLI contract exactly."""

from __future__ import annotations

import os
import subprocess
import sys

LAUNCHER = "/usr/local/bin/petcam"
EXPECTED_STDOUT = """usage: petcam [-h] [-c SOURCE;RESOLUTION;FPS] [--configure]

Configure a USB or CSI camera and serve its native H.264 stream over RTSP.

options:
  -h, --help            show this help message and exit
  -c SOURCE;RESOLUTION;FPS
                        apply a camera mode directly (for example: -c
                        "USB;1920x1080;30")
  --configure           run guided setup and apply a new persistent stream
                        configuration

Examples:
  petcam                 # show the saved stream status
  petcam --configure     # choose a new camera or audio mode
  petcam -c "USB;1920x1080;30"
"""


def main() -> int:
    environment = os.environ.copy()
    for name in ("PYTHONPATH", "PETCAM_ROOT", "PETCAM_INSTALL_ROOT"):
        environment.pop(name, None)
    environment.update({
        "LC_ALL": "C",
        "LANG": "C",
        "COLUMNS": "80",
        "TERM": "dumb",
        "PYTHONIOENCODING": "utf-8",
    })

    try:
        result = subprocess.run(
            [LAUNCHER, "-h"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"installed petcam -h could not execute: {error}", file=sys.stderr)
        return 1

    if result.returncode != 0 or result.stderr or result.stdout != EXPECTED_STDOUT:
        print("installed petcam -h did not match the exact expected output.", file=sys.stderr)
        print(f"exit status: {result.returncode}", file=sys.stderr)
        print(f"expected stdout: {EXPECTED_STDOUT!r}", file=sys.stderr)
        print(f"actual stdout:   {result.stdout!r}", file=sys.stderr)
        print(f"actual stderr:   {result.stderr!r}", file=sys.stderr)
        return 1

    print("installed /usr/local/bin/petcam -h matched the exact expected output")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
