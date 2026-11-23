#pragma once

#include <chrono>
#include <sys/time.h>

namespace petcam {

// Maps the process monotonic clock to wall time once, then timestamps both
// media tracks from their capture/enqueue times. This keeps RTCP sender reports
// on a shared timeline without letting Live555's per-track delivery scheduling
// become the media clock.
class PresentationClock {
public:
  using SteadyClock = std::chrono::steady_clock;
  using SystemClock = std::chrono::system_clock;

  PresentationClock(
      SteadyClock::time_point steadyAnchor = SteadyClock::now(),
      SystemClock::time_point systemAnchor = SystemClock::now())
      : offset_(systemAnchor.time_since_epoch() -
                std::chrono::duration_cast<SystemClock::duration>(
                    steadyAnchor.time_since_epoch())) {}

  timeval toTimeval(SteadyClock::time_point capturedAt) const {
    const auto systemTime =
        std::chrono::duration_cast<SystemClock::duration>(
            capturedAt.time_since_epoch()) + offset_;
    const auto micros =
        std::chrono::duration_cast<std::chrono::microseconds>(systemTime);
    const auto seconds = std::chrono::duration_cast<std::chrono::seconds>(micros);
    const auto remainder = micros - seconds;
    timeval result{};
    result.tv_sec = static_cast<decltype(result.tv_sec)>(seconds.count());
    result.tv_usec = static_cast<decltype(result.tv_usec)>(remainder.count());
    return result;
  }

private:
  SystemClock::duration offset_;
};

}  // namespace petcam
