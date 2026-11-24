# H264 Cam

H264 Cam serves a camera's native H.264 capture stream over RTSP using Live555. The same
RTSP session includes a selectable Opus or uncompressed L16 PCM microphone audio track captured through ALSA at
48 kHz; automatic device selection prefers the Razer Kiyo microphone. PetCam does not
decode or transcode the video, or provide a web interface, motion processing, timelapse,
or Home Assistant
integration.

## Supported platform

- Raspberry Pi OS Trixie Lite, 32-bit ARMHF userspace
- ARMv6 or newer Raspberry Pi processors
- USB cameras that expose native H.264 through V4L2
- Raspberry Pi CSI cameras supported by `rpicam-vid`

## Install from source

On a supported Raspberry Pi OS installation:

```sh
sudo ./install.sh
```

The installer reads the Debian package list from `requirements.apt`. It installs the
Python standard library runtime, camera and audio utilities, Live555 development libraries, and the native compiler needed to build PetCam's Live555 server. There are no
third-party Python packages to install: the application uses the Python standard library
and its RTSP/RTP implementation is the native Live555 library. The compiler and
development packages remain installed; the installer deliberately does not run package
autoremove.

Run the guided terminal workflow:

```sh
petcam
```

On the first successful setup, PetCam saves the selected camera/audio configuration and
enables `petcam.service`. The service starts the Live555 RTSP listener in the background,
independently of the terminal interface. It is enabled for subsequent Pi boots. Pressing
`q` or closing the SSH terminal exits only the status screen; it does not stop the stream.

The service keeps the RTSP listener and existing RTSP sessions alive while it retries
camera capture after device disconnects or capture-process failures. Recovery waits for a
fresh SPS/PPS plus IDR sequence before video resumes. Microphone capture has its own retry
loop, so an audio-device outage does not stop video or the RTSP listener. Re-running `petcam` opens the saved stream status without restarting it. Use
`petcam --configure` to select a new mode; saving changed settings restarts the service
to apply the selected audio-track and camera settings.

Useful service commands:

```sh
sudo systemctl status petcam.service
sudo journalctl -u petcam.service -f
sudo systemctl restart petcam.service
```

## Repair an installed image

The prebuilt image retains the exact release checkout at `/opt/petcam/source/`. To
reinstall the application and rebuild its native Live555 library from that checkout:

```sh
sudo /opt/petcam/source/install.sh
```

To install a different source revision, clone it into a separate directory and run its
top-level installer:

```sh
sudo git clone https://github.com/mintymelinda/h264-cam.git /opt/petcam/source-new
sudo /opt/petcam/source-new/install.sh
```

To select a camera mode directly:

```sh
petcam -c "USB;1920x1080;30"
petcam -c "USB;1280x720;30"
```

The persistent RTSP endpoint is `rtsp://DEVICE-HOSTNAME.local:8554/petcam`. The installer includes
`avahi-daemon`, which publishes the Raspberry Pi hostname over mDNS. The displayed URL
uses the system hostname with `.local`; set `PETCAM_ADVERTISE_HOST` to override it when
using a different DNS name. The client must support mDNS/Bonjour name resolution.

### Stream diagnostics and experimental capture settings

The terminal status display separates NAL units seen from the camera, accepted into the
Live555 queue, and handed to Live555's RTSP media source. Each stage reports IDR (NAL type 5)
and non-IDR slice (NAL type 1) counts independently. RTSP-source counts are not a packet-level
network acknowledgement; they show what Live555 handed to its RTP sink.

For multi-slice H.264 pictures, PetCam reads `first_mb_in_slice` to recognize the first
slice of an IDR picture. It places the cached SPS/PPS before that first slice only; it does not
insert parameter sets between continuation slices of the same picture. This preserves the
picture's access-unit structure while retaining an SPS/PPS + IDR recovery point for new clients.

PetCam accepts both UDP RTP and RTP interleaved over the RTSP TCP connection. The RTSP client
selects the transport in its SETUP request; PetCam preserves that request and returns matching
transport parameters. For VLC, request TCP with:

```sh
vlc --rtsp-tcp rtsp://DEVICE-HOSTNAME.local:8554/petcam
```

To verify TCP negotiation and receipt of an interleaved RTP packet without a video player:

```sh
python3 scripts/check_rtsp_tcp.py rtsp://DEVICE-HOSTNAME.local:8554/petcam
```

CI runs a local Live555 microphone-track self-test using synthetic PCM frames. It verifies
the RTSP audio SDP, TCP SETUP and PLAY negotiation, and receipt of a live Opus RTP packet.
This test deliberately does not open a camera or microphone device, so it runs in a hardware-free
CI environment. To probe an actual camera stream, pass its RTSP URL to
`scripts/check_rtsp_tcp.py`; this checks RTSP/TCP negotiation and receipt of video RTP data.

Optional environment variables can be set before launching `petcam`:

| Variable | Default | Purpose |
|---|---:|---|
| `PETCAM_V4L2_MMAP_BUFFERS` | `3` | USB V4L2 MMAP capture buffers (2–32) |
| `PETCAM_V4L2_STREAM_POLL` | off | Add V4L2 non-blocking poll-based capture |
| `PETCAM_V4L2_CONTROLS` | unset | Optional integer controls passed to `v4l2-ctl --set-ctrl`, e.g. `video_gop_size=30,video_bitrate=6000000` |
| `PETCAM_CSI_INTRA` | `30` | CSI H.264 I-frame interval in frames |
| `PETCAM_CSI_BITRATE` | `8000000` | CSI H.264 target bitrate in bits per second |
| `PETCAM_AUDIO` | `1` | Enable the ALSA microphone audio track; set to `0` to disable it |
| `PETCAM_AUDIO_DEVICE` | `auto` | ALSA capture device; automatic selection prefers Razer Kiyo, otherwise the first capture device |
| `PETCAM_LIVE555_FRAMER` | `stream` | Select `stream` (Annex-B byte-stream parser) or experimental `discrete` NAL framing |

To inspect ALSA capture devices manually, run `arecord -l`. Automatic selection chooses
the Razer Kiyo when it is listed, then falls back to the first available capture device.
To force a specific ALSA device, set `PETCAM_AUDIO_DEVICE=plughw:1,0` (replace the card
and device numbers with those shown by `arecord -l`). At startup, PetCam sets the selected
card's ALSA `Mic` capture volume to 100% and enables capture. This requires the
`amixer` utility and a numeric `hw:CARD,DEVICE` or `plughw:CARD,DEVICE` device name.
Capture requests native-format stereo S16_LE at 48 kHz in 5 ms frames. The Razer Kiyo
advertises this exact format, so its capture path does not need ALSA rate or channel conversion.
Opus is the default compressed codec (128 kbit/s, 20 ms packets, 48 kHz RTP clock). L16 is
also available for uncompressed PCM; it converts little-endian ALSA samples to network byte
order and uses 5 ms packets so 48 kHz stereo payloads remain below a typical Ethernet MTU.
Live555 assigns the dynamic RTP payload type for either codec. Set `PETCAM_AUDIO=0`
only when video-only output is required. Use
`scripts/check_rtsp_tcp.py --require-audio rtsp://DEVICE-HOSTNAME.local:8554/petcam`
to verify both video and microphone RTP delivery from the running device.

For example, to test the optional USB controls and a larger capture ring:

```sh
PETCAM_V4L2_MMAP_BUFFERS=8 \
PETCAM_V4L2_CONTROLS='video_gop_size=30,video_bitrate=6000000' \
petcam
```

V4L2 encoder controls are driver-specific and are **not** assumed to exist on every USB
camera. Check that the selected device exposes the named controls before setting them;
an unsupported control can cause capture startup to fail. The CSI GOP and bitrate settings
apply only to `rpicam-vid` cameras. The discrete Live555 framer assumes each source item is a
complete NAL unit; its access-unit marker is approximate for streams that split one picture
across multiple slices, so it is an experiment rather than the default.

USB H.264 is captured directly with V4L2; CSI H.264 is captured with `rpicam-vid`. Both
paths pass Annex-B NAL units to Live555 without decoding or transcoding. Only the IDR NAL
(type 5) carries the `is_idr` marker. When a client joins or the queue loses data, Live555
uses that IDR marker to locate the associated SPS/PPS entries immediately before it and
resumes from that complete recovery sequence.

## Build an image locally

The image builder downloads and verifies the official Raspberry Pi OS Lite 32-bit image,
installs H264 Cam into its ARMHF chroot, and writes the customized image under
`dist/image/`.

```sh
sudo ./build-image.sh
```

## Create a release

Releases are built from version tags such as `v1.0.0`. The release script builds and
validates the final `.img.xz`, verifies its SHA-256 sidecar, and generates the Raspberry
Pi Imager JSON manifest using the matching official Raspberry Pi OS manifest entry.

```sh
sudo ./release.sh v1.0.0
```

The script writes three files under `dist/release/`:

- `h264-cam-v1.0.0-raspios-lite-armhf.img.xz` — final customized OS image
- `h264-cam-v1.0.0-raspios-lite-armhf.img.xz.sha256` — SHA-256 checksum
- `h264-cam-imager-manifest.json` — Raspberry Pi Imager OS-list manifest

Pushing a `v*` tag runs the same release process in GitHub Actions and publishes all
three files to the GitHub Release. The manifest's image URL points to that exact
versioned release asset.

Latest Raspberry Pi Imager manifest (available after the first release):

`https://github.com/mintymelinda/h264-cam/releases/latest/download/h264-cam-imager-manifest.json`

The manifest includes the image's compressed and extracted SHA-256 hashes and sizes.
Configure hostname, username, password and Wi-Fi in Raspberry Pi Imager before writing
the card; the image does not implement its own first-boot account setup.
