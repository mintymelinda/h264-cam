#!/usr/bin/env python3
"""Verify the installed PetCam command opens and remains in its interactive setup UI."""

from __future__ import annotations

import os
import pty
import select
import signal
import sys
import time

LAUNCHER = "/usr/local/bin/petcam"
EXPECTED = b"Choose capture mode"
START_TIMEOUT = 8.0
STABLE_UI_SECONDS = 1.5


def main() -> int:
    if not hasattr(pty, "fork"):
        print("installed launcher smoke test requires POSIX pseudo-terminals", file=sys.stderr)
        return 2

    pid, master_fd = pty.fork()
    if pid == 0:
        environment = os.environ.copy()
        environment.update({
            "TERM": "xterm-256color",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        })
        os.execve(LAUNCHER, [LAUNCHER], environment)
        raise AssertionError("execve returned unexpectedly")

    output = bytearray()
    status = None
    deadline = time.monotonic() + START_TIMEOUT
    try:
        while time.monotonic() < deadline and EXPECTED not in output:
            readable, _, _ = select.select([master_fd], [], [], 0.1)
            if readable:
                try:
                    chunk = os.read(master_fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)

            waited, child_status = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                status = child_status
                break

        if EXPECTED not in output:
            detail = output.decode("utf-8", "replace")
            print(
                "installed petcam did not render its setup menu; "
                f"exit={status!r}; terminal output={detail!r}",
                file=sys.stderr,
            )
            return 1

        stable_deadline = time.monotonic() + STABLE_UI_SECONDS
        while time.monotonic() < stable_deadline:
            readable, _, _ = select.select([master_fd], [], [], 0.1)
            if readable:
                try:
                    chunk = os.read(master_fd, 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                output.extend(chunk)
            waited, child_status = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                status = child_status
                break

        if status is not None:
            print(
                f"installed petcam exited after rendering its menu: status={status}",
                file=sys.stderr,
            )
            return 1

        print("installed /usr/local/bin/petcam rendered its setup menu and remained interactive")
        return 0
    finally:
        if status is None:
            try:
                os.kill(pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            end = time.monotonic() + 2.0
            while time.monotonic() < end:
                waited, child_status = os.waitpid(pid, os.WNOHANG)
                if waited == pid:
                    status = child_status
                    break
                time.sleep(0.05)
            if status is None:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.waitpid(pid, 0)
        os.close(master_fd)


if __name__ == "__main__":
    raise SystemExit(main())
