from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from petcam.cli import (
    activate_stream_service,
    load_saved_stream_configuration,
    save_stream_configuration,
)
from petcam.infrastructure.streaming.live555 import load_camera_status


class PersistentServiceConfigurationTests(unittest.TestCase):
    def test_saved_configuration_is_loaded_without_a_live_cli_process(self):
        config = {
            "source": "USB",
            "device": "/dev/video0",
            "physical_path": "/sys/devices/pci0000:00/usb1/1-1",
            "resolution": "1280x720",
            "fps": 30,
            "audio": {"enabled": False},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")

            status = load_camera_status(path)

        self.assertEqual(status["backend"], "usb")
        self.assertTrue(status["native_h264"])
        self.assertEqual(status["device"], "/dev/video0")
        self.assertEqual(status["audio"], {"enabled": False})
        self.assertNotIn("pid", status)

    def test_invalid_saved_configuration_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"source":"MJPEG"}', encoding="utf-8")
            self.assertEqual(load_camera_status(path), {})

    def test_configuration_is_installed_as_readable_json_for_system_service(self):
        config = {
            "source": "CSI",
            "camera": 0,
            "sensor": "imx219",
            "device_path": "/base/soc/i2c0/imx219@10",
            "resolution": "1920x1080",
            "fps": 30,
            "audio": {"enabled": False},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "petcam" / "config.json"
            with patch("petcam.cli.os.geteuid", return_value=0):
                save_stream_configuration(config, path)

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), config)

    def test_saved_cli_configuration_is_reused_without_reconfiguration(self):
        config = {
            "source": "USB",
            "device": "/dev/video0",
            "resolution": "1280x720",
            "fps": 30,
            "audio": {"enabled": False},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            self.assertEqual(load_saved_stream_configuration(path), {
                "source": "USB",
                "resolution": "1280x720",
                "fps": 30,
                "device": "/dev/video0",
                "physical_path": "",
                "audio": {"enabled": False},
            })

    def test_existing_service_is_started_without_forcing_a_restart(self):
        with patch("petcam.cli._run_service_command") as run:
            activate_stream_service()

        self.assertEqual(
            run.call_args_list,
            [
                call(["systemctl", "enable", "petcam.service"]),
                call(["systemctl", "start", "petcam.service"]),
            ],
        )

    def test_configuration_change_enables_service_and_restarts_it(self):
        with patch("petcam.cli._run_service_command") as run:
            activate_stream_service(restart=True)

        self.assertEqual(
            run.call_args_list,
            [
                call(["systemctl", "enable", "petcam.service"]),
                call(["systemctl", "restart", "petcam.service"]),
            ],
        )


if __name__ == "__main__":
    unittest.main()
