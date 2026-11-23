#include "media_session_start.hpp"

#include <cassert>
#include <chrono>
#include <memory>
#include <vector>

int main() {
  using Clock = std::chrono::steady_clock;
  using namespace std::chrono_literals;

  petcam::MediaSessionStartRegistry sessions;
  const auto videoRecoveryTime = Clock::time_point{} + 10s;
  const auto laterAudioSetupTime = Clock::time_point{} + 12s;

  // The first track to be set up establishes the common live edge.
  auto videoTrack = sessions.get(42, [&] { return videoRecoveryTime; });
  // A later SETUP for the other track must reuse that exact anchor.
  auto audioTrack = sessions.get(42, [&] { return laterAudioSetupTime; });
  assert(videoTrack == audioTrack);
  assert(audioTrack->captureTime == videoRecoveryTime);

  // A separate RTSP client gets an independent start point.
  auto otherClient = sessions.get(43, [&] { return laterAudioSetupTime; });
  assert(otherClient != videoTrack);
  assert(otherClient->captureTime == laterAudioSetupTime);

  struct AudioFrame {
    std::uint64_t sequence;
    Clock::time_point capturedAt;
  };
  const std::vector<AudioFrame> audioFrames{
      {1, videoRecoveryTime - 40ms},
      {2, videoRecoveryTime - 20ms},
      {3, videoRecoveryTime},
      {4, videoRecoveryTime + 20ms},
  };
  const auto alignedAudio = petcam::firstAtOrAfter(
      audioFrames.begin(), audioFrames.end(), videoTrack->captureTime,
      [](const AudioFrame& frame) { return frame.capturedAt; });
  assert(alignedAudio != audioFrames.end());
  assert(alignedAudio->sequence == 3);
  assert(alignedAudio->capturedAt == videoRecoveryTime);

  // Track setup order/delay cannot move one track to a newer, unrelated edge.
  assert(audioTrack->captureTime == alignedAudio->capturedAt);
  return 0;
}
