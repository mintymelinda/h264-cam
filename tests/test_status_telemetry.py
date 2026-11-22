from __future__ import annotations

import builtins
import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from petcam.infrastructure.streaming import live555
from petcam.infrastructure.system.metrics import ResourceMetrics


class StreamStatusTelemetryTests(unittest.TestCase):
    def test_status_persists_live_client_and_delivery_counters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            status_path = run_dir / "stream-status.json"
            with (
                patch.object(live555, "RUN_DIR", run_dir),
                patch.object(live555, "STREAM_STATUS", status_path),
            ):
                live555.write_status(
                    "ready",
                    {"backend": "usb", "resolution": "1920x1080", "fps": 30},
                    rtsp_clients=2,
                    streamed_nals_per_second=123.46,
                )

            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["state"], "ready")
            self.assertEqual(status["rtsp_clients"], 2)
            self.assertEqual(status["streamed_nals_per_second"], 123.5)
            self.assertNotIn("streamed_fps", status)
            self.assertGreater(status["updated_at"], 0)
            self.assertFalse(status_path.with_name(status_path.name + ".tmp").exists())

    def test_resource_metrics_sample_changes_between_intervals(self) -> None:
        proc_stat = [
            "cpu 100 0 0 100 0 0 0 0\n",
            "cpu 150 0 0 150 0 0 0 0\n",
        ]
        meminfo = (
            "MemTotal:       438272 kB\nMemAvailable:   327680 kB\n"
        )
        responses = iter([proc_stat[0], meminfo, proc_stat[1], meminfo])

        def fake_open(*args, **kwargs):
            return StringIO(next(responses))

        metrics = ResourceMetrics()
        with patch.object(builtins, "open", side_effect=fake_open):
            first = metrics.sample()
            second = metrics.sample()

        self.assertIsNone(first["cpu_percent"])
        self.assertEqual(second["cpu_percent"], 50.0)
        self.assertEqual(second["memory_used_mb"], 108)
        self.assertEqual(second["memory_total_mb"], 428)


if __name__ == "__main__":
    unittest.main()
