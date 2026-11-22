from __future__ import annotations

import builtins
import importlib.util
import socket
import time
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

        media_configured = []
        server = GStreamerRtspServer(
            port=port,
            on_media_configure=lambda appsrc: media_configured.append(appsrc),
        )

        def exchange(client, request):
            client.sendall(request.encode("ascii"))
            response = bytearray()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                server.iterate()
                try:
                    chunk = client.recv(8192)
                    if chunk:
                        response.extend(chunk)
                except BlockingIOError:
                    pass
                header_end = response.find(b"\r\n\r\n")
                if header_end >= 0:
                    headers = bytes(response[:header_end]).lower()
                    content_length = 0
                    for line in headers.split(b"\r\n")[1:]:
                        if line.startswith(b"content-length:"):
                            content_length = int(line.partition(b":")[2].strip())
                            break
                    if len(response) >= header_end + 4 + content_length:
                        return bytes(response)
                time.sleep(0.002)
            self.fail(f"Timed out waiting for RTSP response: {bytes(response)!r}")

        try:
            with socket.create_connection(("127.0.0.1", port), timeout=3) as client:
                client.setblocking(False)
                options = exchange(
                    client,
                    f"OPTIONS rtsp://127.0.0.1:{port}/petcam RTSP/1.0\r\n"
                    "CSeq: 1\r\nUser-Agent: PetCam-test\r\n\r\n",
                )
                self.assertIn(b"RTSP/1.0 200", options)

                describe = exchange(
                    client,
                    f"DESCRIBE rtsp://127.0.0.1:{port}/petcam RTSP/1.0\r\n"
                    "CSeq: 2\r\nAccept: application/sdp\r\n"
                    "User-Agent: PetCam-test\r\n\r\n",
                )
                self.assertIn(b"RTSP/1.0 200", describe)
                self.assertIn(b"application/sdp", describe.lower())
                self.assertIn(b"m=video", describe)
            self.assertTrue(media_configured, "DESCRIBE did not configure the GStreamer media pipeline")
            self.assertIsNotNone(media_configured[0])
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
