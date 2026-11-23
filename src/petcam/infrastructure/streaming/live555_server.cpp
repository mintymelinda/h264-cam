#include <liveMedia.hh>
#include <H264VideoStreamDiscreteFramer.hh>
#include <SimpleRTPSink.hh>
#include <BasicUsageEnvironment.hh>
#include <GroupsockHelper.hh>
#include <algorithm>
#include <array>
#include <atomic>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <condition_variable>
#include <chrono>
#include <deque>
#include <mutex>
#include <string>
#include <thread>
#include <utility>
#include <vector>
#include "nal_chunk.hpp"
#include "pcmu.hpp"

namespace {
constexpr std::size_t kMaxNal = 16U * 1024U * 1024U;
constexpr std::size_t kMaxQueue = 16U * 1024U * 1024U;
constexpr auto kMaxQueueAge = std::chrono::seconds(3);
struct Nal {
  std::uint64_t seq;
  std::vector<unsigned char> data;
  bool isIdr;
  std::chrono::steady_clock::time_point enqueuedAt;
};
std::mutex queueMutex, lifecycleMutex;
std::deque<Nal> queue;
std::size_t queueBytes = 0;
std::mutex audioQueueMutex;
struct AudioFrame { std::uint64_t seq; std::vector<unsigned char> data; };
std::deque<AudioFrame> audioQueue;
std::uint64_t audioSequence = 1;
constexpr std::size_t kMaxAudioQueueFrames = 100;
std::uint64_t sequence = 1;
std::thread worker;
TaskScheduler* scheduler = nullptr;
EventTriggerId shutdownId = 0;
EventLoopWatchVariable watch = 0;
bool startupDone = false;
std::condition_variable startupCondition;
int startupCode = -1;
std::string startupMessage;
std::atomic<unsigned> connectedClients{0};
std::atomic<unsigned long long> deliveredNals{0};
std::array<std::atomic<unsigned long long>, 32> deliveredNalsByType{};

using NalIterator = std::deque<Nal>::iterator;

unsigned nalType(const Nal& nal) {
  return nal.data.empty() ? 0U : static_cast<unsigned>(nal.data[0] & 0x1F);
}

bool isCompleteRecoverySequence(NalIterator idr) {
  if (idr - queue.begin() < 2) return false;
  const auto pps = idr - 1;
  const auto sps = idr - 2;
  return idr->isIdr && nalType(*idr) == 5 &&
    nalType(*sps) == 7 && nalType(*pps) == 8;
}

NalIterator findLatestRecoveryStart() {
  auto latest = queue.end();
  for (auto idr = queue.begin(); idr != queue.end(); ++idr)
    if (isCompleteRecoverySequence(idr)) latest = idr - 2;
  return latest;
}

NalIterator findRecoveryStartAtOrAfter(std::uint64_t minSequence) {
  for (auto idr = queue.begin(); idr != queue.end(); ++idr) {
    if (!isCompleteRecoverySequence(idr)) continue;
    const auto sps = idr - 2;
    if (sps->seq >= minSequence) return sps;
  }
  return queue.end();
}

bool useDiscreteFramer() {
  const char* value = std::getenv("PETCAM_LIVE555_FRAMER");
  return value != nullptr && std::strcmp(value, "discrete") == 0;
}

class NalSource final : public FramedSource {
public:
  static NalSource* createNew(UsageEnvironment& env, bool includeStartCode) {
    return new NalSource(env, includeStartCode);
  }
protected:
  explicit NalSource(UsageEnvironment& env, bool includeStartCode)
    : FramedSource(env), includeStartCode_(includeStartCode) {}
  void doGetNextFrame() override {
    // Never call afterGetting() re-entrantly from RTSP PLAY/SETUP handling.
    // The RTSP response must be written before interleaved RTP/RTCP media.
    nextTask() = envir().taskScheduler().scheduleDelayedTask(0, deliverTask, this);
  }
  void doStopGettingFrames() override {
    if (nextTask() != nullptr) {
      envir().taskScheduler().unscheduleDelayedTask(nextTask());
      nextTask() = nullptr;
    }
  }
private:
  static void deliverTask(void* p) {
    auto* source = static_cast<NalSource*>(p);
    source->nextTask() = nullptr;
    source->deliver();
  }
  static void retry(void* p) {
    auto* source = static_cast<NalSource*>(p);
    source->nextTask() = nullptr;
    source->deliver();
  }
  void deliver() {
    if (!isCurrentlyAwaitingData()) return;

    if (currentNal_.empty()) loadNextNal();
    if (currentNal_.empty()) {
      nextTask() = envir().taskScheduler().scheduleDelayedTask(5000, retry, this);
      return;
    }

    // The discrete framer requires one whole NAL per callback. The byte-stream
    // framer can accept partial reads, so preserve the NAL and continue it on
    // subsequent callbacks when its parser asks for a smaller buffer.
    if (!includeStartCode_ && currentNal_.size() > fMaxSize) {
      fNumTruncatedBytes = static_cast<unsigned>(currentNal_.size() - fMaxSize);
      envir() << "PetCam: discrete H.264 NAL exceeds the Live555 source buffer\\n";
      handleClosure();
      return;
    }

    const auto chunk = petcam::copyNalChunk(
      currentNal_, currentNalOffset_, includeStartCode_, fTo, fMaxSize
    );
    if (chunk.size == 0) {
      nextTask() = envir().taskScheduler().scheduleDelayedTask(5000, retry, this);
      return;
    }

    fFrameSize = static_cast<unsigned>(chunk.size);
    fNumTruncatedBytes = 0;
    if (chunk.complete) {
      const unsigned type = currentNal_[0] & 0x1F;
      deliveredNalsByType[type].fetch_add(1, std::memory_order_relaxed);
      deliveredNals.fetch_add(1, std::memory_order_relaxed);
      currentNal_.clear();
      currentNalOffset_ = 0;
    }
    gettimeofday(&fPresentationTime, nullptr);
    fDurationInMicroseconds = 0;
    FramedSource::afterGetting(this);
  }

  void loadNextNal() {
    std::lock_guard<std::mutex> lock(queueMutex);
    if (queue.empty()) return;

    if (!cursorSet_) {
      auto recovery = findLatestRecoveryStart();
      if (recovery != queue.end()) {
        cursor_ = recovery->seq;
        cursorSet_ = true;
      }
    }
    if (!cursorSet_) return;

    auto it = std::find_if(queue.begin(), queue.end(),
      [this](const Nal& n) { return n.seq >= cursor_; });
    if (it != queue.end() && it->seq != cursor_) {
      const auto minimumSequence = std::max(cursor_, queue.front().seq);
      auto recovery = findRecoveryStartAtOrAfter(minimumSequence);
      if (recovery != queue.end()) {
        cursor_ = recovery->seq;
        it = recovery;
      } else {
        cursorSet_ = false;
        return;
      }
    }
    if (it == queue.end()) return;

    currentNal_ = it->data;
    currentNalOffset_ = 0;
    cursor_ = it->seq + 1;
  }
  std::uint64_t cursor_ = 0;
  bool cursorSet_ = false;
  bool includeStartCode_;
  std::vector<unsigned char> currentNal_;
  std::size_t currentNalOffset_ = 0;
};


class AudioSource final : public FramedSource {
public:
  static AudioSource* createNew(UsageEnvironment& env) { return new AudioSource(env); }
protected:
  explicit AudioSource(UsageEnvironment& env) : FramedSource(env) {}
  void doGetNextFrame() override {
    nextTask() = envir().taskScheduler().scheduleDelayedTask(0, deliverTask, this);
  }
  void doStopGettingFrames() override {
    if (nextTask() != nullptr) {
      envir().taskScheduler().unscheduleDelayedTask(nextTask());
      nextTask() = nullptr;
    }
  }
private:
  static void deliverTask(void* p) {
    auto* source = static_cast<AudioSource*>(p);
    source->nextTask() = nullptr;
    source->deliver();
  }
  static void retry(void* p) {
    auto* source = static_cast<AudioSource*>(p);
    source->nextTask() = nullptr;
    source->deliver();
  }
  void deliver() {
    if (!isCurrentlyAwaitingData()) return;
    std::vector<unsigned char> frame;
    {
      std::lock_guard<std::mutex> lock(audioQueueMutex);
      if (!audioQueue.empty()) {
        if (!cursorSet_) {
          cursor_ = audioQueue.back().seq;
          cursorSet_ = true;
        }
        auto it = std::find_if(audioQueue.begin(), audioQueue.end(),
          [this](const AudioFrame& item) { return item.seq >= cursor_; });
        if (it == audioQueue.end()) {
          nextTask() = envir().taskScheduler().scheduleDelayedTask(5000, retry, this);
          return;
        }
        if (it->seq != cursor_) cursor_ = it->seq;
        frame = it->data;
        cursor_ = it->seq + 1;
      }
    }
    if (frame.empty()) {
      nextTask() = envir().taskScheduler().scheduleDelayedTask(5000, retry, this);
      return;
    }
    const auto size = std::min(frame.size(), static_cast<std::size_t>(fMaxSize));
    std::memcpy(fTo, frame.data(), size);
    fFrameSize = static_cast<unsigned>(size);
    fNumTruncatedBytes = static_cast<unsigned>(frame.size() - size);
    gettimeofday(&fPresentationTime, nullptr);
    fDurationInMicroseconds = 20000;
    FramedSource::afterGetting(this);
  }
  std::uint64_t cursor_ = 0;
  bool cursorSet_ = false;
};
class AudioSubsession final : public OnDemandServerMediaSubsession {
public:
  static AudioSubsession* createNew(UsageEnvironment& env) { return new AudioSubsession(env); }
protected:
  explicit AudioSubsession(UsageEnvironment& env) : OnDemandServerMediaSubsession(env, False) {}
  FramedSource* createNewStreamSource(unsigned, unsigned& bitrate) override {
    bitrate = 64;
    return AudioSource::createNew(envir());
  }
  RTPSink* createNewRTPSink(Groupsock* groupsock, unsigned char payload, FramedSource*) override {
    return SimpleRTPSink::createNew(envir(), groupsock, payload, 8000,
      "audio", "PCMU", 1, True, False);
  }
private:
  ~AudioSubsession() override = default;
};

class H264Subsession final : public OnDemandServerMediaSubsession {
public:
  static H264Subsession* createNew(UsageEnvironment& env) { return new H264Subsession(env); }
protected:
  explicit H264Subsession(UsageEnvironment& env) : OnDemandServerMediaSubsession(env, False) {}
  FramedSource* createNewStreamSource(unsigned, unsigned& bitrate) override {
    bitrate = 8000;
    connectedClients.fetch_add(1, std::memory_order_relaxed);
    const bool discrete = useDiscreteFramer();
    FramedSource* source = NalSource::createNew(envir(), !discrete);
    if (discrete) {
      // Keep to the three-argument API available in the Raspberry Pi OS
      // Trixie Live555 package; the fourth delimiter option is newer upstream.
      return H264VideoStreamDiscreteFramer::createNew(envir(), source, False);
    }
    return H264VideoStreamFramer::createNew(envir(), source);
  }
  void closeStreamSource(FramedSource* inputSource) override {
    unsigned current = connectedClients.load(std::memory_order_relaxed);
    while (current > 0 && !connectedClients.compare_exchange_weak(
      current, current - 1, std::memory_order_relaxed)) {}
    OnDemandServerMediaSubsession::closeStreamSource(inputSource);
  }
  RTPSink* createNewRTPSink(Groupsock* groupsock, unsigned char payload, FramedSource*) override {
    return H264VideoRTPSink::createNew(envir(), groupsock, payload);
  }
private:
  ~H264Subsession() override = default;
};
void stopEvent(void*) { watch = 1; }

void serve(unsigned short port, std::string path) {
  OutPacketBuffer::maxSize = static_cast<unsigned>(kMaxNal + 4);
  TaskScheduler* localScheduler = BasicTaskScheduler::createNew();
  UsageEnvironment* env = BasicUsageEnvironment::createNew(*localScheduler);
  RTSPServer* server = env ? RTSPServer::createNew(*env, Port(port), nullptr) : nullptr;
  ServerMediaSession* session = nullptr;
  if (server) {
    while (!path.empty() && path.front() == '/') path.erase(path.begin());
    if (path.empty()) path = "petcam";
    session = ServerMediaSession::createNew(*env, path.c_str(), "PetCam",
      "Native H.264 camera stream", True);
    if (session) {
      session->addSubsession(H264Subsession::createNew(*env));
      session->addSubsession(AudioSubsession::createNew(*env));
      server->addServerMediaSession(session);
    }
  }
  {
    std::lock_guard<std::mutex> lock(lifecycleMutex);
    scheduler = localScheduler;
    shutdownId = localScheduler->createEventTrigger(stopEvent);
    startupCode = (server && session) ? 0 : -1;
    startupMessage = startupCode == 0 ? "" : (env ? env->getResultMsg() : "Live555 environment creation failed");
    startupDone = true;
  }
  startupCondition.notify_all();
  if (startupCode == 0) {
    watch = 0;
    localScheduler->doEventLoop(&watch);
  }
  if (server) {
    if (session) server->deleteServerMediaSession(session);
    Medium::close(server);
  }
  if (env) env->reclaim();
  delete localScheduler;
  {
    std::lock_guard<std::mutex> lock(lifecycleMutex);
    scheduler = nullptr;
    shutdownId = 0;
  }
}
}

extern "C" {
int petcam_start(unsigned short port, const char* path) {
  std::unique_lock<std::mutex> lock(lifecycleMutex);
  if (worker.joinable()) return -1;
  connectedClients.store(0, std::memory_order_relaxed);
  { std::lock_guard<std::mutex> audioLock(audioQueueMutex); audioQueue.clear(); audioSequence = 1; }
  deliveredNals.store(0, std::memory_order_relaxed);
  for (auto& count : deliveredNalsByType) count.store(0, std::memory_order_relaxed);
  startupDone = false;
  startupCode = -1;
  startupMessage.clear();
  worker = std::thread(serve, port, path ? path : "/petcam");
  startupCondition.wait_for(lock, std::chrono::seconds(5), [] { return startupDone; });
  if (!startupDone) {
    startupMessage = "Timed out waiting for Live555 startup";
    return -1;
  }
  return startupCode;
}

int petcam_push_audio_pcm(const unsigned char* data, std::size_t size) {
  if (!data || size != 320) return -1; // 160 mono S16_LE samples at 8 kHz (20 ms).
  std::vector<unsigned char> encoded;
  encoded.reserve(160);
  for (std::size_t i = 0; i < size; i += 2) {
    const auto sample = static_cast<std::int16_t>(
      static_cast<unsigned>(data[i]) | (static_cast<unsigned>(data[i + 1]) << 8));
    encoded.push_back(petcam::encodePcmuSample(sample));
  }
  std::lock_guard<std::mutex> lock(audioQueueMutex);
  audioQueue.push_back(AudioFrame{audioSequence++, std::move(encoded)});
  while (audioQueue.size() > kMaxAudioQueueFrames) audioQueue.pop_front();
  return 0;
}
void petcam_clear_audio() {
  std::lock_guard<std::mutex> lock(audioQueueMutex);
  audioQueue.clear();
}

int petcam_push_nal(const unsigned char* data, std::size_t size, int isIdr) {
  if (!data || size == 0 || size > kMaxNal) return -1;
  const bool nalIsIdr = (data[0] & 0x1F) == 5;
  if (nalIsIdr != (isIdr != 0)) return -1;
  const auto now = std::chrono::steady_clock::now();
  Nal item{0, std::vector<unsigned char>(data, data + size), isIdr != 0, now};
  std::lock_guard<std::mutex> lock(queueMutex);
  item.seq = sequence++;
  queueBytes += item.data.size();
  queue.push_back(std::move(item));
  // Retain only a short live window. If a consumer falls behind, its next
  // read will detect the sequence gap and resume at the next SPS/PPS + IDR.
  // This prevents a slow RTSP client from watching an ever-older recording.
  while (!queue.empty() &&
         (queueBytes > kMaxQueue || queue.size() > 8192 ||
          petcam::isNalQueueEntryExpired(now, queue.front().enqueuedAt, kMaxQueueAge))) {
    queueBytes -= queue.front().data.size();
    queue.pop_front();
  }
  return 0;
}
const char* petcam_last_error() { return startupMessage.c_str(); }
unsigned petcam_client_count() { return connectedClients.load(std::memory_order_relaxed); }
unsigned long long petcam_nal_count() { return deliveredNals.load(std::memory_order_relaxed); }
unsigned long long petcam_nal_type_count(unsigned int nalType) {
  if (nalType >= deliveredNalsByType.size()) return 0;
  return deliveredNalsByType[nalType].load(std::memory_order_relaxed);
}
void petcam_stop() {
  {
    std::lock_guard<std::mutex> lock(lifecycleMutex);
    if (scheduler && shutdownId) scheduler->triggerEvent(shutdownId, nullptr);
  }
  if (worker.joinable()) worker.join();
  std::lock_guard<std::mutex> lock(queueMutex);
  queue.clear();
  queueBytes = 0;
  { std::lock_guard<std::mutex> audioLock(audioQueueMutex); audioQueue.clear(); }
  sequence = 1;
}
}
