"""Frame an Annex-B H.264 byte stream into complete access units without decoding."""

from __future__ import annotations

MAX_NAL_BYTES = 16 * 1024 * 1024
MAX_ACCESS_UNIT_BYTES = 32 * 1024 * 1024
_START_3 = b"\x00\x00\x01"
_START_4 = b"\x00\x00\x00\x01"


class H264FramingError(ValueError):
    """Raised when an encoded H.264 boundary cannot be identified safely."""


def _find_start_code(data: bytearray, start: int = 0) -> tuple[int, int] | None:
    """Find the next Annex-B prefix using the bytearray's optimized search."""
    index = data.find(_START_3, start)
    if index < 0:
        return None
    if index > 0 and data[index - 1] == 0:
        return index - 1, 4
    return index, 3


class AnnexBNalFramer:
    """Split arbitrary read chunks into complete Annex-B NAL units.

    Search progress is retained between reads. Rescanning a growing IDR NAL
    from byte zero on every pipe read made the previous implementation
    quadratic in NAL size and could consume an entire ARM core.
    """

    def __init__(self):
        self.buffer = bytearray()
        self._scan_offset = 0
        self._start_code_length = 0

    def feed(self, chunk: bytes) -> list[bytes]:
        if not chunk:
            return []
        self.buffer.extend(chunk)
        output = []

        if not self._start_code_length:
            first = _find_start_code(self.buffer)
            if first is None:
                if len(self.buffer) > 3:
                    del self.buffer[:-3]
                return output
            start, self._start_code_length = first
            if start:
                del self.buffer[:start]
            self._scan_offset = self._start_code_length

        while True:
            next_start = _find_start_code(self.buffer, self._scan_offset)
            if next_start is None:
                if len(self.buffer) > MAX_NAL_BYTES:
                    raise H264FramingError("H.264 NAL unit exceeded the size limit.")
                # Only the final three bytes can begin a prefix that crosses
                # the next read boundary. Everything before them was scanned.
                self._scan_offset = max(self._scan_offset, len(self.buffer) - 3)
                break

            boundary, next_length = next_start
            if boundary > self._start_code_length:
                output.append(bytes(self.buffer[:boundary]))
            del self.buffer[:boundary]
            self._start_code_length = next_length
            self._scan_offset = next_length

        return output

    def flush(self) -> list[bytes]:
        if not self._start_code_length:
            self.buffer.clear()
            self._scan_offset = 0
            return []
        if len(self.buffer) <= self._start_code_length:
            self.buffer.clear()
            output = []
        else:
            output = [bytes(self.buffer)]
            self.buffer.clear()
        self._scan_offset = 0
        self._start_code_length = 0
        return output


def _nal_header(nal: bytes) -> tuple[int, int]:
    if nal.startswith(_START_4):
        offset = 4
    elif nal.startswith(_START_3):
        offset = 3
    else:
        raise H264FramingError("H.264 NAL is missing its Annex-B start code.")
    if len(nal) <= offset:
        raise H264FramingError("H.264 NAL is missing its header.")
    return offset, nal[offset] & 0x1F


def first_mb_in_slice(nal: bytes) -> int | None:
    """Read only first_mb_in_slice; no video decoding or codec conversion occurs."""
    offset, nal_type = _nal_header(nal)
    if nal_type not in (1, 2, 5):
        return None

    rbsp = bytearray()
    zero_count = 0
    for value in nal[offset + 1:]:
        if zero_count >= 2 and value == 0x03:
            continue
        rbsp.append(value)
        zero_count = zero_count + 1 if value == 0 else 0

    bit_position = 0

    def read_bit() -> int:
        nonlocal bit_position
        if bit_position >= len(rbsp) * 8:
            raise H264FramingError("Truncated H.264 slice header.")
        value = (rbsp[bit_position // 8] >> (7 - bit_position % 8)) & 1
        bit_position += 1
        return value

    leading_zero_bits = 0
    while read_bit() == 0:
        leading_zero_bits += 1
        if leading_zero_bits > 31:
            raise H264FramingError("Invalid H.264 first_mb_in_slice value.")

    suffix = 0
    for _ in range(leading_zero_bits):
        suffix = (suffix << 1) | read_bit()
    return (1 << leading_zero_bits) - 1 + suffix


class H264AccessUnitFramer:
    """Group NAL units into access units suitable for rtph264pay alignment=au."""

    _PREFIX_NAL_TYPES = {6, 7, 8, 14, 15, 16, 17, 18, 19, 20, 21}

    def __init__(self):
        self.nals = AnnexBNalFramer()
        self.current: list[bytes] = []
        self.current_size = 0
        self.has_vcl = False

    def _take_access_unit(self) -> bytes | None:
        if not self.current or not self.has_vcl:
            self.current.clear()
            self.current_size = 0
            self.has_vcl = False
            return None
        result = b"".join(self.current)
        self.current.clear()
        self.current_size = 0
        self.has_vcl = False
        return result

    def _append(self, nal: bytes) -> None:
        self.current.append(nal)
        self.current_size += len(nal)
        if self.current_size > MAX_ACCESS_UNIT_BYTES:
            raise H264FramingError("H.264 access unit exceeded the size limit.")

    def _consume_nals(self, nals: list[bytes]) -> list[bytes]:
        output = []
        for nal in nals:
            _offset, nal_type = _nal_header(nal)
            if nal_type == 9:  # AUD marks the beginning of an access unit.
                if self.has_vcl:
                    previous = self._take_access_unit()
                    if previous is not None:
                        output.append(previous)
                self._append(nal)
                continue

            if nal_type in (1, 2, 5):  # Coded slice; first_mb==0 starts a new picture.
                first_mb = first_mb_in_slice(nal)
                if self.has_vcl and first_mb == 0:
                    previous = self._take_access_unit()
                    if previous is not None:
                        output.append(previous)
                self._append(nal)
                self.has_vcl = True
                continue

            if self.has_vcl and nal_type in self._PREFIX_NAL_TYPES:
                previous = self._take_access_unit()
                if previous is not None:
                    output.append(previous)
            self._append(nal)
        return output

    def feed(self, chunk: bytes) -> list[bytes]:
        return self._consume_nals(self.nals.feed(chunk))

    def flush(self) -> list[bytes]:
        output = self._consume_nals(self.nals.flush())
        final = self._take_access_unit()
        if final is not None:
            output.append(final)
        else:
            self.current.clear()
            self.current_size = 0
        return output



def inspect_h264_access_unit(access_unit: bytes) -> dict:
    """Extract in-band parameter sets and IDR presence without decoding."""
    framer = AnnexBNalFramer()
    nals = framer.feed(access_unit)
    nals.extend(framer.flush())
    result = {"sps": b"", "pps": b"", "has_sps": False, "has_pps": False, "has_idr": False}
    for nal in nals:
        _offset, nal_type = _nal_header(nal)
        if nal_type == 7:
            result["sps"] = nal
            result["has_sps"] = True
        elif nal_type == 8:
            result["pps"] = nal
            result["has_pps"] = True
        elif nal_type == 5:
            result["has_idr"] = True
    return result
