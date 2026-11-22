#pragma once

#include <cctype>
#include <string>

namespace petcam {

inline bool isTransportHeader(const std::string& line) {
  static constexpr char name[] = "Transport:";
  if (line.size() < sizeof(name) - 1) return false;
  for (std::size_t i = 0; i < sizeof(name) - 1; ++i) {
    const auto actual = static_cast<unsigned char>(line[i]);
    const auto expected = static_cast<unsigned char>(name[i]);
    if (std::tolower(actual) != std::tolower(expected)) return false;
  }
  return true;
}

// Force a SETUP request to select RTP interleaved over its RTSP TCP connection.
// The request's other headers and any body are preserved.
inline std::string forceRtpTcpTransport(const std::string& request) {
  std::string rewritten;
  rewritten.reserve(request.size() + 32);
  bool inserted = false;
  std::size_t lineStart = 0;

  while (lineStart < request.size()) {
    const std::size_t lineEnd = request.find("\r\n", lineStart);
    if (lineEnd == std::string::npos) {
      rewritten.append(request, lineStart, std::string::npos);
      return rewritten;
    }

    const std::string line = request.substr(lineStart, lineEnd - lineStart);
    if (line.empty()) {
      if (!inserted) rewritten += "Transport: RTP/AVP/TCP;unicast\r\n";
      rewritten += "\r\n";
      rewritten.append(request, lineEnd + 2, std::string::npos);
      return rewritten;
    }

    if (isTransportHeader(line)) {
      if (!inserted) {
        rewritten += "Transport: RTP/AVP/TCP;unicast";
        inserted = true;
      }
      // Drop duplicate Transport headers so the selected mode is unambiguous.
    } else {
      rewritten += line;
    }
    rewritten += "\r\n";
    lineStart = lineEnd + 2;
  }

  return request;
}

}  // namespace petcam
