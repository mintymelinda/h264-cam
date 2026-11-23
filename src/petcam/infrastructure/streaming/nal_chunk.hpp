#pragma once

#include <algorithm>
#include <cstddef>
#include <cstring>
#include <chrono>
#include <vector>

namespace petcam {

inline bool isNalQueueEntryExpired(
    std::chrono::steady_clock::time_point now,
    std::chrono::steady_clock::time_point enqueuedAt,
    std::chrono::steady_clock::duration maxAge) {
  return now - enqueuedAt >= maxAge;
}

struct NalChunkResult {
  std::size_t size;
  bool complete;
};

// Copy the next section of one NAL into a bounded Live555 read buffer.
// In byte-stream mode, the Annex-B start code is part of the same logical
// byte sequence and may itself be split across callbacks.
inline NalChunkResult copyNalChunk(
    const std::vector<unsigned char>& nal,
    std::size_t& offset,
    bool includeStartCode,
    unsigned char* destination,
    std::size_t capacity) {
  static constexpr unsigned char startCode[] = {0x00, 0x00, 0x00, 0x01};
  const std::size_t prefixSize = includeStartCode ? sizeof(startCode) : 0;
  const std::size_t totalSize = prefixSize + nal.size();

  if (nal.empty() || destination == nullptr || capacity == 0 || offset >= totalSize) {
    return {0, false};
  }

  const std::size_t count = std::min(capacity, totalSize - offset);
  std::size_t prefixCopied = 0;

  if (offset < prefixSize) {
    prefixCopied = std::min(count, prefixSize - offset);
    std::memcpy(destination, startCode + offset, prefixCopied);
  }

  if (count > prefixCopied) {
    const std::size_t nalOffset = offset + prefixCopied - prefixSize;
    std::memcpy(destination + prefixCopied, nal.data() + nalOffset, count - prefixCopied);
  }

  offset += count;
  return {count, offset == totalSize};
}

}  // namespace petcam
