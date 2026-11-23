#include "media_session_start.hpp"

#include <cassert>
#include <chrono>
#include <cstdint>
#include <vector>

int main() {
  using Clock = std::chrono::steady_clock;
  using namespace std::chrono_literals;

  petcam::MediaSessionStartRegistry sessions;
  const auto initialRecoveryTime = Clock::time_point{} + 8s;
  const auto selectedVideoRecoveryTime = Clock::time_point{} + 10s;
  const auto laterAudioSetupTime = Clock::time_point{} + 12s;

  // The first track to be set up establishes a provisional live-edge anchor.
  auto videoTrack = sessions.get(42, [&] { return initialRecoveryTime; });
  assert(!videoTrack->videoStartReady);

  // A delayed SETUP for the other track must reuse that same session state.
  auto audioTrack = sessions.get(42, [&] { return laterAudioSetupTime; });
  assert(videoTrack == audioTrack);
  assert(audioTrack->captureTime == initialRecoveryTime);

  // Video may have to advance to a newer IDR if the provisional one aged out.
  // Audio must wait for this decision, then follow the exact selected IDR time.
  videoTrack->captureTime = selectedVideoRecoveryTime;
  videoTrack->videoStartReady = true;
  assert(audioTrack->videoStartReady);
  assert(audioTrack->captureTime == selectedVideoRecoveryTime);

  // A separate RTSP client gets an independent start point.
  auto otherClient = sessions.get(43, [&] { return laterAudioSetupTime; });
  assert(otherClient != videoTrack);
  assert(otherClient->captureTime == laterAudioSetupTime);
  assert(!otherClient->videoStartReady);

  struct AudioFrame {
    std::uint64_t sequence;
    Clock::time_point capturedAt;
  };
  const std::vector<AudioFrame> audioFrames{
      {1, selectedVideoRecoveryTime - 40ms},
      {2, selectedVideoRecoveryTime - 20ms},
      {3, selectedVideoRecoveryTime},
      {4, selectedVideoRecoveryTime + 20ms},
  };
  const auto alignedAudio = petcam::firstAtOrAfter(
      audioFrames.begin(), audioFrames.end(), videoTrack->captureTime,
      [](const AudioFrame& frame) { return frame.capturedAt; });
  assert(alignedAudio != audioFrames.end());
  assert(alignedAudio->sequence == 3);
  assert(alignedAudio->capturedAt == selectedVideoRecoveryTime);
  return 0;
}
