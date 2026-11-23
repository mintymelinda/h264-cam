#pragma once

#include <algorithm>
#include <cstdint>

namespace petcam {

inline unsigned char encodePcmuSample(std::int16_t sample) noexcept {
  constexpr int kBias = 0x84;
  constexpr int kClip = 32635;

  const int value = static_cast<int>(sample);
  const int sign = value < 0 ? 0x80 : 0x00;
  const int magnitude = std::min(value < 0 ? -value : value, kClip) + kBias;

  int exponent = 7;
  for (int mask = 0x4000; exponent > 0 && (magnitude & mask) == 0; mask >>= 1) {
    --exponent;
  }

  const int mantissa = (magnitude >> (exponent + 3)) & 0x0F;
  return static_cast<unsigned char>(~(sign | (exponent << 4) | mantissa));
}

}  // namespace petcam
