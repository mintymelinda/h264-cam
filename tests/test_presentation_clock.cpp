#include "presentation_clock.hpp"

#include <cassert>
#include <chrono>

int main() {
  using Clock = petcam::PresentationClock::SteadyClock;
  using SystemClock = petcam::PresentationClock::SystemClock;

  const auto steadyAnchor = Clock::time_point{} + std::chrono::seconds(10);
  const auto systemAnchor = SystemClock::time_point{} + std::chrono::seconds(100);
  const petcam::PresentationClock clock(steadyAnchor, systemAnchor);

  const auto audioCapturedAt = steadyAnchor + std::chrono::milliseconds(125);
  const auto videoCapturedAt = steadyAnchor + std::chrono::milliseconds(145);
  const auto audioTime = clock.toTimeval(audioCapturedAt);
  const auto videoTime = clock.toTimeval(videoCapturedAt);

  assert(audioTime.tv_sec == 100);
  assert(audioTime.tv_usec == 125000);
  assert(videoTime.tv_sec == 100);
  assert(videoTime.tv_usec == 145000);

  const auto audioMicros =
      static_cast<long long>(audioTime.tv_sec) * 1000000LL + audioTime.tv_usec;
  const auto videoMicros =
      static_cast<long long>(videoTime.tv_sec) * 1000000LL + videoTime.tv_usec;
  assert(videoMicros - audioMicros == 20000);

  return 0;
}
