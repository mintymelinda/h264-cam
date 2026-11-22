#!/usr/bin/env python3
"""Probe an RTSP endpoint using RTP interleaved over the RTSP TCP connection."""

from __future__ import annotations

import argparse
import socket
import sys
import time
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

    def _read_interleaved_frame(self, deadline: float) -> tuple[int, bytes]:
        while time.monotonic() < deadline:
            while len(self._buffer) < 4:
                self._socket.settimeout(max(0.05, deadline - time.monotonic()))
                self._receive()
            if self._buffer[0] != ord("$"):
                raise RTSPProbeError("Expected an interleaved RTCP/RTP frame after PLAY.")
            channel = self._buffer[1]
            packet_size = int.from_bytes(self._buffer[2:4], "big")
            while len(self._buffer) < packet_size + 4:
                self._socket.settimeout(max(0.05, deadline - time.monotonic()))
                self._receive()
            packet = bytes(self._buffer[4:packet_size + 4])
            del self._buffer[:packet_size + 4]
            return channel, packet
        raise RTSPProbeError("Timed out waiting for an interleaved RTP frame.")

    @staticmethod
    def _rtp_payload(packet: bytes) -> tuple[int, int, int, bytes]:
        if len(packet) < 12 or packet[0] >> 6 != 2:
            raise RTSPProbeError("Received an invalid RTP packet.")
        padding = bool(packet[0] & 0x20)
        extension = bool(packet[0] & 0x10)
        csrc_count = packet[0] & 0x0F
        header_size = 12 + csrc_count * 4
        if len(packet) < header_size:
            raise RTSPProbeError("RTP packet ended inside its CSRC list.")
        if extension:
            if len(packet) < header_size + 4:
                raise RTSPProbeError("RTP packet ended inside its header extension.")
            extension_words = int.from_bytes(packet[header_size + 2:header_size + 4], "big")
            header_size += 4 + extension_words * 4
        payload_end = len(packet)
        if padding:
            padding_size = packet[-1]
            if padding_size == 0 or padding_size > payload_end - header_size:
                raise RTSPProbeError("RTP packet has invalid padding.")
            payload_end -= padding_size
        if payload_end <= header_size:
            raise RTSPProbeError("RTP packet has an empty payload.")
        sequence = int.from_bytes(packet[2:4], "big")
        timestamp = int.from_bytes(packet[4:8], "big")
        marker = (packet[1] & 0x80) != 0
        return sequence, timestamp, int(marker), packet[header_size:payload_end]

    def wait_for_nal(self, expected_nal: bytes, timeout: float) -> tuple[int, int, int]:
        deadline = time.monotonic() + timeout
        fragments: bytearray | None = None
        fragment_timestamp: int | None = None
        previous_sequence: int | None = None
        fragment_packets = 0

        while time.monotonic() < deadline:
            channel, packet = self._read_interleaved_frame(deadline)
            if channel != 0:
                continue  # Ignore RTCP on interleaved channel 1.
            sequence, timestamp, marker, payload = self._rtp_payload(packet)
            nal_type = payload[0] & 0x1F

            if nal_type == 28:  # H.264 FU-A
                if len(payload) < 3:
                    raise RTSPProbeError("Received a truncated H.264 FU-A packet.")
                fu_header = payload[1]
                start = bool(fu_header & 0x80)
                end = bool(fu_header & 0x40)
                if start:
                    if fragments is not None:
                        raise RTSPProbeError("A new FU-A NAL began before the previous one ended.")
                    fragments = bytearray([(payload[0] & 0xE0) | (fu_header & 0x1F)])
                    fragments.extend(payload[2:])
                    fragment_timestamp = timestamp
                    previous_sequence = sequence
                    fragment_packets = 1
                    continue

                if fragments is None:
                    continue  # Ignore fragments belonging to an earlier NAL.
                if sequence != ((previous_sequence + 1) & 0xFFFF):
                    raise RTSPProbeError("RTP sequence gap inside a fragmented H.264 NAL.")
                if timestamp != fragment_timestamp:
                    raise RTSPProbeError("RTP timestamp changed inside a fragmented H.264 NAL.")
                fragments.extend(payload[2:])
                previous_sequence = sequence
                fragment_packets += 1
                if end:
                    reconstructed = bytes(fragments)
                    if reconstructed != expected_nal:
                        mismatch = next(
                            (i for i, (a, b) in enumerate(zip(reconstructed, expected_nal)) if a != b),
                            min(len(reconstructed), len(expected_nal)),
                        )
                        raise RTSPProbeError(
                            f"FU-A reassembly differed from the source NAL "
                            f"(received {len(reconstructed)} bytes, expected {len(expected_nal)}, "
                            f"first mismatch at byte {mismatch})."
                        )
                    if not marker:
                        raise RTSPProbeError("The final FU-A packet did not carry the H.264 access-unit marker bit.")
                    return fragment_packets, len(reconstructed), timestamp
                continue

            if fragments is None and payload == expected_nal:
                return 1, len(payload), timestamp

        raise RTSPProbeError(
            f"Expected H.264 NAL ({len(expected_nal)} bytes) was not received "
            f"intact within {timeout:g} seconds."
        )


def probe(
    url: str,
    timeout: float = 5.0,
    expected_nal: bytes | None = None,
) -> tuple[int, int, int]:
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
        if expected_nal is not None:
            return connection.wait_for_nal(expected_nal, timeout)
        channel, packet_size = connection.wait_for_rtp(timeout)
        return 1, packet_size, channel
    finally:
        connection.close()


def run_self_test(timeout: float) -> tuple[int, int, int]:
    from petcam.infrastructure.streaming.live555 import Live555Server

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    server = Live555Server()
    server.start(port, "/petcam")
    try:
        # A large synthetic IDR forces Live555 to fragment the NAL into FU-A
        # RTP packets. The TCP probe reassembles those packets and compares
        # every NAL byte with this source buffer.
        large_idr = bytes([0x65, 0x80]) + bytes([0x55]) * (256 * 1024 - 2)
        server.push_nal(bytes.fromhex("6742001f95a814016e40"))
        server.push_nal(bytes.fromhex("68ce06e2"))
        server.push_nal(large_idr, is_idr=True)
        server.push_nal(bytes.fromhex("41803344"))
        return probe(f"rtsp://127.0.0.1:{port}/petcam", timeout, large_idr)
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
        packet_count, nal_size, timestamp = (
            run_self_test(args.timeout)
            if args.self_test
            else probe(args.url, args.timeout)
        )
    except (OSError, RTSPProbeError, RuntimeError) as error:
        print(f"RTSP/TCP probe FAILED: {error}", file=sys.stderr)
        return 1
    if args.self_test:
        print(
            "RTSP/TCP probe PASSED: OPTIONS, DESCRIBE, TCP SETUP, PLAY, "
            f"and byte-exact FU-A reassembly of a {nal_size}-byte NAL "
            f"across {packet_count} RTP packets (timestamp {timestamp})."
        )
    else:
        print(
            "RTSP/TCP probe PASSED: OPTIONS, DESCRIBE, TCP SETUP, PLAY, "
            f"and interleaved RTP channel {timestamp} ({nal_size} bytes)."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
