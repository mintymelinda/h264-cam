"""Host naming for user-facing services advertised on the local network."""

from __future__ import annotations

import os
import socket


def advertised_hostname() -> str:
    """Return the RTSP host clients should use to reach this device.

    Avahi publishes the device system hostname over mDNS. Deployments using
    unicast DNS can override the advertised name with PETCAM_ADVERTISE_HOST.
    """
    override = os.environ.get("PETCAM_ADVERTISE_HOST", "").strip().rstrip(".")
    if override:
        return override

    hostname = socket.gethostname().strip().rstrip(".").lower()
    if not hostname:
        return "localhost"
    if hostname.endswith(".local") or "." in hostname:
        return hostname
    return f"{hostname}.local"
