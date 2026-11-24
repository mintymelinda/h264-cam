from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from petcam.cli import (
    _audio_capture_mode_label,
    _audio_framing_label,
    camera_modes,
    inventory_signature,
    parse_short_config,
)
from petcam.configuration import ConfigurationError, validate_config
from petcam.domain.camera.capabilities import parse_csi_cameras, parse_v4l2_modes
from petcam.infrastructure.camera.h264 import (
    AnnexBNalFramer,
    H264AccessUnitFramer,
    first_mb_in_slice,
    inspect_h264_access_unit,
)
from petcam.infrastructure.streaming.live555 import (
    H264Publisher,
    Live555Error,
    build_capture_command,
    load_camera_status,
)


class ShortConfigurationTests(unittest.TestCase):
    def test_parses_usb_shortcut(self):
        self.assertEqual(parse_short_config("USB;1920x1080;30"), ("USB", "1920x1080", 30.0))

    def test_parses_csi_shortcut_case_insensitively(self):
        self.assertEqual(parse_short_config("csi;1280x720;29.97"), ("CSI", "1280x720", 29.97))

    def test_rejects_invalid_shortcut(self):
        for value in ("USB;1920x1080", "MJPEG;1280x720;30", "USB;1280x720;0"):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                parse_short_config(value)

    def test_configuration_has_only_camera_stream_parameters(self):
        config = validate_config({
            "source": "USB",
            "device": "/dev/video2",
            "physical_path": "/sys/devices/usb1/1-1",
            "resolution": "1280x720",
            "fps": 30,
        })
        self.assertEqual(
            set(config),
            {"source", "device", "physical_path", "resolution", "fps", "audio"},
        )
        self.assertEqual(config["audio"], {
            "sample_rate": 48000,
            "channels": 2,
            "input_format": "S16_LE",
            "output_codec": "OPUS",
            "packet_ms": 20.0,
            "capture_gain_percent": 100,
            "device": "auto",
        })


class AudioCaptureModeDisplayTests(unittest.TestCase):
    def test_formats_mono_capture_mode(self):
        self.assertEqual(
            _audio_capture_mode_label({
                "sample_rate": 48000,
                "channels": 1,
                "input_format": "S16_LE",
            }),
            "Audio capture mode: 48 kHz · Mono · S16_LE",
        )

    def test_formats_stereo_capture_mode(self):
        self.assertEqual(
            _audio_capture_mode_label({
                "sample_rate": 24000,
                "channels": 2,
                "input_format": "S16_LE",
            }),
            "Audio capture mode: 24 kHz · Stereo · S16_LE",
        )


class L16FramingDisplayTests(unittest.TestCase):
    def test_reports_pcm_samples_and_48khz_rtp_ticks_for_non_48khz_capture(self):
        self.assertEqual(
            _audio_framing_label({"sample_rate": 24000, "output_codec": "L16", "packet_ms": 5}),
            "L16 packetization: 5 ms · 120 PCM samples/ch · 120 RTP ticks",
        )

    def test_reports_short_packet_duration(self):
        self.assertEqual(
            _audio_framing_label({"sample_rate": 48000, "output_codec": "L16", "packet_ms": 5}),
            "L16 packetization: 5 ms · 240 PCM samples/ch · 240 RTP ticks",
        )


class CameraModeTests(unittest.TestCase):
    def test_v4l2_modes_are_parsed_with_format_and_rate(self):
        output = """[0]: 'H264' (H.264, compressed)
    Size: Discrete 1920x1080
        Interval: Discrete 0.033s (30.000 fps)
        Interval: Discrete 0.017s (60.000 fps)
[1]: 'MJPG' (Motion-JPEG, compressed)
    Size: Discrete 1280x720
        Interval: Discrete 0.033s (30.000 fps)
"""
        modes = parse_v4l2_modes(output)
        self.assertEqual([mode["format"] for mode in modes], ["H264", "MJPG"])
        self.assertEqual(modes[0]["fps"], [60.0, 30.0])

    def test_usb_mode_selector_excludes_non_h264_modes(self):
        camera = {"modes": [
            {"format": "MJPG", "width": 1280, "height": 720, "fps": [30]},
            {"format": "H264", "width": 1920, "height": 1080, "fps": [30]},
        ]}
        self.assertEqual(
            camera_modes("USB", camera),
            [{"resolution": "1920x1080", "fps": 30.0, "format": "H264"}],
        )

    def test_csi_modes_parse_from_sensor_mode_listing(self):
        output = """Available cameras
-----------------
0 : imx219 [3280x2464] (/base/soc/i2c0/imx219@10)
    Modes: 'SRGGB10_CSI2P' : 1920x1080 [30.00 fps (0, 0)/3280x2464 crop]
"""
        cameras = parse_csi_cameras(output)
        self.assertEqual(len(cameras), 1)
        self.assertEqual(cameras[0]["modes"], [{"width": 1920, "height": 1080, "fps": 30.0}])

    def test_inventory_signature_changes_when_camera_list_changes(self):
        first = [{"device": "/dev/video0", "physical_path": "usb-a", "modes": []}]
        second = first + [{"device": "/dev/video2", "physical_path": "usb-b", "modes": []}]
        self.assertNotEqual(inventory_signature("USB", first), inventory_signature("USB", second))


class NativeCaptureTests(unittest.TestCase):
    def setUp(self):
        self.usb = {
            "native_h264": True, "backend": "usb", "device": "/dev/video0",
            "resolution": "1920x1080", "fps": 29.97,
        }

    def test_usb_capture_uses_v4l2_direct_h264_streaming(self):
        with patch("petcam.infrastructure.streaming.live555.shutil.which", return_value="/usr/bin/v4l2-ctl"):
            command = build_capture_command(self.usb)
        self.assertEqual(command[0], "v4l2-ctl")
        self.assertIn("--stream-mmap=3", command)
        self.assertIn("--stream-to=-", command)
        self.assertIn("--set-fmt-video=width=1920,height=1080,pixelformat=H264", command)
        self.assertFalse(any(
            codec in part.lower()
            for part in command
            for codec in ("ffmpeg", "gst", "x264", "openh264", "avdec")
        ))

    def test_usb_capture_exposes_experimental_buffer_and_encoder_controls(self):
        with patch("petcam.infrastructure.streaming.live555.shutil.which", return_value="/usr/bin/v4l2-ctl"), patch.dict(
            os.environ,
            {
                "PETCAM_V4L2_MMAP_BUFFERS": "8",
                "PETCAM_V4L2_CONTROLS": "video_gop_size=30,video_bitrate=6000000",
                "PETCAM_V4L2_STREAM_POLL": "1",
            },
        ):
            command = build_capture_command(self.usb)
        self.assertIn("--stream-mmap=8", command)
        self.assertIn("--set-ctrl=video_gop_size=30,video_bitrate=6000000", command)
        self.assertIn("--stream-poll", command)

    def test_usb_capture_rejects_invalid_experimental_parameters(self):
        with patch("petcam.infrastructure.streaming.live555.shutil.which", return_value="/usr/bin/v4l2-ctl"), patch.dict(
            os.environ, {"PETCAM_V4L2_MMAP_BUFFERS": "100"}
        ):
            with self.assertRaisesRegex(Live555Error, "PETCAM_V4L2_MMAP_BUFFERS"):
                build_capture_command(self.usb)
        with patch("petcam.infrastructure.streaming.live555.shutil.which", return_value="/usr/bin/v4l2-ctl"), patch.dict(
            os.environ, {"PETCAM_V4L2_CONTROLS": "video-gop-size=30"}
        ):
            with self.assertRaisesRegex(Live555Error, "comma-separated"):
                build_capture_command(self.usb)

    def test_csi_capture_exposes_experimental_gop_and_bitrate(self):
        csi = {
            "native_h264": True, "backend": "csi", "camera": 0,
            "resolution": "1920x1080", "fps": 30,
        }
        with patch("petcam.infrastructure.streaming.live555.shutil.which", return_value="/usr/bin/rpicam-vid"), patch(
            "petcam.infrastructure.streaming.live555.detect_hardware_h264_encoder",
            return_value={"available": True},
        ), patch.dict(os.environ, {"PETCAM_CSI_INTRA": "15", "PETCAM_CSI_BITRATE": "10000000"}):
            command = build_capture_command(csi)
        self.assertIn("--intra", command)
        self.assertEqual(command[command.index("--intra") + 1], "15")
        self.assertIn("--bitrate", command)
        self.assertEqual(command[command.index("--bitrate") + 1], "10000000")

    def test_non_native_source_is_rejected(self):
        with self.assertRaises(Live555Error):
            build_capture_command({**self.usb, "native_h264": False})

    def test_invalid_backend_is_rejected(self):
        with self.assertRaisesRegex(Live555Error, "backend"):
            build_capture_command({**self.usb, "backend": "network"})

    def test_publisher_marks_only_the_idr_nal(self):
        class FakeServer:
            def __init__(self):
                self.nals = []
            def push_nal(self, nal, is_idr=False):
                self.nals.append((nal, is_idr))

        server = FakeServer()
        publisher = H264Publisher(server)
        prefix = b"\x00\x00\x00\x01"
        publisher.publish(prefix + b"\x67\x42\x00\x1e")
        publisher.publish(prefix + b"\x68\xce\x06")
        publisher.publish(prefix + b"\x65\x88")
        publisher.publish(prefix + b"\x41\x9a")
        self.assertEqual(server.nals, [
            (b"\x67\x42\x00\x1e", False),
            (b"\x68\xce\x06", False),
            (b"\x65\x88", True),
            (b"\x41\x9a", False),
        ])
        self.assertEqual(
            publisher.counter_snapshot(),
            {
                "captured": {"7": 1, "8": 1, "5": 1, "1": 1},
                "queued": {"7": 1, "8": 1, "5": 1, "1": 1},
            },
        )

    def test_capture_reset_requires_a_new_sps_pps_idr_recovery_sequence(self):
        class FakeServer:
            def __init__(self):
                self.nals = []
            def push_nal(self, nal, is_idr=False):
                self.nals.append((nal, is_idr))

        prefix = b"\x00\x00\x00\x01"
        sps = b"\x67\x42\x00\x1e"
        pps = b"\x68\xce\x06"
        idr = b"\x65\x88"
        delta = b"\x41\x9a"
        server = FakeServer()
        publisher = H264Publisher(server)

        publisher.publish(prefix + sps)
        publisher.publish(prefix + pps)
        publisher.publish(prefix + idr)
        publisher.publish(prefix + delta)
        publisher.reset_capture()
        before = list(server.nals)

        # A delta frame from the new capture generation must not be published.
        publisher.publish(prefix + delta)
        self.assertEqual(server.nals, before)
        self.assertFalse(publisher.started)

        publisher.publish(prefix + sps)
        publisher.publish(prefix + pps)
        publisher.publish(prefix + idr)
        self.assertEqual(server.nals[-3:], [
            (sps, False), (pps, False), (idr, True)
        ])
        self.assertTrue(publisher.started)

    def test_publisher_rejects_idr_without_parameter_sets(self):
        class FakeServer:
            def push_nal(self, nal, is_idr=False):
                raise AssertionError("Must not publish without SPS/PPS")
        with self.assertRaisesRegex(Live555Error, "SPS and PPS"):
            H264Publisher(FakeServer()).publish(b"\x00\x00\x00\x01\x65\x88")


    def test_publisher_repeats_parameter_sets_only_before_first_idr_slice(self):
        class FakeServer:
            def __init__(self):
                self.nals = []
            def push_nal(self, nal, is_idr=False):
                self.nals.append((nal, is_idr))

        prefix = b"\x00\x00\x00\x01"
        sps = b"\x67\x42\x00\x1e"
        pps = b"\x68\xce\x06"
        idr_first = b"\x65\xe0"  # first_mb_in_slice = 0
        idr_continuation = b"\x65\x58"  # first_mb_in_slice = 1

        server = FakeServer()
        publisher = H264Publisher(server)
        publisher.publish(prefix + sps)
        publisher.publish(prefix + pps)
        publisher.publish(prefix + idr_first)
        publisher.publish(prefix + idr_continuation)
        publisher.publish(prefix + idr_first)
        publisher.publish(prefix + idr_continuation)

        self.assertEqual(server.nals, [
            (sps, False),
            (pps, False),
            (idr_first, True),
            (idr_continuation, True),
            (sps, False),
            (pps, False),
            (idr_first, True),
            (idr_continuation, True),
        ])

    def test_first_mb_in_slice_distinguishes_picture_start_from_continuation(self):
        self.assertEqual(first_mb_in_slice(b"\x00\x00\x00\x01\x65\xe0"), 0)
        self.assertEqual(first_mb_in_slice(b"\x00\x00\x00\x01\x65\x58"), 1)

class H264AccessUnitFramingTests(unittest.TestCase):
    @staticmethod
    def nal(header: int, payload: bytes = b"\x80") -> bytes:
        return b"\x00\x00\x00\x01" + bytes([header]) + payload

    def test_annex_b_framer_handles_split_start_codes_and_final_nal(self):
        sps = self.nal(0x67, b"\x42\x00\x1e")
        pps = self.nal(0x68, b"\xce\x06")
        framer = AnnexBNalFramer()
        stream = sps + pps
        output = []
        output.extend(framer.feed(stream[:7]))
        output.extend(framer.feed(stream[7:13]))
        output.extend(framer.feed(stream[13:]))
        output.extend(framer.flush())
        self.assertEqual(output, [sps, pps])

    def test_large_nal_is_scanned_incrementally_across_pipe_reads(self):
        prefix = b"\x00\x00\x00\x01"
        first_nal = prefix + b"\x65" + b"x" * 131072
        framer = AnnexBNalFramer()

        self.assertEqual(framer.feed(first_nal[:65541]), [])
        previous_offset = framer._scan_offset
        self.assertEqual(framer.feed(first_nal[65541:]), [])
        self.assertGreater(framer._scan_offset, previous_offset)

        output = []
        output.extend(framer.feed(b"\x00\x00"))
        output.extend(framer.feed(b"\x00"))
        output.extend(framer.feed(b"\x01\x41\x80"))
        self.assertEqual(output, [first_nal])
        self.assertEqual(framer.flush(), [prefix + b"\x41\x80"])

    def test_access_unit_metadata_retains_codec_config_for_late_rtsp_clients(self):
        sps = self.nal(0x67, b"\x42\x00\x1e")
        pps = self.nal(0x68, b"\xce\x06")
        aud = self.nal(0x09, b"\xf0")
        idr = self.nal(0x65)
        metadata = inspect_h264_access_unit(sps + pps + aud + idr)
        self.assertEqual(metadata["sps"], sps)
        self.assertEqual(metadata["pps"], pps)
        self.assertTrue(metadata["has_sps"])
        self.assertTrue(metadata["has_pps"])
        self.assertTrue(metadata["has_idr"])

    def test_access_units_split_at_first_slice_and_keep_parameter_sets(self):
        sps = self.nal(0x67, b"\x42\x00\x1e")
        pps = self.nal(0x68, b"\xce\x06")
        aud = self.nal(0x09, b"\xf0")
        idr = self.nal(0x65)
        first_p = self.nal(0x41)
        second_p = self.nal(0x41)
        framer = H264AccessUnitFramer()
        stream = sps + pps + aud + idr + first_p + second_p
        output = []
        for offset in range(0, len(stream), 5):
            output.extend(framer.feed(stream[offset:offset + 5]))
        output.extend(framer.flush())
        self.assertEqual(output, [sps + pps + aud + idr, first_p, second_p])


if __name__ == "__main__":
    unittest.main()
