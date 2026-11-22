from __future__ import annotations

import os
import pty
import select
import signal
import sys
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(hasattr(pty, "fork"), "requires POSIX pseudo-terminals")
class InteractiveLauncherTests(unittest.TestCase):
    def test_launcher_enters_and_remains_in_configuration_ui(self):
        child_pid, terminal_fd = pty.fork()
        if child_pid == 0:
            environment = os.environ.copy()
            environment.update({
                "PETCAM_ROOT": str(ROOT),
                "PYTHONPATH": str(ROOT / "src"),
                "TERM": "xterm",
                "LANG": "C.UTF-8",
            })
            launcher = ROOT / "petcam"
            os.execve(str(launcher), [str(launcher)], environment)
            raise AssertionError("execve returned unexpectedly")

        output = bytearray()
        status = None
        deadline = time.monotonic() + 5
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([terminal_fd], [], [], 0.1)
                if ready:
                    try:
                        output.extend(os.read(terminal_fd, 65536))
                    except OSError:
                        break
                waited_pid, waited_status = os.waitpid(child_pid, os.WNOHANG)
                if waited_pid == child_pid:
                    status = waited_status
                    break
                if b"Choose capture mode" in output:
                    break

            self.assertTrue(
                b"Choose capture mode" in output,
                "launcher did not render the configuration menu; output was "
                + repr(output.decode("utf-8", "replace")),
            )
            waited_pid, waited_status = os.waitpid(child_pid, os.WNOHANG)
            self.assertEqual(
                waited_pid,
                0,
                "launcher returned immediately instead of remaining interactive",
            )
        finally:
            if status is None:
                try:
                    os.kill(child_pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
                end = time.monotonic() + 3
                while time.monotonic() < end:
                    waited_pid, waited_status = os.waitpid(child_pid, os.WNOHANG)
                    if waited_pid == child_pid:
                        status = waited_status
                        break
                    time.sleep(0.05)
                if status is None:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    os.waitpid(child_pid, 0)
            os.close(terminal_fd)


if __name__ == "__main__":
    unittest.main()
