#!/usr/bin/env python3
"""Capture native H.264 NAL units and publish them through Live555."""

from __future__ import annotations

import ctypes
import json
import logging
import math
import struct
from collections import Counter
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from ..camera.discovery import detect_hardware_h264_encoder
from ..camera.h264 import AnnexBNalFramer, H264FramingError, first_mb_in_slice
from ..system.hostname import advertised_hostname

RUN_DIR = Path(os.environ.get("PETCAM_RUN", f"/tmp/petcam-{os.getuid()}"))
CAMERA_STATUS = RUN_DIR / "camera-status.json"
STREAM_STATUS = RUN_DIR / "stream-status.json"
RTSP_PORT = 8554
RTSP_PATH = "/petcam"
MAX_NAL_BYTES = 16 * 1024 * 1024


class Live555Error(RuntimeError):
    """Raised when native capture or the Live555 RTSP server cannot start."""


def load_camera_status(path: Path | str = CAMERA_STATUS) -> dict:
    try:
        status = json.loads(Path(path).read_text(encoding="utf-8"))
        pid = int(status.get("pid", 0))
        if status.get("native_h264") is not True or pid <= 1:
            return {}
        os.kill(pid, 0)
        return status
    except (OSError, ValueError, TypeError):
        return {}


def validate_mode(status: dict) -> tuple[int, int, float]:
    try:
        width_text, height_text = str(status.get("resolution", "")).split("x", 1)
        width, height = int(width_text), int(height_text)
        fps = float(status.get("fps"))
    except (ValueError, TypeError) as error:
        raise Live555Error("Configured H.264 capture mode is invalid.") from error
    if not (0 < width <= 65535 and 0 < height <= 65535 and 0 < fps <= 1000):
        raise Live555Error("Configured H.264 capture mode is outside supported limits.")
    return width, height, fps


def _environment_integer(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw, 10)
    except ValueError as error:
        raise Live555Error(f"{name} must be an integer.") from error
    if not minimum <= value <= maximum:
        raise Live555Error(f"{name} must be between {minimum} and {maximum}.")
    return value


def _v4l2_controls() -> list[tuple[str, int]]:
    raw = os.environ.get("PETCAM_V4L2_CONTROLS", "").strip()
    if not raw:
        return []
    controls = []
    seen = set()
    for item in raw.split(","):
        name, separator, raw_value = item.strip().partition("=")
        if not separator or not name or not name.replace("_", "").isalnum() or not name[0].isalpha():
            raise Live555Error(
                "PETCAM_V4L2_CONTROLS must be comma-separated control=value integer pairs."
            )
        if name in seen:
            raise Live555Error(f"PETCAM_V4L2_CONTROLS contains duplicate control {name}.")
        try:
            value = int(raw_value, 10)
        except ValueError as error:
            raise Live555Error(f"V4L2 control {name} must have an integer value.") from error
        if not -(2**31) <= value < 2**31:
            raise Live555Error(f"V4L2 control {name} is outside the signed 32-bit range.")
        seen.add(name)
        controls.append((name, value))
    return controls


def build_capture_command(status: dict) -> list[str]:
    if status.get("native_h264") is not True:
        raise Live555Error("PetCam accepts only native H.264 camera sources.")
    width, height, fps = validate_mode(status)
    if status.get("backend") == "usb":
        device = str(status.get("device", ""))
        if not (device.startswith("/dev/video") or device.startswith("/dev/v4l/by-id/")):
            raise Live555Error("Configured USB camera device path is invalid.")
        if not shutil.which("v4l2-ctl"):
            raise Live555Error("v4l2-ctl is missing; install v4l-utils.")
        buffers = _environment_integer("PETCAM_V4L2_MMAP_BUFFERS", 3, 2, 32)
        command = [
            "v4l2-ctl", f"--device={device}",
            f"--set-fmt-video=width={width},height={height},pixelformat=H264",
            f"--set-parm={fps:g}",
        ]
        controls = _v4l2_controls()
        if controls:
            command.append("--set-ctrl=" + ",".join(f"{name}={value}" for name, value in controls))
        command.append(f"--stream-mmap={buffers}")
        if os.environ.get("PETCAM_V4L2_STREAM_POLL", "").strip().lower() in {"1", "true", "yes", "on"}:
            command.append("--stream-poll")
        command.append("--stream-to=-")
        return command
    if status.get("backend") == "csi":
        try:
            camera = int(status.get("camera"))
        except (TypeError, ValueError) as error:
            raise Live555Error("Configured CSI camera index is invalid.") from error
        if camera < 0:
            raise Live555Error("Configured CSI camera index is invalid.")
        if not shutil.which("rpicam-vid"):
            raise Live555Error("rpicam-vid is missing; install rpicam-apps-lite.")
        if detect_hardware_h264_encoder().get("available") is not True:
            raise Live555Error("No Raspberry Pi hardware H.264 encoder was detected.")
        intra = _environment_integer("PETCAM_CSI_INTRA", 30, 1, 1000)
        bitrate = _environment_integer("PETCAM_CSI_BITRATE", 8_000_000, 64_000, 100_000_000)
        return [
            "rpicam-vid", "--camera", str(camera), "--timeout", "0",
            "--nopreview", "--codec", "h264", "--inline",
            "--intra", str(intra), "--bitrate", str(bitrate),
            "--width", str(width), "--height", str(height),
            "--framerate", f"{fps:g}", "--output", "-",
        ]
    raise Live555Error("Configured camera backend must be usb or csi.")



class PcmAudioFramer:
    """Turn arbitrary ALSA pipe reads into exact PCM frames for the selected codec."""

    def __init__(self, sample_rate: int = 48000, channels: int = 2, packet_ms: float = 20) -> None:
        self.frame_bytes = int(sample_rate * packet_ms / 1000) * channels * 2
        if self.frame_bytes <= 0:
            raise ValueError("Invalid PCM frame configuration.")
        self._pending = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        if not chunk:
            return []
        self._pending.extend(chunk)
        frames = []
        while len(self._pending) >= self.frame_bytes:
            frames.append(bytes(self._pending[:self.frame_bytes]))
            del self._pending[:self.frame_bytes]
        return frames


def _detect_alsa_capture_device() -> str:
    """Prefer the Kiyo mic; otherwise use the first ALSA capture device."""
    try:
        result = subprocess.run(
            ["arecord", "-l"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Live555Error(f"Unable to discover ALSA capture devices: {error}") from error

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise Live555Error(
            "Unable to list ALSA capture devices."
            + (f" {detail}" if detail else "")
        )

    devices = []
    pattern = re.compile(r"^\s*card\s+(\d+):.*?,\s*device\s+(\d+):")
    for line in result.stdout.splitlines():
        match = pattern.search(line)
        if match:
            devices.append((int(match.group(1)), int(match.group(2)), line))

    if not devices:
        raise Live555Error(
            "No ALSA capture device was found. Connect the Razer Kiyo or set "
            "PETCAM_AUDIO_DEVICE to an ALSA capture device."
        )

    selected = next(
        (item for item in devices if "kiyo" in item[2].lower() or "razer" in item[2].lower()),
        devices[0],
    )
    return f"plughw:{selected[0]},{selected[1]}"


def set_alsa_capture_volume(device: str, gain_percent: int = 100) -> None:
    """Set the selected ALSA card's microphone capture volume."""
    try:
        gain_percent = int(gain_percent)
    except (TypeError, ValueError) as error:
        raise Live555Error("Microphone capture gain must be an integer from 0 to 100 percent.") from error
    if not 0 <= gain_percent <= 100:
        raise Live555Error("Microphone capture gain must be between 0 and 100 percent.")
    match = re.fullmatch(r"(?:plug)?hw:(\d+),\d+", device)
    if match is None:
        raise Live555Error(
            "Cannot set microphone volume for ALSA device "
            f"{device!r}; use a numeric hw:CARD,DEVICE or plughw:CARD,DEVICE name."
        )
    amixer = shutil.which("amixer")
    if not amixer:
        raise Live555Error("amixer is missing; install alsa-utils to set microphone volume.")
    command = [amixer, "-c", match.group(1), "sset", "Mic", f"{gain_percent}%", "cap"]
    try:
        result = subprocess.run(
            command, check=False, capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Live555Error(f"Unable to set microphone capture volume: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise Live555Error(
            f"Unable to set ALSA microphone capture volume to {gain_percent}%."
            + (f" {detail}" if detail else "")
        )


def build_audio_capture_command(camera_status: dict | None = None) -> list[str] | None:
    """Capture the configured signed PCM mode for the Live555 audio track."""
    if os.environ.get("PETCAM_AUDIO", "1").strip().lower() in {"0", "false", "no", "off"}:
        return None
    if not shutil.which("arecord"):
        raise Live555Error("arecord is missing; install alsa-utils to enable microphone audio.")

    audio = (camera_status or {}).get("audio", {})
    device = str(audio.get("device") or os.environ.get("PETCAM_AUDIO_DEVICE", "auto")).strip()
    if not device or any(char in device for char in "\r\n"):
        raise Live555Error("PETCAM_AUDIO_DEVICE must be a non-empty ALSA capture device name.")
    if device.lower() == "auto":
        device = _detect_alsa_capture_device()

    rate = int(audio.get("sample_rate", 48000))
    channels = int(audio.get("channels", 2))
    packet_ms = float(audio.get("packet_ms", 20))
    samples = int(rate * packet_ms / 1000)
    # Keep packetization at the selected duration while giving ALSA enough
    # hardware-buffer headroom to survive short Linux scheduling stalls.
    buffer_periods = _environment_integer("PETCAM_AUDIO_BUFFER_PERIODS", 20, 4, 64)
    return [
        "arecord", "--quiet", "--device", device,
        "--format", "S16_LE", "--rate", str(rate), "--channels", str(channels),
        "--file-type", "raw", "--period-size", str(samples),
        "--buffer-size", str(samples * buffer_periods),
    ]


class Live555Server:
    """ctypes binding to the small ARMHF Live555 server library."""

    def __init__(self, library_path: Path | None = None):
        if library_path is None:
            override = os.environ.get("PETCAM_LIVE555_LIBRARY")
            library_path = Path(override) if override else (
                Path(__file__).resolve().parents[4] / "lib" / "libpetcam_live555.so"
            )
        try:
            self.library = ctypes.CDLL(str(library_path))
        except OSError as error:
            raise Live555Error(f"Unable to load Live555 native library: {error}") from error
        self.library.petcam_configure_audio.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int
        ]
        self.library.petcam_configure_audio.restype = ctypes.c_int
        self.library.petcam_start.argtypes = [ctypes.c_ushort, ctypes.c_char_p]
        self.library.petcam_start.restype = ctypes.c_int
        self.library.petcam_push_nal.argtypes = [
            ctypes.POINTER(ctypes.c_ubyte), ctypes.c_size_t, ctypes.c_int
        ]
        self.library.petcam_push_nal.restype = ctypes.c_int
        self.library.petcam_push_audio_pcm.argtypes = [
            ctypes.POINTER(ctypes.c_ubyte), ctypes.c_size_t
        ]
        self.library.petcam_push_audio_pcm.restype = ctypes.c_int
        self.library.petcam_clear_audio.argtypes = []
        self.library.petcam_clear_audio.restype = None
        self.library.petcam_last_error.restype = ctypes.c_char_p
        self.library.petcam_stop.argtypes = []
        self.library.petcam_stop.restype = None
        self.library.petcam_client_count.argtypes = []
        self.library.petcam_client_count.restype = ctypes.c_uint
        self.library.petcam_nal_count.argtypes = []
        self.library.petcam_nal_count.restype = ctypes.c_ulonglong
        self.library.petcam_audio_handoff_count.argtypes = []
        self.library.petcam_audio_handoff_count.restype = ctypes.c_ulonglong
        self.library.petcam_nal_type_count.argtypes = [ctypes.c_uint]
        self.library.petcam_nal_type_count.restype = ctypes.c_ulonglong

    def configure_audio(self, audio: dict) -> None:
        codec = str(audio.get("output_codec", "OPUS")).upper()
        codec_id = 1 if codec == "OPUS" else 0
        packet_ms10 = round(float(audio.get("packet_ms", 20 if codec == "OPUS" else 5)) * 10)
        result = self.library.petcam_configure_audio(
            int(audio.get("sample_rate", 48000)),
            int(audio.get("channels", 2)),
            codec_id,
            int(audio.get("bitrate", 128000)),
            packet_ms10,
        )
        if result != 0:
            raise Live555Error("The native audio encoder rejected the selected codec configuration.")

    def push_audio_pcm(self, pcm: bytes) -> None:
        buffer = (ctypes.c_ubyte * len(pcm)).from_buffer_copy(pcm)
        if self.library.petcam_push_audio_pcm(buffer, len(pcm)) != 0:
            raise Live555Error("Unable to queue microphone audio.")

    def clear_audio(self) -> None:
        self.library.petcam_clear_audio()

    def start(self, port: int = RTSP_PORT, path: str = RTSP_PATH) -> None:
        if self.library.petcam_start(port, path.encode("ascii")) != 0:
            detail = self.library.petcam_last_error()
            message = detail.decode("utf-8", "replace") if detail else "unknown error"
            raise Live555Error(f"Unable to start Live555 RTSP server: {message}")

    def push_nal(self, nal: bytes, is_idr: bool = False) -> None:
        if not nal or len(nal) > MAX_NAL_BYTES:
            raise Live555Error("H.264 NAL unit is empty or exceeds the size limit.")
        buffer = (ctypes.c_ubyte * len(nal)).from_buffer_copy(nal)
        if self.library.petcam_push_nal(buffer, len(nal), int(is_idr)) != 0:
            raise Live555Error("Live555 rejected an H.264 NAL unit.")

    def client_count(self) -> int:
        return int(self.library.petcam_client_count())

    def nal_count(self) -> int:
        return int(self.library.petcam_nal_count())

    def audio_handoff_count(self) -> int:
        return int(self.library.petcam_audio_handoff_count())

    def nal_type_counts(self) -> dict[str, int]:
        return {
            str(nal_type): int(self.library.petcam_nal_type_count(nal_type))
            for nal_type in range(32)
        }

    def stop(self) -> None:
        self.library.petcam_stop()


class H264Publisher:
    """Cache parameter sets and prepend them only before the first slice of each IDR picture."""

    def __init__(self, server: Live555Server):
        self.server = server
        self.sps = b""
        self.pps = b""
        self.started = False
        self._counter_lock = threading.Lock()
        self._captured_nals = Counter()
        self._queued_nals = Counter()

    def counter_snapshot(self) -> dict[str, dict[str, int]]:
        with self._counter_lock:
            return {
                "captured": {str(nal_type): count for nal_type, count in self._captured_nals.items()},
                "queued": {str(nal_type): count for nal_type, count in self._queued_nals.items()},
            }

    def _queue(self, payload: bytes, *, is_idr: bool = False) -> None:
        self.server.push_nal(payload, is_idr=is_idr)
        nal_type = payload[0] & 0x1F
        with self._counter_lock:
            self._queued_nals[nal_type] += 1

    @staticmethod
    def nal_payload(nal: bytes) -> tuple[int, bytes]:
        if nal.startswith(b"\x00\x00\x00\x01"):
            payload = nal[4:]
        elif nal.startswith(b"\x00\x00\x01"):
            payload = nal[3:]
        else:
            raise Live555Error("Camera emitted an H.264 NAL without an Annex-B start code.")
        if not payload:
            raise Live555Error("Camera emitted an empty H.264 NAL unit.")
        return payload[0] & 0x1F, payload

    def publish(self, nal: bytes) -> None:
        nal_type, payload = self.nal_payload(nal)
        with self._counter_lock:
            self._captured_nals[nal_type] += 1
        if nal_type == 7:
            self.sps = payload
        elif nal_type == 8:
            self.pps = payload
        elif nal_type == 5:
            first_mb = first_mb_in_slice(b"\x00\x00\x00\x01" + payload)
            if first_mb == 0:
                if not self.sps or not self.pps:
                    raise Live555Error("Camera emitted an IDR before SPS and PPS were available.")
                # Parameter sets belong before the picture, not between its
                # slices. Repeating them between IDR slices breaks the access unit.
                self._queue(self.sps)
                self._queue(self.pps)
                self.started = True
            elif not self.started:
                # A continuation slice cannot independently start a stream.
                return
            # Every IDR slice is type 5, but only the first one follows the
            # SPS/PPS recovery pair and can be used as a queue restart point.
            self._queue(payload, is_idr=True)
        elif self.started:
            self._queue(payload)


def write_status(
    state: str,
    source: dict,
    message: str = "",
    *,
    rtsp_clients: int = 0,
    captured_nals_per_second: float = 0.0,
    queued_nals_per_second: float = 0.0,
    streamed_nals_per_second: float = 0.0,
    captured_nals_by_type: dict[str, int] | None = None,
    queued_nals_by_type: dict[str, int] | None = None,
    rtsp_source_nals_by_type: dict[str, int] | None = None,
    audio_diagnostics: dict | None = None,
) -> None:
    RUN_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {
        "state": state,
        "source": source.get("backend"),
        "resolution": source.get("resolution"),
        "capture_fps": source.get("fps"),
        "rtsp_clients": rtsp_clients,
        "captured_nals_per_second": round(captured_nals_per_second, 1),
        "queued_nals_per_second": round(queued_nals_per_second, 1),
        "streamed_nals_per_second": round(streamed_nals_per_second, 1),
        "captured_nals_by_type": captured_nals_by_type or {},
        "queued_nals_by_type": queued_nals_by_type or {},
        "rtsp_source_nals_by_type": rtsp_source_nals_by_type or {},
        "audio": audio_diagnostics or {},
        "updated_at": time.time(),
        "message": message,
    }
    temporary = STREAM_STATUS.with_name(STREAM_STATUS.name + ".tmp")
    temporary.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    os.replace(temporary, STREAM_STATUS)


def run_server(camera_status: dict) -> int:
    command = build_capture_command(camera_status)
    server = Live555Server()
    capture = None
    reader = None
    audio_capture = None
    audio_reader = None
    stop_reader = threading.Event()
    first_idr = threading.Event()
    reader_error = {"message": ""}
    audio_reader_error = {"message": ""}
    audio_metrics_lock = threading.Lock()
    audio_metrics = {"capture_frames": 0, "native_accepted_frames": 0, "pcm_bytes": 0, "rms": 0.0, "peak": 0, "minimum": 0, "maximum": 0, "last_frame_at": None, "pending_bytes": 0}
    publisher = H264Publisher(server)
    framer_mode = os.environ.get("PETCAM_LIVE555_FRAMER", "stream").strip().lower()
    if framer_mode not in {"stream", "discrete"}:
        raise Live555Error("PETCAM_LIVE555_FRAMER must be 'stream' or 'discrete'.")
    stop_requested = threading.Event()
    previous_sigterm = signal.signal(signal.SIGTERM, lambda _signum, _frame: stop_requested.set())
    previous_sigint = signal.signal(signal.SIGINT, lambda _signum, _frame: stop_requested.set())

    def read_capture() -> None:
        assert capture is not None and capture.stdout is not None
        framer = AnnexBNalFramer()
        try:
            while not stop_reader.is_set():
                chunk = capture.stdout.read(65536)
                if not chunk:
                    break
                for nal in framer.feed(chunk):
                    publisher.publish(nal)
                    if publisher.started:
                        first_idr.set()
            for nal in framer.flush():
                publisher.publish(nal)
                if publisher.started:
                    first_idr.set()
            if not stop_reader.is_set():
                reader_error["message"] = "Camera capture stopped producing H.264 data."
        except (OSError, H264FramingError, Live555Error) as error:
            reader_error["message"] = str(error)
            logging.exception("H.264 capture/publishing failed")

    def read_audio_capture(audio: dict) -> None:
        assert audio_capture is not None and audio_capture.stdout is not None
        framer = PcmAudioFramer(
            int(audio["sample_rate"]), int(audio["channels"]), float(audio["packet_ms"])
        )
        try:
            while not stop_reader.is_set():
                chunk = audio_capture.stdout.read(framer.frame_bytes)
                if not chunk:
                    if not stop_reader.is_set():
                        audio_reader_error["message"] = "ALSA microphone capture stopped unexpectedly."
                    break
                for frame in framer.feed(chunk):
                    count = 0
                    sum_squares = 0
                    peak = 0
                    minimum = 32767
                    maximum = -32768
                    for item in struct.iter_unpack("<h", frame):
                        sample = item[0]
                        count += 1
                        sum_squares += sample * sample
                        peak = max(peak, abs(sample))
                        minimum = min(minimum, sample)
                        maximum = max(maximum, sample)
                    server.push_audio_pcm(frame)
                    with audio_metrics_lock:
                        audio_metrics["capture_frames"] += 1
                        audio_metrics["native_accepted_frames"] += 1
                        audio_metrics["pcm_bytes"] += len(frame)
                        audio_metrics["rms"] = round(math.sqrt(sum_squares / max(count, 1)), 1)
                        audio_metrics["peak"] = peak
                        audio_metrics["minimum"] = minimum
                        audio_metrics["maximum"] = maximum
                        audio_metrics["last_frame_at"] = time.time()
                        audio_metrics["pending_bytes"] = len(framer._pending)
        except (OSError, Live555Error) as error:
            if not stop_reader.is_set():
                audio_reader_error["message"] = str(error)
                logging.exception("Microphone audio capture failed")

    logging.info("Using Live555 H.264 framer mode: %s", framer_mode)
    started_at = time.monotonic()
    previous_capture_count = 0
    previous_queued_count = 0
    previous_nal_count = 0
    previous_sample_at = started_at
    previous_audio_frames = 0
    try:
        audio = camera_status.get("audio", {})
        server.configure_audio(audio)
        server.start()
        server.clear_audio()
        audio_command = build_audio_capture_command(camera_status)
        if audio_command is not None:
            audio_device = audio_command[audio_command.index("--device") + 1]
            capture_gain_percent = int(audio.get("capture_gain_percent", 100))
            set_alsa_capture_volume(audio_device, capture_gain_percent)
            logging.info(
                "Set ALSA microphone capture volume to %s%% on %s",
                capture_gain_percent,
                audio_device,
            )
            try:
                audio_capture = subprocess.Popen(
                    audio_command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=None, bufsize=0,
                )
            except OSError as error:
                raise Live555Error(f"Unable to start ALSA microphone capture: {error}") from error
            if audio_capture.stdout is None:
                raise Live555Error("Unable to open the microphone capture pipe.")
            audio_reader = threading.Thread(
                target=read_audio_capture, args=(audio,), name="petcam-audio-capture", daemon=True
            )
            audio_reader.start()
            logging.info(
                "Microphone audio enabled: ALSA buffer=%d periods (%.1fms), packet=%sms; command: %s",
                _environment_integer("PETCAM_AUDIO_BUFFER_PERIODS", 20, 4, 64),
                float(audio["packet_ms"]) * _environment_integer("PETCAM_AUDIO_BUFFER_PERIODS", 20, 4, 64),
                audio["packet_ms"],
                " ".join(audio_command),
            )
        else:
            logging.info("Microphone audio is disabled by PETCAM_AUDIO.")
        capture = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0,
        )
        reader = threading.Thread(target=read_capture, name="petcam-h264-capture", daemon=True)
        reader.start()
        logging.info("H.264 RTSP endpoint: rtsp://%s:%s%s", advertised_hostname(), RTSP_PORT, RTSP_PATH)
        while not stop_requested.is_set():
            if capture.poll() is not None:
                raise Live555Error(f"Camera capture exited with status {capture.returncode}.")
            if reader_error["message"]:
                raise Live555Error(reader_error["message"])
            if audio_capture is not None and audio_capture.poll() is not None:
                raise Live555Error(
                    f"ALSA microphone capture exited with status {audio_capture.returncode}."
                )
            if audio_reader_error["message"]:
                raise Live555Error(audio_reader_error["message"])
            now = time.monotonic()
            elapsed = max(now - previous_sample_at, 0.001)
            publisher_counts = publisher.counter_snapshot()
            captured_by_type = publisher_counts["captured"]
            queued_by_type = publisher_counts["queued"]
            source_by_type = server.nal_type_counts()
            captured_count = sum(captured_by_type.values())
            queued_count = sum(queued_by_type.values())
            nal_count = server.nal_count()
            captured_rate = (captured_count - previous_capture_count) / elapsed
            queued_rate = (queued_count - previous_queued_count) / elapsed
            nals_per_second = (nal_count - previous_nal_count) / elapsed
            previous_capture_count = captured_count
            previous_queued_count = queued_count
            previous_nal_count = nal_count
            previous_sample_at = now
            with audio_metrics_lock:
                audio_snapshot = dict(audio_metrics)
            audio_rate = (audio_snapshot["native_accepted_frames"] - previous_audio_frames) / elapsed
            previous_audio_frames = audio_snapshot["native_accepted_frames"]
            audio_snapshot["frames_per_second"] = round(audio_rate, 1)
            audio_snapshot["capture_device"] = audio_command[audio_command.index("--device") + 1] if audio_command else None
            audio_snapshot["capture_command"] = audio_command
            audio_snapshot["capture_process_running"] = audio_capture is not None and audio_capture.poll() is None
            audio_snapshot["gain_percent"] = int(audio.get("capture_gain_percent", 100))
            audio_snapshot["codec"] = str(audio.get("output_codec", "OPUS")).upper()
            audio_snapshot["sample_rate"] = int(audio.get("sample_rate", 48000))
            audio_snapshot["channels"] = int(audio.get("channels", 2))
            audio_snapshot["live555_source_handoffs"] = server.audio_handoff_count()
            logging.info(
                "Audio diagnostic: device=%s running=%s frames/s=%.1f accepted=%d handoffs=%d RMS=%s peak=%s range=[%s,%s] pending=%sB",
                audio_snapshot["capture_device"], audio_snapshot["capture_process_running"],
                audio_rate, audio_snapshot["native_accepted_frames"], audio_snapshot["live555_source_handoffs"], audio_snapshot["rms"],
                audio_snapshot["peak"], audio_snapshot["minimum"], audio_snapshot["maximum"],
                audio_snapshot["pending_bytes"],
            )
            status_metrics = {
                "rtsp_clients": server.client_count(),
                "captured_nals_per_second": captured_rate,
                "queued_nals_per_second": queued_rate,
                "streamed_nals_per_second": nals_per_second,
                "captured_nals_by_type": captured_by_type,
                "queued_nals_by_type": queued_by_type,
                "rtsp_source_nals_by_type": source_by_type,
                "audio_diagnostics": audio_snapshot,
            }
            if first_idr.is_set():
                write_status("ready", camera_status, **status_metrics)
            elif now - started_at > 12:
                raise Live555Error("No H.264 IDR frame with SPS/PPS arrived within 12 seconds.")
            else:
                write_status("starting", camera_status, **status_metrics)
            stop_requested.wait(1)
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        signal.signal(signal.SIGINT, previous_sigint)
        stop_reader.set()
        if audio_capture is not None:
            if audio_capture.poll() is None:
                audio_capture.terminate()
                try:
                    audio_capture.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    audio_capture.kill()
                    audio_capture.wait(timeout=2)
            if audio_capture.stdout is not None:
                audio_capture.stdout.close()
        if audio_reader is not None and audio_reader.is_alive():
            audio_reader.join(timeout=2)
        if capture is not None:
            if capture.poll() is None:
                capture.terminate()
                try:
                    capture.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    capture.kill()
                    capture.wait(timeout=2)
            if capture.stdout is not None:
                capture.stdout.close()
        if reader is not None and reader.is_alive():
            reader.join(timeout=2)
        server.stop()
        write_status("stopped", camera_status)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    camera_status = load_camera_status()
    if not camera_status:
        logging.error("No active native H.264 camera capture was found.")
        return 1
    try:
        return run_server(camera_status)
    except Exception as error:
        logging.exception("Unable to start the H.264 RTSP server")
        try:
            write_status("error", camera_status, str(error))
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
