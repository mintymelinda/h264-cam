#include "nal_chunk.hpp"

#include <cassert>
#include <chrono>
#include <cstddef>
#include <vector>

int main() {
  {
    using Clock = std::chrono::steady_clock;
    const auto queuedAt = Clock::time_point{} + std::chrono::seconds(10);
    const auto maxAge = std::chrono::seconds(3);
    assert(!petcam::isNalQueueEntryExpired(
      queuedAt + std::chrono::milliseconds(2999), queuedAt, maxAge
    ));
    assert(petcam::isNalQueueEntryExpired(
      queuedAt + std::chrono::seconds(3), queuedAt, maxAge
    ));
  }

  {
    std::vector<unsigned char> nal(256U * 1024U, 0x55);
    nal[0] = 0x65;
    std::vector<unsigned char> reconstructed;
    std::size_t offset = 0;
    unsigned callbacks = 0;
    bool complete = false;

    while (!complete) {
      unsigned char buffer[150000];
      const auto result = petcam::copyNalChunk(
        nal, offset, true, buffer, sizeof(buffer)
      );
      assert(result.size > 0);
      reconstructed.insert(reconstructed.end(), buffer, buffer + result.size);
      complete = result.complete;
      ++callbacks;
    }

    assert(callbacks == 2);
    assert(reconstructed.size() == nal.size() + 4);
    assert((reconstructed[0] == 0 && reconstructed[1] == 0 &&
            reconstructed[2] == 0 && reconstructed[3] == 1));
    assert(std::vector<unsigned char>(reconstructed.begin() + 4, reconstructed.end()) == nal);
  }

  {
    const std::vector<unsigned char> nal{0x65, 0x88, 0x99};
    std::vector<unsigned char> reconstructed;
    std::size_t offset = 0;
    bool complete = false;
    const std::size_t capacities[] = {2, 2, 1, 2};

    for (const auto capacity : capacities) {
      unsigned char buffer[2];
      const auto result = petcam::copyNalChunk(
        nal, offset, true, buffer, capacity
      );
      reconstructed.insert(reconstructed.end(), buffer, buffer + result.size);
      complete = result.complete;
      if (complete) break;
    }

    const std::vector<unsigned char> expected{0, 0, 0, 1, 0x65, 0x88, 0x99};
    assert(complete);
    assert(reconstructed == expected);
  }

  {
    const std::vector<unsigned char> nal{0x41, 0x9A, 0x01};
    unsigned char buffer[8]{};
    std::size_t offset = 0;
    const auto result = petcam::copyNalChunk(
      nal, offset, false, buffer, sizeof(buffer)
    );
    assert(result.complete);
    assert(result.size == nal.size());
    assert(std::vector<unsigned char>(buffer, buffer + result.size) == nal);
  }

  return 0;
}
