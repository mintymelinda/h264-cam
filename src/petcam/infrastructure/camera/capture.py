"""Camera capture command construction, independent of the streaming server."""

from __future__ import annotations

import os
import shutil

from .discovery import detect_hardware_h264_encoder


class CaptureError(RuntimeError):
    """Raised when the configured camera cannot provide native H.264."""


def validate_mode(status: dict) -> tuple[int, int, float]:
    try:
        width_text, height_text = str(status.get("resolution", "")).split("x", 1)
        width, height = int(width_text), int(height_text)
        fps = float(status.get("fps"))
    except (ValueError, TypeError) as error:
        raise CaptureError("Configured H.264 capture mode is invalid.") from error
    if not (0 < width <= 65535 and 0 < height <= 65535 and 0 < fps <= 1000):
        raise CaptureError("Configured H.264 capture mode is outside supported limits.")
    return width, height, fps


def _environment_integer(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw, 10)
    except ValueError as error:
        raise CaptureError(f"{name} must be an integer.") from error
    if not minimum <= value <= maximum:
        raise CaptureError(f"{name} must be between {minimum} and {maximum}.")
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
            raise CaptureError(
                "PETCAM_V4L2_CONTROLS must be comma-separated control=value integer pairs."
            )
        if name in seen:
            raise CaptureError(f"PETCAM_V4L2_CONTROLS contains duplicate control {name}.")
        try:
            value = int(raw_value, 10)
        except ValueError as error:
            raise CaptureError(f"V4L2 control {name} must have an integer value.") from error
        if not -(2**31) <= value < 2**31:
            raise CaptureError(f"V4L2 control {name} is outside the signed 32-bit range.")
        seen.add(name)
        controls.append((name, value))
    return controls


def build_capture_command(status: dict) -> list[str]:
    width, height, fps = validate_mode(status)
    if status.get("source") == "USB":
        device = str(status.get("device", ""))
        if not (device.startswith("/dev/video") or device.startswith("/dev/v4l/by-id/")):
            raise CaptureError("Configured USB camera device path is invalid.")
        if not shutil.which("v4l2-ctl"):
            raise CaptureError("v4l2-ctl is missing; install v4l-utils.")
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
    if status.get("source") == "CSI":
        try:
            camera = int(status.get("camera"))
        except (TypeError, ValueError) as error:
            raise CaptureError("Configured CSI camera index is invalid.") from error
        if camera < 0:
            raise CaptureError("Configured CSI camera index is invalid.")
        if not shutil.which("rpicam-vid"):
            raise CaptureError("rpicam-vid is missing; install rpicam-apps-lite.")
        if detect_hardware_h264_encoder().get("available") is not True:
            raise CaptureError("No Raspberry Pi hardware H.264 encoder was detected.")
        intra = _environment_integer("PETCAM_CSI_INTRA", 30, 1, 1000)
        bitrate = _environment_integer("PETCAM_CSI_BITRATE", 8_000_000, 64_000, 100_000_000)
        return [
            "rpicam-vid", "--camera", str(camera), "--timeout", "0",
            "--nopreview", "--codec", "h264", "--inline",
            "--intra", str(intra), "--bitrate", str(bitrate),
            "--width", str(width), "--height", str(height),
            "--framerate", f"{fps:g}", "--output", "-",
        ]
    raise CaptureError("Configured camera source must be USB or CSI.")

