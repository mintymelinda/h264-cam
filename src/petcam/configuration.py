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
    if audio.get("enabled", True) is False:
        normalized["audio"] = {"enabled": False}
        return normalized
    try:
        sample_rate = int(audio.get("sample_rate", 48000))
        channels = int(audio.get("channels", 2))
        output_codec = str(audio.get("output_codec", "OPUS")).upper()
        packet_ms = float(audio.get("packet_ms", 20 if output_codec == "OPUS" else 5))
        capture_gain_percent = int(audio.get("capture_gain_percent", 100))
    except (TypeError, ValueError) as error:
        raise ConfigurationError("Audio settings must be numeric.") from error
    if sample_rate not in {8000, 12000, 16000, 24000, 48000}:
        raise ConfigurationError("PCM sample rate must be 8000, 12000, 16000, 24000, or 48000 Hz.")
    if channels not in {1, 2}:
        raise ConfigurationError("L16 PCM supports mono or stereo input.")
    if output_codec == "OPUS":
        if packet_ms not in {10, 20, 40, 60}:
            raise ConfigurationError("Opus packet duration must be 10, 20, 40, or 60 ms.")
    elif output_codec == "L16":
        if packet_ms != 5:
            raise ConfigurationError("L16 RTP packet duration is fixed at 5 ms to fit stereo payloads within a typical MTU.")
    else:
        raise ConfigurationError("Audio output codec must be OPUS or L16.")
    if not 0 <= capture_gain_percent <= 100:
        raise ConfigurationError("Microphone capture gain must be between 0 and 100 percent.")
    normalized["audio"] = {
        "sample_rate": sample_rate,
        "channels": channels,
        "input_format": str(audio.get("input_format", "S16_LE")),
        "output_codec": output_codec,
        "packet_ms": packet_ms,
        "capture_gain_percent": capture_gain_percent,
        "device": str(audio.get("device", "auto")),
    }
    if normalized["audio"]["input_format"] != "S16_LE":
        raise ConfigurationError("Only S16_LE PCM input is currently supported.")
    # Both codecs consume the same ALSA S16_LE capture stream; encoding is selected natively.
    return normalized
