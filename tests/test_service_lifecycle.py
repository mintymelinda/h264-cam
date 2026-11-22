from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from petcam import cli
from petcam.infrastructure.streaming import live555


USB_CONFIG = {
    "source": "USB",
    "device": "/dev/video0",
    "physical_path": "usb-0000:00:00.0-1",
    "resolution": "1920x1080",
    "fps": 30,
}


class ManagedStreamTests(unittest.TestCase):
    def test_saved_configuration_preserves_capture_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "stream.json"
            with (
                patch.object(cli, "CONFIG_FILE", config_file),
                patch.dict(os.environ, {
                    "PETCAM_V4L2_MMAP_BUFFERS": "8",
                    "PETCAM_V4L2_CONTROLS": "video_bitrate=6000000",
                }, clear=True),
            ):
                cli._save_config(USB_CONFIG)
                saved = cli._read_saved_payload()

            self.assertIsNotNone(saved)
            config, environment = saved
            self.assertEqual(config, USB_CONFIG)
            self.assertEqual(environment, {
                "PETCAM_V4L2_MMAP_BUFFERS": "8",
                "PETCAM_V4L2_CONTROLS": "video_bitrate=6000000",
            })
            self.assertEqual(config_file.stat().st_mode & 0o777, 0o660)

    def test_service_mode_does_not_require_an_interactive_terminal(self):
        with patch.object(cli, "run_service", return_value=23) as run_service:
            self.assertEqual(cli.main(["--service"]), 23)
        run_service.assert_called_once_with()

    def test_unprivileged_service_control_uses_narrow_sudo_command(self):
        with (
            patch.object(cli.os, "geteuid", return_value=1000),
            patch.object(cli.subprocess, "run") as run,
        ):
            run.return_value.returncode = 0
            cli._systemctl("restart")

        command = run.call_args.args[0]
        self.assertEqual(command, [
            "/usr/bin/sudo", "-n", "/usr/bin/systemctl", "restart", "petcam.service"
        ])

    def test_service_runs_live555_in_process_and_restores_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            camera_status = run_dir / "camera-status.json"
            stream_status = run_dir / "stream-status.json"

            def run_streamer(runtime_directory):
                self.assertEqual(runtime_directory, run_dir)
                self.assertEqual(os.environ["PETCAM_RUN"], str(run_dir))
                self.assertEqual(os.environ["PETCAM_V4L2_MMAP_BUFFERS"], "8")
                self.assertEqual(
                    os.environ["PETCAM_V4L2_CONTROLS"], "video_bitrate=6000000"
                )
                marker = json.loads(camera_status.read_text(encoding="utf-8"))
                self.assertEqual(marker["pid"], os.getpid())
                stream_status.write_text("{}\n", encoding="utf-8")
                return None

            with (
                patch.object(cli, "RUN_DIR", run_dir),
                patch.object(cli, "CAMERA_STATUS", camera_status),
                patch.object(cli, "STREAM_STATUS", stream_status),
                patch.object(
                    cli,
                    "_read_saved_payload",
                    return_value=(USB_CONFIG, {
                        "PETCAM_V4L2_MMAP_BUFFERS": "8",
                        "PETCAM_V4L2_CONTROLS": "video_bitrate=6000000",
                    }),
                ),
                patch.dict(os.environ, {"PETCAM_V4L2_MMAP_BUFFERS": "3"}, clear=True),
                patch.object(live555, "main", side_effect=run_streamer) as stream_main,
                patch.object(cli.subprocess, "Popen") as popen,
            ):
                self.assertEqual(cli.run_service(), 0)
                stream_main.assert_called_once_with(run_dir)
                popen.assert_not_called()
                self.assertEqual(os.environ["PETCAM_V4L2_MMAP_BUFFERS"], "3")
                self.assertNotIn("PETCAM_V4L2_CONTROLS", os.environ)
                self.assertNotIn("PETCAM_RUN", os.environ)
                self.assertFalse(camera_status.exists())
                self.assertFalse(stream_status.exists())

    def test_systemd_unit_owns_stream_process_lifecycle(self):
        unit = Path(__file__).resolve().parents[1] / "systemd" / "petcam.service"
        content = unit.read_text(encoding="utf-8")
        self.assertIn("ExecStart=/usr/local/bin/petcam --service", content)
        self.assertIn("KillMode=control-group", content)
        self.assertIn("Restart=on-failure", content)


if __name__ == "__main__":
    unittest.main()
