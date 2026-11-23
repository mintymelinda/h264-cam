"""Validate the camera parameters used by the H.264 RTSP session."""

from __future__ import annotations

import re
from typing import Any

RESOLUTION_RE = re.compile(r"([1-9][0-9]{0,4})x([1-9][0-9]{0,4})")


class ConfigurationError(ValueError):
    """Raised when the requested camera mode is invalid."""


def validate_config(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError("Camera configuration must be an object.")
    source = str(value.get("source", "")).upper()
    if source not in {"USB", "CSI"}:
        raise ConfigurationError("Capture mode must be USB or CSI.")
    resolution = str(value.get("resolution", ""))
    if not RESOLUTION_RE.fullmatch(resolution):
        raise ConfigurationError("Resolution must use WIDTHxHEIGHT format.")
    try:
        fps = float(value.get("fps"))
    except (TypeError, ValueError) as error:
        raise ConfigurationError("Frame rate must be a positive number.") from error
    if not 0 < fps <= 1000:
        raise ConfigurationError("Frame rate must be greater than 0 and no more than 1000 FPS.")
    normalized: dict[str, Any] = {
        "source": source,
        "resolution": resolution,
        "fps": int(fps) if fps.is_integer() else fps,
    }
    if source == "USB":
        device = str(value.get("device", ""))
        if not re.fullmatch(r"/dev/(?:video[0-9]+|v4l/by-id/[A-Za-z0-9._:+-]+)", device):
            raise ConfigurationError("USB camera device path is invalid.")
        normalized["device"] = device
        normalized["physical_path"] = str(value.get("physical_path", ""))
    else:
        try:
            camera = int(value.get("camera"))
        except (TypeError, ValueError) as error:
            raise ConfigurationError("CSI camera index is invalid.") from error
        if camera < 0:
            raise ConfigurationError("CSI camera index cannot be negative.")
        normalized["camera"] = camera
        normalized["sensor"] = str(value.get("sensor", ""))
        normalized["device_path"] = str(value.get("device_path", ""))
    audio = value.get("audio", {})
    if not isinstance(audio, dict):
        raise ConfigurationError("Audio configuration must be an object.")
    try:
        sample_rate = int(audio.get("sample_rate", 48000))
        channels = int(audio.get("channels", 2))
        bitrate = int(audio.get("bitrate", 128000))
        packet_ms = float(audio.get("packet_ms", 20))
    except (TypeError, ValueError) as error:
        raise ConfigurationError("Audio settings must be numeric.") from error
    if sample_rate not in {8000, 12000, 16000, 24000, 48000}:
        raise ConfigurationError("Opus input sample rate must be 8000, 12000, 16000, 24000, or 48000 Hz.")
    if channels not in {1, 2}:
        raise ConfigurationError("Opus supports mono or stereo input.")
    if not 6000 <= bitrate <= 510000:
        raise ConfigurationError("Opus bitrate must be between 6000 and 510000 bit/s.")
    if packet_ms not in {2.5, 5, 10, 20, 40, 60}:
        raise ConfigurationError("Opus packet duration must be 2.5, 5, 10, 20, 40, or 60 ms.")
    normalized["audio"] = {
        "sample_rate": sample_rate,
        "channels": channels,
        "input_format": str(audio.get("input_format", "S16_LE")),
        "output_codec": str(audio.get("output_codec", "OPUS")).upper(),
        "bitrate": bitrate,
        "packet_ms": packet_ms,
    }
    if normalized["audio"]["input_format"] != "S16_LE":
        raise ConfigurationError("Only S16_LE PCM input is currently supported.")
    if normalized["audio"]["output_codec"] != "OPUS":
        raise ConfigurationError("Only the Opus output codec is currently supported.")
    return normalized
