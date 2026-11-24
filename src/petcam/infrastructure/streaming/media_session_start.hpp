#pragma once

#include <algorithm>
#include <chrono>
#include <map>
#include <memory>
#include <mutex>
#include <utility>

namespace petcam {

struct MediaSessionStart {
  std::mutex mutex;
  std::chrono::steady_clock::time_point captureTime;
  bool videoStartReady = false;
};

// Both RTSP subsessions for one client session share the same live-edge anchor,
// even when Live555 creates their media sources at different times.
class MediaSessionStartRegistry {
public:
  template <typename Factory>
  std::shared_ptr<MediaSessionStart> get(unsigned clientSessionId, Factory&& create) {
    std::lock_guard<std::mutex> lock(mutex_);
    auto& weakStart = starts_[clientSessionId];
    if (auto existing = weakStart.lock()) return existing;

    auto start = std::make_shared<MediaSessionStart>();
    start->captureTime = std::forward<Factory>(create)();
    for (auto it = starts_.begin(); it != starts_.end();) {
      if (it->first != clientSessionId && it->second.expired())
        it = starts_.erase(it);
      else
        ++it;
    }
    weakStart = start;
    return start;
  }

private:
  std::mutex mutex_;
  std::map<unsigned, std::weak_ptr<MediaSessionStart>> starts_;
};

template <typename Iterator, typename TimePoint, typename Timestamp>
Iterator firstAtOrAfter(
    Iterator begin, Iterator end, const TimePoint& target, Timestamp timestamp) {
  return std::find_if(begin, end, [&](const auto& item) {
    return timestamp(item) >= target;
  });
}

}  // namespace petcam
