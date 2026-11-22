#include "nal_chunk.hpp"
#include "rtsp_transport.hpp"

#include <cassert>
#include <cstddef>
#include <vector>

int main() {
  {
    const std::string udpRequest =
      "SETUP rtsp://kittycam.local/petcam/track1 RTSP/1.0\r\n"
      "CSeq: 2\r\n"
      "Transport: RTP/AVP;unicast;client_port=5000-5001\r\n"
      "User-Agent: PetCam-Test\r\n\r\n";
    const std::string forced = petcam::forceRtpTcpTransport(udpRequest);
    assert(forced.find("Transport: RTP/AVP/TCP;unicast\r\n") != std::string::npos);
    assert(forced.find("client_port=") == std::string::npos);
    assert(forced.find("User-Agent: PetCam-Test\r\n") != std::string::npos);
  }

  {
    const std::string noTransport =
      "SETUP rtsp://kittycam.local/petcam/track1 RTSP/1.0\r\n"
      "CSeq: 3\r\n\r\n";
    const std::string forced = petcam::forceRtpTcpTransport(noTransport);
    assert(forced.find(
      "CSeq: 3\r\nTransport: RTP/AVP/TCP;unicast\r\n\r\n"
    ) != std::string::npos);
  }

  {
    const std::string mixedCase =
      "SETUP rtsp://kittycam.local/petcam/track1 RTSP/1.0\r\n"
      "cSeQ: 4\r\n"
      "tRaNsPoRt: RTP/AVP;unicast;client_port=1000-1001\r\n"
      "TRANSPORT: RTP/AVP;unicast;client_port=2000-2001\r\n\r\n";
    const std::string forced = petcam::forceRtpTcpTransport(mixedCase);
    const std::string selected = "Transport: RTP/AVP/TCP;unicast";
    const auto first = forced.find(selected);
    assert(first != std::string::npos);
    assert(forced.find(selected, first + selected.size()) == std::string::npos);
    assert(forced.find("client_port=") == std::string::npos);
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
