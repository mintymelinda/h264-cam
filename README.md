# H264 Cam

H264 Cam publishes a camera's **native H.264 stream over RTSP using GStreamer**. It does
not decode or re-encode video, and it does not provide a web interface, motion processing,
timelapse, or Home Assistant integration.

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

The installer installs the GStreamer RTSP Server Python bindings and the GStreamer H.264
parser and RTP payloader from Raspberry Pi OS packages. It does not compile or install a
separate native RTSP server.

Run the guided terminal workflow:

```sh
petcam
```

**Leave this terminal window open while the stream is running.** The camera capture and
GStreamer RTSP server are owned by this foreground PetCam process. Press **q** to stop
the stream cleanly. Closing the terminal or disconnecting SSH ends the foreground process
and stops the stream. PetCam does not install a systemd service or run a detached server.

## Repair an installed image

The prebuilt image retains the exact release checkout at `/opt/petcam/source/`. To
reinstall the application from that checkout:

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

The RTSP endpoint is `rtsp://DEVICE-HOSTNAME.local:8554/petcam`. The installer includes
`avahi-daemon`, which publishes the Raspberry Pi hostname over mDNS. The displayed URL
uses the system hostname with `.local`; set `PETCAM_ADVERTISE_HOST` to override it when
using a different DNS name. The client must support mDNS/Bonjour name resolution.

## Streaming pipeline

USB H.264 is captured directly with V4L2. CSI H.264 is captured with `rpicam-vid`.
PetCam passes the resulting Annex-B byte stream into this GStreamer RTSP pipeline:

```
appsrc
  ! h264parse
  ! video/x-h264,alignment=au
  ! queue (bounded, drops old access units for slow clients)
  ! rtph264pay
  ! GStreamer RTSP Server
```

GStreamer handles H.264 stream parsing, access-unit framing, RTP packetization, RTSP
sessions, and client transport negotiation. The pipeline has no video decoder or encoder:
the camera's encoded H.264 data is passed through. The RTP payloader is configured to
repeat available SPS/PPS parameter sets with every IDR frame. The downstream queue is
bounded so a slow client does not make latency grow without limit.

The GStreamer listener starts with the terminal UI; native camera capture begins when an RTSP client requests the media pipeline. The terminal display reports listener state, connected clients, received camera-data rate,
and system resource usage. It does not claim that bytes read from the camera have been
acknowledged by a remote client.

For VLC, request RTSP over TCP with:

```sh
vlc --rtsp-tcp rtsp://DEVICE-HOSTNAME.local:8554/petcam
```

To verify TCP negotiation and receipt of an interleaved RTP packet without a video player:

```sh
python3 scripts/check_rtsp_tcp.py rtsp://DEVICE-HOSTNAME.local:8554/petcam
```

Run this probe from a machine that can reach the camera's RTSP endpoint. It verifies
RTSP/TCP negotiation and receipt of an interleaved RTP packet from the running stream.

## Optional camera settings

Environment variables can be set before launching `petcam`:

| Variable | Default | Purpose |
|---|---:|---|
| `PETCAM_V4L2_MMAP_BUFFERS` | `3` | USB V4L2 MMAP capture buffers (2–32) |
| `PETCAM_V4L2_STREAM_POLL` | off | Add V4L2 non-blocking poll-based capture |
| `PETCAM_V4L2_CONTROLS` | unset | Optional integer controls passed to `v4l2-ctl --set-ctrl`, e.g. `video_gop_size=30,video_bitrate=6000000` |
| `PETCAM_CSI_INTRA` | `30` | CSI H.264 I-frame interval in frames |
| `PETCAM_CSI_BITRATE` | `8000000` | CSI H.264 target bitrate in bits per second |

For example, to test the optional USB controls and a larger capture ring:

```sh
PETCAM_V4L2_MMAP_BUFFERS=8 \
PETCAM_V4L2_CONTROLS='video_gop_size=30,video_bitrate=6000000' \
petcam
```

V4L2 encoder controls are driver-specific and are **not** assumed to exist on every USB
camera. Check that the selected device exposes the named controls before setting them;
an unsupported control can cause capture startup to fail. The CSI GOP and bitrate settings
apply only to `rpicam-vid` cameras.

## Build an image locally

The image builder downloads and verifies the official Raspberry Pi OS Lite 32-bit image,
installs H264 Cam into its ARMHF chroot, and writes the customized image under
`dist/image/`.

```sh
sudo ./build-image.sh
```

## Create a release

Releases are built from version tags such as `v1.0.0`. The release script builds and
validates the final `.img.xz`, verifies its SHA-256 sidecar, and generates the
Raspberry Pi Imager JSON manifest using the matching official Raspberry Pi OS manifest
entry.

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
