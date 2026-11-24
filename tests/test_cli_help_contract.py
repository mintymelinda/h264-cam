from __future__ import annotations

import os
import runpy
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELP_SMOKE_TEST = ROOT / "scripts" / "smoke-test-installed-help.py"


class CliHelpContractTests(unittest.TestCase):
    def test_expected_installed_help_matches_the_current_cli(self):
        expected = runpy.run_path(str(HELP_SMOKE_TEST))["EXPECTED_STDOUT"]
        environment = os.environ.copy()
        environment.update({
            "PYTHONPATH": str(ROOT / "src"),
            "LC_ALL": "C",
            "LANG": "C",
            "COLUMNS": "80",
            "TERM": "dumb",
            "PYTHONIOENCODING": "utf-8",
        })

        result = subprocess.run(
            [sys.executable, "-m", "petcam.cli", "-h"],
            cwd=ROOT,
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

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, expected)


if __name__ == "__main__":
    unittest.main()
