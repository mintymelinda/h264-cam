#include "pcmu.hpp"

#include <cassert>
#include <cstdint>

int main() {
  // ITU-T G.711 μ-law reference points.
  assert(petcam::encodePcmuSample(0) == 0xFF);
  assert(petcam::encodePcmuSample(1000) == 0xCE);
  assert(petcam::encodePcmuSample(-1000) == 0x4E);
  assert(petcam::encodePcmuSample(32767) == 0x80);
  assert(petcam::encodePcmuSample(-32768) == 0x00);

  // The encoder must preserve sign and be monotonic across representative levels.
  const auto positiveQuiet = petcam::encodePcmuSample(100);
  const auto positiveLoud = petcam::encodePcmuSample(5000);
  const auto negativeQuiet = petcam::encodePcmuSample(-100);
  const auto negativeLoud = petcam::encodePcmuSample(-5000);
  assert((positiveQuiet & 0x80) != 0);
  assert((negativeQuiet & 0x80) == 0);
  assert(positiveLoud < positiveQuiet);
  assert(negativeLoud < negativeQuiet);

  return 0;
}
