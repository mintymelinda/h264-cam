from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from petcam.infrastructure.streaming.live555 import (
    Live555Error,
    PcmAudioFramer,
    build_audio_capture_command,
    set_alsa_capture_volume,
)


class PcmAudioFramerTests(unittest.TestCase):
    def test_frames_arbitrary_partial_reads_without_dropping_bytes(self):
        framer = PcmAudioFramer(packet_ms=20)
        first = bytes(range(123))
        second = bytes((index % 256 for index in range(4000)))
        third = b"x" * 3557

        self.assertEqual(framer.feed(first), [])
        frames = framer.feed(second)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0], first + second[:3717])
        frames += framer.feed(third)
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[1], second[3717:] + third)


class AlsaCaptureVolumeTests(unittest.TestCase):
    def test_sets_selected_microphone_capture_volume(self):
        with (
            patch("petcam.infrastructure.streaming.live555.shutil.which", return_value="/usr/bin/amixer"),
            patch(
                "petcam.infrastructure.streaming.live555.subprocess.run",
                return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
            ) as run,
        ):
            set_alsa_capture_volume("plughw:1,0", 75)

        run.assert_called_once_with(
            ["/usr/bin/amixer", "-c", "1", "sset", "Mic", "75%", "cap"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )

    def test_accepts_raw_hw_device_names(self):
        with (
            patch("petcam.infrastructure.streaming.live555.shutil.which", return_value="/usr/bin/amixer"),
            patch(
                "petcam.infrastructure.streaming.live555.subprocess.run",
                return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
            ) as run,
        ):
            set_alsa_capture_volume("hw:2,0", 50)

        self.assertEqual(run.call_args.args[0][2], "2")
        self.assertEqual(run.call_args.args[0][5], "50%")

    def test_rejects_device_names_without_a_numeric_card(self):
        with self.assertRaisesRegex(Live555Error, "numeric hw:CARD,DEVICE"):
            set_alsa_capture_volume("default")

    def test_fails_clearly_when_amixer_is_missing(self):
        with patch("petcam.infrastructure.streaming.live555.shutil.which", return_value=None):
            with self.assertRaisesRegex(Live555Error, "amixer is missing"):
                set_alsa_capture_volume("plughw:1,0")

    def test_reports_mixer_failure(self):
        with (
            patch("petcam.infrastructure.streaming.live555.shutil.which", return_value="/usr/bin/amixer"),
            patch(
                "petcam.infrastructure.streaming.live555.subprocess.run",
                return_value=SimpleNamespace(returncode=1, stdout="", stderr="No such control"),
            ),
        ):
            with self.assertRaisesRegex(Live555Error, "capture volume to 100%"):
                set_alsa_capture_volume("plughw:1,0")

    def test_rejects_capture_gain_outside_percentage_range(self):
        with self.assertRaisesRegex(Live555Error, "between 0 and 100"):
            set_alsa_capture_volume("plughw:1,0", 101)


class AudioCaptureCommandTests(unittest.TestCase):
    def test_camera_configuration_can_disable_audio_without_alsa(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch("petcam.infrastructure.streaming.live555.shutil.which", return_value=None):
                self.assertIsNone(build_audio_capture_command({"audio": {"enabled": False}}))

    def test_disabled_audio_does_not_require_alsa(self):
        with patch.dict(os.environ, {"PETCAM_AUDIO": "0"}, clear=True):
            with patch("petcam.infrastructure.streaming.live555.shutil.which", return_value=None):
                self.assertIsNone(build_audio_capture_command())

    def test_auto_selection_prefers_razer_kiyo_capture_device(self):
        listing = (
            "card 0: HDMI [vc4-hdmi], device 0: HDMI PCM [HDMI PCM]\n"
            "card 2: Kiyo [Razer Kiyo], device 0: USB Audio [USB Audio]\n"
        )
        with patch.dict(os.environ, {"PETCAM_AUDIO": "1"}, clear=True):
            with (
                patch("petcam.infrastructure.streaming.live555.shutil.which", return_value="/usr/bin/arecord"),
                patch(
                    "petcam.infrastructure.streaming.live555.subprocess.run",
                    return_value=SimpleNamespace(returncode=0, stdout=listing, stderr=""),
                ),
            ):
                command = build_audio_capture_command()

        self.assertEqual(command[0], "arecord")
        self.assertEqual(command[command.index("--device") + 1], "plughw:2,0")
        self.assertEqual(command[command.index("--rate") + 1], "48000")
        self.assertEqual(command[command.index("--channels") + 1], "2")
        self.assertEqual(command[command.index("--period-size") + 1], "960")
        self.assertEqual(command[command.index("--buffer-size") + 1], "19200")
        self.assertIn("--file-type", command)

    def test_audio_buffer_periods_can_be_configured(self):
        with patch.dict(
            os.environ,
            {"PETCAM_AUDIO": "1", "PETCAM_AUDIO_DEVICE": "hw:1,0",
             "PETCAM_AUDIO_BUFFER_PERIODS": "24"},
            clear=True,
        ):
            with patch("petcam.infrastructure.streaming.live555.shutil.which", return_value="/usr/bin/arecord"):
                command = build_audio_capture_command()

        self.assertEqual(command[command.index("--period-size") + 1], "960")
        self.assertEqual(command[command.index("--buffer-size") + 1], "23040")

    def test_rejects_unreasonable_audio_buffer_period_count(self):
        with patch.dict(
            os.environ,
            {"PETCAM_AUDIO": "1", "PETCAM_AUDIO_DEVICE": "hw:1,0",
             "PETCAM_AUDIO_BUFFER_PERIODS": "2"},
            clear=True,
        ):
            with patch("petcam.infrastructure.streaming.live555.shutil.which", return_value="/usr/bin/arecord"):
                with self.assertRaisesRegex(Live555Error, "PETCAM_AUDIO_BUFFER_PERIODS"):
                    build_audio_capture_command()

    def test_auto_selection_uses_first_capture_device_when_kiyo_name_is_unavailable(self):
        listing = "card 1: USB [USB Audio], device 0: USB Audio [USB Audio]\n"
        with patch.dict(os.environ, {"PETCAM_AUDIO": "1"}, clear=True):
            with (
                patch("petcam.infrastructure.streaming.live555.shutil.which", return_value="/usr/bin/arecord"),
                patch(
                    "petcam.infrastructure.streaming.live555.subprocess.run",
                    return_value=SimpleNamespace(returncode=0, stdout=listing, stderr=""),
                ),
            ):
                command = build_audio_capture_command()

        self.assertEqual(command[command.index("--device") + 1], "plughw:1,0")

    def test_explicit_device_skips_hardware_discovery(self):
        with patch.dict(
            os.environ,
            {"PETCAM_AUDIO": "1", "PETCAM_AUDIO_DEVICE": "hw:3,0"},
            clear=True,
        ):
            with (
                patch("petcam.infrastructure.streaming.live555.shutil.which", return_value="/usr/bin/arecord"),
                patch("petcam.infrastructure.streaming.live555.subprocess.run") as run,
            ):
                command = build_audio_capture_command()

        run.assert_not_called()
        self.assertEqual(command[command.index("--device") + 1], "hw:3,0")

    def test_missing_capture_device_fails_with_actionable_error(self):
        with patch.dict(os.environ, {"PETCAM_AUDIO": "1"}, clear=True):
            with (
                patch("petcam.infrastructure.streaming.live555.shutil.which", return_value="/usr/bin/arecord"),
                patch(
                    "petcam.infrastructure.streaming.live555.subprocess.run",
                    return_value=SimpleNamespace(returncode=0, stdout="no soundcards found\n", stderr=""),
                ),
            ):
                with self.assertRaisesRegex(Live555Error, "No ALSA capture device"):
                    build_audio_capture_command()

    def test_invalid_device_name_is_rejected(self):
        with patch.dict(
            os.environ,
            {"PETCAM_AUDIO": "1", "PETCAM_AUDIO_DEVICE": "hw:1,0\narecord"},
            clear=True,
        ):
            with patch("petcam.infrastructure.streaming.live555.shutil.which", return_value="/usr/bin/arecord"):
                with self.assertRaisesRegex(Live555Error, "PETCAM_AUDIO_DEVICE"):
                    build_audio_capture_command()


if __name__ == "__main__":
    unittest.main()
