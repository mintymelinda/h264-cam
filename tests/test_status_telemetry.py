from __future__ import annotations

import builtins
import importlib.util
import socket
import threading
import unittest
from io import StringIO
from unittest.mock import patch

from petcam.infrastructure.streaming.gstreamer import GStreamerError, GStreamerRtspServer, validate_runtime
from petcam.infrastructure.system.metrics import ResourceMetrics


class GStreamerRtspTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("gi"), "GStreamer Python bindings are not installed")
    def test_rtsp_server_answers_options_and_describe(self):
        try:
            validate_runtime()
        except GStreamerError as error:
            self.skipTest(str(error))

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        media_configured = threading.Event()
        server = GStreamerRtspServer(
            port=port,
            on_media_configure=lambda _appsrc: media_configured.set(),
        )
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=3) as client:
                client.settimeout(3)
                client.sendall(
                    f"OPTIONS rtsp://127.0.0.1:{port}/petcam RTSP/1.0\\r\\n"
                    "CSeq: 1\\r\\nUser-Agent: PetCam-test\\r\\n\\r\\n".encode("ascii")
                )
                options_response = client.recv(4096)
                self.assertIn(b"RTSP/1.0 200", options_response)

                client.sendall(
                    f"DESCRIBE rtsp://127.0.0.1:{port}/petcam RTSP/1.0\\r\\n"
                    "CSeq: 2\\r\\nAccept: application/sdp\\r\\n"
                    "User-Agent: PetCam-test\\r\\n\\r\\n".encode("ascii")
                )
                describe_response = client.recv(8192)

            self.assertIn(b"RTSP/1.0 200", describe_response)
            self.assertIn(b"application/sdp", describe_response.lower())
            self.assertIn(b"m=video", describe_response)
            self.assertTrue(media_configured.wait(3), "DESCRIBE did not configure the GStreamer media pipeline")
        finally:
            server.stop()


class ResourceMetricsTests(unittest.TestCase):
    def test_resource_metrics_sample_changes_between_intervals(self):
        proc_stat = [
            "cpu 100 0 0 100 0 0 0 0\n",
            "cpu 150 0 0 150 0 0 0 0\n",
        ]
        meminfo = "MemTotal:       438272 kB\nMemAvailable:   327680 kB\n"
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
