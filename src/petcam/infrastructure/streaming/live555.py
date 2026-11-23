#!/usr/bin/env python3
"""Capture native H.264 NAL units and publish them through Live555."""

from __future__ import annotations

import ctypes
import json
import logging
from collections import Counter
import os
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



def build_audio_capture_command() -> list[str] | None:
    """Capture mono 8 kHz signed PCM for the Live555 PCMU audio track."""
    if os.environ.get("PETCAM_AUDIO", "1").strip().lower() in {"0", "false", "no", "off"}:
        return None
    if not shutil.which("arecord"):
        raise Live555Error("arecord is missing; install alsa-utils to enable microphone audio.")
    device = os.environ.get("PETCAM_AUDIO_DEVICE", "default").strip()
    if not device or any(char in device for char in "\r\n"):
        raise Live555Error("PETCAM_AUDIO_DEVICE must be a non-empty ALSA capture device name.")
    return [
        "arecord", "--quiet", "--device", device,
        "--format", "S16_LE", "--rate", "8000", "--channels", "1",
        "--file-type", "raw", "--period-size", "160", "--buffer-size", "640",
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
        self.library.petcam_start.argtypes = [ctypes.c_ushort, ctypes.c_char_p]
        self.library.petcam_start.restype = ctypes.c_int
        self.library.petcam_push_nal.argtypes = [
            ctypes.POINTER(ctypes.c_ubyte), ctypes.c_size_t, ctypes.c_int
        ]
        self.library.petcam_push_nal.restype = ctypes.c_int        self.library.petcam_push_audio_pcm.argtypes = [
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
        self.library.petcam_nal_type_count.argtypes = [ctypes.c_uint]
        self.library.petcam_nal_type_count.restype = ctypes.c_ulonglong

    def push_audio_pcm(self, pcm: bytes) -> None:
        if len(pcm) != 320:
            raise Live555Error("Microphone PCM frames must contain exactly 320 bytes.")
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

    def read_audio_capture() -> None:
        assert audio_capture is not None and audio_capture.stdout is not None
        try:
            while not stop_reader.is_set():
                pcm = audio_capture.stdout.read(320)
                if not pcm:
                    break
                if len(pcm) == 320:
                    server.push_audio_pcm(pcm)
        except (OSError, Live555Error):
            if not stop_reader.is_set():
                logging.exception("Microphone audio capture failed")

    logging.info("Using Live555 H.264 framer mode: %s", framer_mode)
    started_at = time.monotonic()
    previous_capture_count = 0
    previous_queued_count = 0
    previous_nal_count = 0
    previous_sample_at = started_at
    try:
        server.start()
        server.clear_audio()
        audio_command = build_audio_capture_command()
        if audio_command is not None:
            try:
                audio_capture = subprocess.Popen(
                    audio_command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, bufsize=0,
                )
                if audio_capture.stdout is None:
                    raise Live555Error("Unable to open the microphone capture pipe.")
                audio_reader = threading.Thread(
                    target=read_audio_capture, name="petcam-audio-capture", daemon=True
                )
                audio_reader.start()
                logging.info(
                    "Microphone audio enabled from ALSA device %s",
                    os.environ.get("PETCAM_AUDIO_DEVICE", "default"),
                )
            except (OSError, Live555Error) as error:
                logging.warning("Microphone audio is unavailable; continuing with video only: %s", error)
                if audio_capture is not None:
                    audio_capture.terminate()
                    audio_capture = None
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
            status_metrics = {
                "rtsp_clients": server.client_count(),
                "captured_nals_per_second": captured_rate,
                "queued_nals_per_second": queued_rate,
                "streamed_nals_per_second": nals_per_second,
                "captured_nals_by_type": captured_by_type,
                "queued_nals_by_type": queued_by_type,
                "rtsp_source_nals_by_type": source_by_type,
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
