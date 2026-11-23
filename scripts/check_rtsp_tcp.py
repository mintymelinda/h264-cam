#!/usr/bin/env python3
"""Probe an RTSP endpoint using RTP interleaved over the RTSP TCP connection."""

from __future__ import annotations

import argparse
import socket
import sys
import threading
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
        self._interleaved_frames: list[tuple[int, bytes]] = []
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

    def _extract_interleaved_frame(self) -> tuple[int, bytes] | None:
        if not self._buffer or self._buffer[0] != ord("$"):
            return None
        while len(self._buffer) < 4:
            self._receive()
        channel = self._buffer[1]
        packet_size = int.from_bytes(self._buffer[2:4], "big")
        while len(self._buffer) < packet_size + 4:
            self._receive()
        packet = bytes(self._buffer[4:packet_size + 4])
        del self._buffer[:packet_size + 4]
        return channel, packet

    def _read_response(self) -> tuple[int, dict[str, str], bytes]:
        while True:
            # RTP/RTCP interleaved frames can arrive between RTSP responses.
            # Preserve them instead of treating their '$' prefix as an RTSP line.
            frame = self._extract_interleaved_frame()
            if frame is not None:
                self._interleaved_frames.append(frame)
                continue
            if b"\r\n\r\n" in self._buffer:
                break
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
        channel, packet = self._read_interleaved_frame(deadline, channel=0)
        if len(packet) < 12:
            raise RTSPProbeError("Received an invalid RTP packet.")
        return channel, len(packet)

    def wait_for_audio(
        self, timeout: float, payload_type: int, channel: int = 2
    ) -> tuple[int, int]:
        deadline = time.monotonic() + timeout
        try:
            channel, packet = self._read_interleaved_frame(deadline, channel=channel)
        except RTSPProbeError as error:
            raise RTSPProbeError(f"Microphone audio RTP did not arrive: {error}") from error
        _, timestamp, _, received_payload_type, payload = self._rtp_payload(packet)
        if received_payload_type != payload_type:
            raise RTSPProbeError(
                f"Expected dynamic RTP payload type {payload_type}, received {received_payload_type}."
            )
        if len(payload) < 2:
            raise RTSPProbeError(
                f"Expected a non-empty audio packet, received {len(payload)} bytes."
            )
        return len(payload), timestamp

    def _read_interleaved_frame(
        self,
        deadline: float,
        channel: int | None = None,
    ) -> tuple[int, bytes]:
        while time.monotonic() < deadline:
            for index, frame in enumerate(self._interleaved_frames):
                if channel is None or frame[0] == channel:
                    del self._interleaved_frames[index]
                    return frame
            frame = self._extract_interleaved_frame()
            if frame is not None:
                if channel is None or frame[0] == channel:
                    return frame
                self._interleaved_frames.append(frame)
                continue
            self._socket.settimeout(max(0.05, deadline - time.monotonic()))
            try:
                self._receive()
            except socket.timeout:
                break
        track = "any" if channel is None else str(channel)
        raise RTSPProbeError(
            f"Timed out waiting for an interleaved RTP frame on TCP channel {track}."
        )

    @staticmethod
    def _rtp_payload(packet: bytes) -> tuple[int, int, int, int, bytes]:
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
        payload_type = packet[1] & 0x7F
        return sequence, timestamp, int(marker), payload_type, packet[header_size:payload_end]

    def wait_for_nal(self, expected_nal: bytes, timeout: float) -> tuple[int, int, int]:
        deadline = time.monotonic() + timeout
        fragments: bytearray | None = None
        fragment_timestamp: int | None = None
        previous_sequence: int | None = None
        fragment_packets = 0

        while time.monotonic() < deadline:
            try:
                channel, packet = self._read_interleaved_frame(deadline, channel=0)
            except RTSPProbeError as error:
                raise RTSPProbeError(f"Video RTP did not arrive: {error}") from error
            sequence, timestamp, marker, _payload_type, payload = self._rtp_payload(packet)
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
    require_audio: bool = False,
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

        sdp_lines = sdp.decode("utf-8", "replace").splitlines()
        control = None
        audio_control = None
        audio_payload_type = None
        audio_codec = None
        audio_payload_types: set[str] = set()
        current_media = None
        for line in sdp_lines:
            if line.lower().startswith("m="):
                media_fields = line[2:].split()
                current_media = media_fields[0].lower() if media_fields else None
                audio_payload_types = set(media_fields[3:]) if current_media == "audio" else set()
            elif line.lower().startswith("a=control:"):
                media_control = line.partition(":")[2].strip()
                if media_control == "*":
                    continue
                if current_media == "audio":
                    audio_control = media_control
                elif current_media == "video" and control is None:
                    control = media_control
            elif current_media == "audio" and line.lower().startswith("a=rtpmap:"):
                payload_field, separator, codec = line.partition(":")[2].partition(" ")
                if (
                    separator
                    and payload_field in audio_payload_types
                    and codec.strip().upper().split("/", 1)[0] in {"L16", "OPUS"}
                ):
                    audio_payload_type = int(payload_field)
                    audio_codec = codec.strip().upper().split("/", 1)[0]

        if not control:
            raise RTSPProbeError("DESCRIBE SDP did not advertise a video media control URL.")
        if require_audio and (
            not audio_control
            or audio_payload_type is None
            or not 96 <= audio_payload_type <= 127
        ):
            raise RTSPProbeError(
                "DESCRIBE SDP did not advertise an Opus or L16 microphone audio track."
            )
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

        if require_audio:
            audio_track_url = urljoin(content_base, audio_control)
            audio_status, audio_headers, _ = connection.request(
                "SETUP",
                audio_track_url,
                {
                    "Transport": "RTP/AVP/TCP;unicast;interleaved=2-3",
                    "Session": session,
                },
            )
            if audio_status != 200:
                raise RTSPProbeError(
                    f"Audio TCP SETUP failed with RTSP status {audio_status}."
                )
            audio_transport = audio_headers.get("transport", "")
            if "RTP/AVP/TCP" not in audio_transport.upper() or "interleaved=2-3" not in audio_transport.lower():
                raise RTSPProbeError(
                    f"Server returned an unexpected audio TCP Transport header: {audio_transport!r}"
                )
            audio_session = audio_headers.get("session", "").split(";", 1)[0]
            if audio_session and audio_session != session:
                raise RTSPProbeError("Video and audio tracks were assigned different RTSP sessions.")

        status, _, _ = connection.request(
            "PLAY", url, {"Session": session, "Range": "npt=0.000-"}
        )
        if status != 200:
            raise RTSPProbeError(f"PLAY failed with RTSP status {status}.")
        if expected_nal is not None:
            video_result = connection.wait_for_nal(expected_nal, timeout)
            if require_audio:
                connection.wait_for_audio(timeout, payload_type=audio_payload_type)
            return video_result
        channel, packet_size = connection.wait_for_rtp(timeout)
        if require_audio:
            connection.wait_for_audio(timeout, channel=2, payload_type=audio_payload_type)
        return 1, packet_size, channel
    finally:
        connection.close()


def probe_audio(url: str, timeout: float = 5.0) -> tuple[int, int]:
    """Verify the RTSP microphone track using synthetic audio only (no camera)."""
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

        audio_control = None
        audio_payload_types: set[str] = set()
        audio_payload_type = None
        for line in sdp.decode("utf-8", "replace").splitlines():
            if line.lower().startswith("m="):
                media_fields = line[2:].split()
                current_media = media_fields[0].lower() if media_fields else None
                audio_payload_types = set(media_fields[3:]) if current_media == "audio" else set()
            elif line.lower().startswith("a=control:") and current_media == "audio":
                value = line.partition(":")[2].strip()
                if value != "*":
                    audio_control = value
            elif current_media == "audio" and line.lower().startswith("a=rtpmap:"):
                payload_field, separator, codec = line.partition(":")[2].partition(" ")
                if separator and payload_field in audio_payload_types and codec.strip().upper().split("/", 1)[0] in {"L16", "OPUS"}:
                    audio_payload_type = int(payload_field)

        if not audio_control or audio_payload_type is None or not 96 <= audio_payload_type <= 127:
            raise RTSPProbeError("DESCRIBE SDP did not advertise an Opus or L16 microphone audio track.")

        content_base = describe_headers.get("content-base", url.rstrip("/") + "/")
        if not content_base.endswith("/"):
            content_base += "/"
        audio_track_url = urljoin(content_base, audio_control)
        status, setup_headers, _ = connection.request(
            "SETUP",
            audio_track_url,
            {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"},
        )
        if status != 200:
            raise RTSPProbeError(
                f"Audio TCP SETUP failed with RTSP status {status}."
            )
        transport = setup_headers.get("transport", "")
        if "RTP/AVP/TCP" not in transport.upper() or "interleaved=0-1" not in transport.lower():
            raise RTSPProbeError(
                f"Server returned an unexpected audio TCP Transport header: {transport!r}"
            )
        session = setup_headers.get("session", "").split(";", 1)[0]
        if not session:
            raise RTSPProbeError("Audio TCP SETUP succeeded without an RTSP Session header.")

        status, _, _ = connection.request(
            "PLAY", url, {"Session": session, "Range": "npt=0.000-"}
        )
        if status != 200:
            raise RTSPProbeError(f"PLAY failed with RTSP status {status}.")

        return connection.wait_for_audio(
            timeout, channel=0, payload_type=audio_payload_type
        )
    finally:
        connection.close()


def run_self_test(timeout: float) -> tuple[int, int, int]:
    """Exercise Live555 with synthetic PCM; deliberately requires no camera or ALSA."""
    from petcam.infrastructure.streaming.live555 import Live555Server

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    server = Live555Server()
    server.configure_audio({"sample_rate": 48000, "channels": 2, "output_codec": "OPUS", "packet_ms": 20})
    server.start(port, "/petcam")
    stop_audio = threading.Event()

    def publish_test_audio() -> None:
        frame = bytes(3840)  # 20 ms of synthetic stereo S16_LE silence at 48 kHz.
        next_frame = time.monotonic()
        while not stop_audio.is_set():
            server.push_audio_pcm(frame)
            next_frame += 0.020
            stop_audio.wait(max(0.0, next_frame - time.monotonic()))

    audio_thread = threading.Thread(target=publish_test_audio, name="opus-self-test", daemon=True)
    try:
        audio_thread.start()
        payload_size, timestamp = probe_audio(
            f"rtsp://127.0.0.1:{port}/petcam",
            timeout,
        )
        return 1, payload_size, timestamp
    finally:
        stop_audio.set()
        if audio_thread.is_alive():
            audio_thread.join(timeout=1)
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
    parser.add_argument(
        "--require-audio",
        action="store_true",
        help="also require an Opus or L16 microphone track and receive an audio RTP packet",
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
            else probe(args.url, args.timeout, require_audio=args.require_audio)
        )
    except (OSError, RTSPProbeError, RuntimeError) as error:
        print(f"RTSP/TCP probe FAILED: {error}", file=sys.stderr)
        return 1
    if args.self_test:
        print(
            "RTSP audio self-test PASSED: OPTIONS, DESCRIBE, audio TCP SETUP, "
            f"PLAY, and a live {nal_size}-byte Opus RTP payload (timestamp {timestamp}). "
            "The test uses synthetic audio and does not require a camera or microphone."
        )
    else:
        print(
            "RTSP/TCP probe PASSED: OPTIONS, DESCRIBE, TCP SETUP, PLAY, "
            f"and interleaved RTP channel {timestamp} ({nal_size} bytes)."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
