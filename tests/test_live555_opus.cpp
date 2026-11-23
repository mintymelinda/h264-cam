#include <opus/opus.h>

#include <array>
#include <cassert>
#include <cmath>
#include <cstdint>
#include <vector>

int main() {
  constexpr std::array<int, 5> kRates{8000, 12000, 16000, 24000, 48000};
  constexpr std::array<int, 2> kChannelCounts{1, 2};
  // Opus durations in tenths of a millisecond, including the 2.5 ms frame.
  constexpr std::array<int, 6> kPacketDurationsMs10{25, 50, 100, 200, 400, 600};
  constexpr int kRtpClockRate = 48000;

  for (const int rate : kRates) {
    for (const int channels : kChannelCounts) {
      for (const int durationMs10 : kPacketDurationsMs10) {
        assert(rate * durationMs10 % 10000 == 0);
        const int frameSamples = rate * durationMs10 / 10000;
        const int rtpSamples = kRtpClockRate * durationMs10 / 10000;
        int error = OPUS_OK;
        OpusEncoder* encoder = opus_encoder_create(
          rate, channels, OPUS_APPLICATION_AUDIO, &error
        );
        assert(encoder != nullptr);
        assert(error == OPUS_OK);
        assert(opus_encoder_ctl(encoder, OPUS_SET_BITRATE(128000)) == OPUS_OK);
        assert(opus_encoder_ctl(encoder, OPUS_SET_COMPLEXITY(5)) == OPUS_OK);
        assert(opus_encoder_ctl(encoder, OPUS_SET_FORCE_CHANNELS(channels)) == OPUS_OK);

        std::vector<opus_int16> input(
          static_cast<std::size_t>(frameSamples * channels)
        );
        for (int frame = 0; frame < frameSamples; ++frame) {
          const auto sample = static_cast<opus_int16>(
            std::sin(2.0 * 3.141592653589793 * 440.0 * frame / rate) * 12000.0
          );
          for (int channel = 0; channel < channels; ++channel) {
            input[static_cast<std::size_t>(frame * channels + channel)] = sample;
          }
        }

        std::array<unsigned char, 4000> packet{};
        const int packetSize = opus_encode(
          encoder, input.data(), frameSamples,
          packet.data(), static_cast<opus_int32>(packet.size())
        );
        assert(packetSize > 0);
        assert(opus_packet_get_nb_samples(packet.data(), packetSize, rate) == frameSamples);
        assert(opus_packet_get_nb_samples(packet.data(), packetSize, kRtpClockRate) == rtpSamples);
        assert(opus_packet_get_nb_channels(packet.data()) == channels);

        OpusDecoder* decoder = opus_decoder_create(
          kRtpClockRate, channels, &error
        );
        assert(decoder != nullptr);
        assert(error == OPUS_OK);
        std::vector<opus_int16> decoded(
          static_cast<std::size_t>(rtpSamples * channels)
        );
        const int decodedSamples = opus_decode(
          decoder, packet.data(), packetSize, decoded.data(), rtpSamples, 0
        );
        assert(decodedSamples == rtpSamples);

        opus_decoder_destroy(decoder);
        opus_encoder_destroy(encoder);
      }
    }
  }
  return 0;
}
