from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from petcam.infrastructure.streaming.live555 import (
    Live555Error,
    PcmAudioFramer,
    build_audio_capture_command,
)


class PcmAudioFramerTests(unittest.TestCase):
    def test_frames_arbitrary_partial_reads_without_dropping_bytes(self):
        framer = PcmAudioFramer()
        first = bytes(range(123))
        second = bytes((index % 256 for index in range(500)))
        third = b"x" * 17

        self.assertEqual(framer.feed(first), [])
        frames = framer.feed(second)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0], first + second[:197])
        frames += framer.feed(third)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[1 - 1], second[197:] + third[:3])


class AudioCaptureCommandTests(unittest.TestCase):
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
        self.assertEqual(command[command.index("--rate") + 1], "8000")
        self.assertEqual(command[command.index("--channels") + 1], "1")
        self.assertIn("--file-type", command)

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
