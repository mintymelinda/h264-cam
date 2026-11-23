#include <opus/opus.h>

#include <array>
#include <cassert>
#include <cmath>
#include <cstdint>

int main() {
  constexpr int kRate = 48000;
  constexpr int kChannels = 2;
  constexpr int kFrameSamples = 960;

  int error = OPUS_OK;
  OpusEncoder* encoder = opus_encoder_create(kRate, kChannels, OPUS_APPLICATION_AUDIO, &error);
  assert(encoder != nullptr);
  assert(error == OPUS_OK);
  assert(opus_encoder_ctl(encoder, OPUS_SET_BITRATE(64000)) == OPUS_OK);
  assert(opus_encoder_ctl(encoder, OPUS_SET_COMPLEXITY(1)) == OPUS_OK);

  std::array<opus_int16, kFrameSamples * kChannels> input{};
  for (int frame = 0; frame < kFrameSamples; ++frame) {
    const auto sample = static_cast<opus_int16>(
      std::sin(2.0 * 3.141592653589793 * 440.0 * frame / kRate) * 12000.0
    );
    input[frame * kChannels] = sample;
    input[frame * kChannels + 1] = sample;
  }

  std::array<unsigned char, 4000> packet{};
  const int packetSize = opus_encode(
    encoder, input.data(), kFrameSamples, packet.data(), static_cast<opus_int32>(packet.size())
  );
  assert(packetSize > 0);
  assert(opus_packet_get_nb_samples(packet.data(), packetSize, kRate) == kFrameSamples);
  assert(opus_packet_get_nb_channels(packet.data()) == kChannels);

  OpusDecoder* decoder = opus_decoder_create(kRate, kChannels, &error);
  assert(decoder != nullptr);
  assert(error == OPUS_OK);
  std::array<opus_int16, kFrameSamples * kChannels> decoded{};
  const int decodedSamples = opus_decode(
    decoder, packet.data(), packetSize, decoded.data(), kFrameSamples, 0
  );
  assert(decodedSamples == kFrameSamples);

  opus_decoder_destroy(decoder);
  opus_encoder_destroy(encoder);
  return 0;
}
