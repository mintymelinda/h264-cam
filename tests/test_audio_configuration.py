from __future__ import annotations

import unittest
from unittest.mock import patch

from petcam.configuration import ConfigurationError, validate_config
from petcam.infrastructure.streaming.live555 import PcmAudioFramer, build_audio_capture_command


class AudioConfigurationTests(unittest.TestCase):
    def base_config(self, audio: dict) -> dict:
        return {
            "source": "USB",
            "device": "/dev/video0",
            "resolution": "1920x1080",
            "fps": 30,
            "audio": audio,
        }

    def test_validates_supported_opus_packet_durations_and_audio_modes(self) -> None:
        for packet_ms in (2.5, 5, 10, 20, 40, 60):
            with self.subTest(packet_ms=packet_ms):
                normalized = validate_config(self.base_config({
                    "sample_rate": 48000,
                    "channels": 2,
                    "input_format": "S16_LE",
                    "output_codec": "OPUS",
                    "bitrate": 128000,
                    "packet_ms": packet_ms,
                    "capture_gain_percent": 75,
                    "device": "plughw:1,0",
                }))
                self.assertEqual(normalized["audio"]["packet_ms"], packet_ms)
                self.assertEqual(normalized["audio"]["capture_gain_percent"], 75)
                self.assertEqual(normalized["audio"]["device"], "plughw:1,0")

    def test_rejects_non_opus_packet_duration(self) -> None:
        with self.assertRaises(ConfigurationError):
            validate_config(self.base_config({"packet_ms": 15}))

    def test_defaults_legacy_audio_configuration_to_full_capture_gain(self) -> None:
        normalized = validate_config(self.base_config({}))
        self.assertEqual(normalized["audio"]["capture_gain_percent"], 100)

    def test_rejects_capture_gain_outside_percentage_range(self) -> None:
        for gain in (-1, 101):
            with self.subTest(gain=gain), self.assertRaises(ConfigurationError):
                validate_config(self.base_config({"capture_gain_percent": gain}))

    def test_pcm_framer_uses_selected_rate_channels_and_packet_duration(self) -> None:
        framer = PcmAudioFramer(sample_rate=48000, channels=1, packet_ms=2.5)
        self.assertEqual(framer.frame_bytes, 240)
        self.assertEqual(framer.feed(b"x" * 239), [])
        self.assertEqual(framer.feed(b"x"), [b"x" * 240])

    def test_capture_command_uses_selected_alsa_mode(self) -> None:
        status = {
            "audio": {
                "sample_rate": 24000,
                "channels": 1,
                "packet_ms": 10,
                "device": "plughw:1,0",
            }
        }
        with patch("petcam.infrastructure.streaming.live555.shutil.which", return_value="/usr/bin/arecord"):
            command = build_audio_capture_command(status)
        self.assertIn("--rate", command)
        self.assertEqual(command[command.index("--rate") + 1], "24000")
        self.assertEqual(command[command.index("--channels") + 1], "1")
        self.assertEqual(command[command.index("--device") + 1], "plughw:1,0")


if __name__ == "__main__":
    unittest.main()
