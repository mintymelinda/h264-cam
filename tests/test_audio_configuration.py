from __future__ import annotations

import unittest
from unittest.mock import patch

from petcam.cli import configure_audio
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

    def test_validates_disabled_audio_without_capture_settings(self) -> None:
        normalized = validate_config(self.base_config({"enabled": False}))
        self.assertEqual(normalized["audio"], {"enabled": False})

    def test_validates_l16_audio_configuration(self) -> None:
        normalized = validate_config(self.base_config({
            "sample_rate": 48000,
            "channels": 2,
            "input_format": "S16_LE",
            "output_codec": "L16",
            "packet_ms": 5,
            "capture_gain_percent": 75,
            "device": "plughw:1,0",
        }))
        self.assertEqual(normalized["audio"]["packet_ms"], 5)
        self.assertEqual(normalized["audio"]["output_codec"], "L16")
        self.assertEqual(normalized["audio"]["capture_gain_percent"], 75)
        self.assertEqual(normalized["audio"]["device"], "plughw:1,0")

    def test_validates_opus_audio_configuration(self) -> None:
        normalized = validate_config(self.base_config({"output_codec": "OPUS"}))
        self.assertEqual(normalized["audio"]["output_codec"], "OPUS")
        self.assertEqual(normalized["audio"]["packet_ms"], 20)

    def test_rejects_unsupported_opus_packet_duration(self) -> None:
        with self.assertRaises(ConfigurationError):
            validate_config(self.base_config({"output_codec": "OPUS", "packet_ms": 5}))

    def test_rejects_unknown_codec(self) -> None:
        with self.assertRaises(ConfigurationError):
            validate_config(self.base_config({"output_codec": "AAC"}))

    def test_zero_capture_gain_is_valid(self) -> None:
        normalized = validate_config(self.base_config({"capture_gain_percent": 0}))
        self.assertEqual(normalized["audio"]["capture_gain_percent"], 0)

    def test_defaults_to_opus_and_full_capture_gain(self) -> None:
        normalized = validate_config(self.base_config({}))
        self.assertEqual(normalized["audio"]["output_codec"], "OPUS")
        self.assertEqual(normalized["audio"]["capture_gain_percent"], 100)
        self.assertEqual(normalized["audio"]["packet_ms"], 20)

    def test_rejects_capture_gain_outside_percentage_range(self) -> None:
        for gain in (-1, 101):
            with self.subTest(gain=gain), self.assertRaises(ConfigurationError):
                validate_config(self.base_config({"capture_gain_percent": gain}))

    def test_interactive_audio_setup_can_disable_audio_without_discovery(self) -> None:
        with (
            patch("petcam.cli.select_menu", return_value=1) as menu,
            patch("petcam.cli.discover_audio_capabilities") as discover,
        ):
            audio = configure_audio(object())

        self.assertEqual(audio, {"enabled": False})
        menu.assert_called_once()
        discover.assert_not_called()

    def test_interactive_audio_setup_offers_both_codecs_and_zero_gain(self) -> None:
        menu_calls = []

        def choose(_stdscr, title, _subtitle, items):
            menu_calls.append((title, items))
            return 0

        with (
            patch("petcam.cli.discover_audio_capabilities", return_value={
                "sample_rates": [48000], "channels": [2], "device": "plughw:1,0",
            }),
            patch("petcam.cli.select_menu", side_effect=choose),
        ):
            audio = configure_audio(object())

        codec_menu = next(items for title, items in menu_calls if "output codec" in title.lower())
        gain_menu = next(items for title, items in menu_calls if "capture gain" in title.lower())
        self.assertEqual(codec_menu, ["Opus (compressed)", "PCM (L16, uncompressed)"])
        self.assertIn("0%", gain_menu)
        self.assertEqual(audio["output_codec"], "OPUS")
        self.assertEqual(audio["capture_gain_percent"], 75)

    def test_pcm_framer_uses_selected_rate_channels_and_packet_duration(self) -> None:
        framer = PcmAudioFramer(sample_rate=48000, channels=2, packet_ms=5)
        self.assertEqual(framer.frame_bytes, 960)
        self.assertEqual(framer.feed(b"x" * 959), [])
        self.assertEqual(framer.feed(b"x"), [b"x" * 960])

    def test_capture_command_uses_selected_alsa_mode(self) -> None:
        status = {
            "audio": {
                "sample_rate": 24000,
                "channels": 1,
                "packet_ms": 5,
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
