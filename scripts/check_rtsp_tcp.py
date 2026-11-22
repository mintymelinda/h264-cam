#!/usr/bin/env python3
"""Probe an RTSP endpoint using RTP interleaved over the RTSP TCP connection."""

from __future__ import annotations

import argparse
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit


class RTSPProbeError(RuntimeError):
    """Raised when RTSP/TCP negotiation or interleaved media fails."""


class RTSPConnection:
    def __init__(self, url: str, timeout: float):
        self.url = url
        self.timeout = timeout
        parsed = urlsplit(url)
        if parsed.scheme.lower() != "rtsp" or not parsed.hostname:
            raise RTSPProbeError("URL must be an rtsp:// URL with a hostname.")
        if parsed.username or parsed.password:
            raise RTSPProbeError("Credential-bearing RTSP URLs are not supported by this probe.")
        self.host = parsed.hostname
        self.port = parsed.port or 554
        self.stream_url = url
        self._socket = socket.create_connection((self.host, self.port), timeout=timeout)
        self._socket.settimeout(timeout)
        self._buffer = bytearray()
        self._cseq = 0

    def close(self) -> None:
        self._socket.close()

    def _receive(self) -> None:
        chunk = self._socket.recv(65536)
        if not chunk:
            raise RTSPProbeError("RTSP server closed the TCP connection.")
        self._buffer.extend(chunk)

    def _read_exactly(self, size: int) -> bytes:
        while len(self._buffer) < size:
            self._receive()
        result = bytes(self._buffer[:size])
        del self._buffer[:size]
        return result

    def _read_response(self) -> tuple[int, dict[str, str], bytes]:
        while b"\r\n\r\n" not in self._buffer:
            self._receive()
        header_end = self._buffer.index(b"\r\n\r\n")
        raw_headers = bytes(self._buffer[:header_end])
        del self._buffer[:header_end + 4]
        lines = raw_headers.decode("latin-1").split("\r\n")
        status_parts = lines[0].split(" ", 2)
        if len(status_parts) < 2 or not status_parts[1].isdigit():
            raise RTSPProbeError(f"Invalid RTSP status line: {lines[0]!r}")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, separator, value = line.partition(":")
            if separator:
                headers[name.strip().lower()] = value.strip()
        content_length = int(headers.get("content-length", "0"))
        body = self._read_exactly(content_length)
        return int(status_parts[1]), headers, body

    def request(
        self,
        method: str,
        target: str,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        self._cseq += 1
        port_suffix = f":{self.port}" if self.port != 554 else ""
        request_headers = {
            "CSeq": str(self._cseq),
            "User-Agent": "PetCam-RTSP-TCP-Probe/1.0",
            "Host": f"{self.host}{port_suffix}",
        }
        if headers:
            request_headers.update(headers)
        request = (
            f"{method} {target} RTSP/1.0\r\n"
            + "".join(f"{name}: {value}\r\n" for name, value in request_headers.items())
            + "\r\n"
        )
        self._socket.sendall(request.encode("latin-1"))
        return self._read_response()

    def wait_for_rtp(self, timeout: float) -> tuple[int, int]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self._buffer) >= 4 and self._buffer[0] == ord("$"):
                channel = self._buffer[1]
                packet_size = int.from_bytes(self._buffer[2:4], "big")
                if len(self._buffer) >= packet_size + 4:
                    del self._buffer[:4]
                    del self._buffer[:packet_size]
                    if channel == 0:
                        return channel, packet_size
                    continue
            remaining = max(0.05, deadline - time.monotonic())
            self._socket.settimeout(remaining)
            try:
                self._receive()
            except socket.timeout:
                break
        raise RTSPProbeError(
            f"RTSP/TCP SETUP and PLAY succeeded, but no interleaved RTP packet "
            f"arrived within {timeout:g} seconds."
        )


def probe(url: str, timeout: float = 5.0) -> tuple[int, int]:
    connection = RTSPConnection(url, timeout)
    try:
        status, _, _ = connection.request("OPTIONS", url)
        if status != 200:
            raise RTSPProbeError(f"OPTIONS failed with RTSP status {status}.")

        status, describe_headers, sdp = connection.request(
            "DESCRIBE", url, {"Accept": "application/sdp"}
        )
        if status != 200:
            raise RTSPProbeError(f"DESCRIBE failed with RTSP status {status}.")

        control = next(
            (
                line.partition(":")[2].strip()
                for line in sdp.decode("utf-8", "replace").splitlines()
                if line.lower().startswith("a=control:")
                and line.partition(":")[2].strip() != "*"
            ),
            None,
        )
        if not control:
            raise RTSPProbeError("DESCRIBE SDP did not advertise a media control URL.")
        content_base = describe_headers.get("content-base", url.rstrip("/") + "/")
        if not content_base.endswith("/"):
            content_base += "/"
        track_url = urljoin(content_base, control)

        status, setup_headers, _ = connection.request(
            "SETUP",
            track_url,
            {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"},
        )
        if status != 200:
            raise RTSPProbeError(
                f"TCP SETUP failed with RTSP status {status}; "
                "the server did not accept interleaved RTP."
            )
        selected_transport = setup_headers.get("transport", "")
        if "RTP/AVP/TCP" not in selected_transport.upper() or "interleaved=0-1" not in selected_transport.lower():
            raise RTSPProbeError(
                f"Server returned an unexpected TCP Transport header: {selected_transport!r}"
            )
        session = setup_headers.get("session", "").split(";", 1)[0]
        if not session:
            raise RTSPProbeError("TCP SETUP succeeded without an RTSP Session header.")

        status, _, _ = connection.request(
            "PLAY", url, {"Session": session, "Range": "npt=0.000-"}
        )
        if status != 200:
            raise RTSPProbeError(f"PLAY failed with RTSP status {status}.")
        return connection.wait_for_rtp(timeout)
    finally:
        connection.close()


def run_self_test(timeout: float) -> tuple[int, int]:
    source_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(source_root / "src"))
    from petcam.infrastructure.streaming.live555 import Live555Server

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    server = Live555Server()
    server.start(port, "/petcam")
    try:
        # Small Annex-B NAL payloads are sufficient to exercise Live555's
        # H.264 framing, TCP SETUP negotiation, PLAY, and RTP interleaving.
        server.push_nal(bytes.fromhex("6742001f95a814016e40"))
        server.push_nal(bytes.fromhex("68ce06e2"))
        server.push_nal(bytes.fromhex("65888421a0"), is_idr=True)
        for _ in range(8):
            server.push_nal(bytes.fromhex("419a2211"))
            server.push_nal(bytes.fromhex("65888421a0"), is_idr=True)
        return probe(f"rtsp://127.0.0.1:{port}/petcam", timeout)
    finally:
        server.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "url",
        nargs="?",
        help="RTSP URL to probe, e.g. rtsp://kittycam.local:8554/petcam",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="seconds to wait for the first interleaved RTP packet (default: 5)",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="start the installed native Live555 library locally and run the TCP probe",
    )
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if args.self_test == bool(args.url):
        parser.error("provide exactly one RTSP URL or --self-test")

    try:
        channel, packet_size = (
            run_self_test(args.timeout)
            if args.self_test
            else probe(args.url, args.timeout)
        )
    except (OSError, RTSPProbeError, RuntimeError) as error:
        print(f"RTSP/TCP probe FAILED: {error}", file=sys.stderr)
        return 1
    print(
        "RTSP/TCP probe PASSED: OPTIONS, DESCRIBE, TCP SETUP, PLAY, "
        f"and interleaved RTP channel {channel} ({packet_size} bytes)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
