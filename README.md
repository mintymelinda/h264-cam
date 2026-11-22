# H264 Cam

H264 Cam serves a camera's native H.264 capture stream over RTSP using Live555. It does
not decode, transcode, or provide a web interface, motion processing, timelapse, or
Home Assistant integration.

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

The installer uses the Live555 packages provided by Raspberry Pi OS and compiles the native
RTSP/RTP server against the system libraries. The compiler and development packages remain
installed; the installer deliberately does not run package autoremove. No GStreamer or PyPI
dependencies are required.

Run the guided terminal workflow:

```sh
petcam
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

The RTSP endpoint is `rtsp://DEVICE-HOSTNAME.local:8554/petcam`. The installer includes
`avahi-daemon`, which publishes the Raspberry Pi hostname over mDNS. The displayed URL
uses the system hostname with `.local`; set `PETCAM_ADVERTISE_HOST` to override it when
using a different DNS name. The client must support mDNS/Bonjour name resolution.

### Stream diagnostics and experimental capture settings

The terminal status display separates NAL units seen from the camera, accepted into the
Live555 queue, and handed to Live555's RTSP media source. Each stage reports IDR (NAL type 5)
and non-IDR slice (NAL type 1) counts independently. RTSP-source counts are not a packet-level
network acknowledgement; they show what Live555 handed to its RTP sink.

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

The installer and CI run the same probe against a local Live555 instance using synthetic H.264
NAL units. The probe checks OPTIONS, DESCRIBE, a TCP SETUP response with interleaved channels,
PLAY, and receipt of RTP on interleaved channel 0. It does not assess visual decoding quality.

Optional environment variables can be set before launching `petcam`:

| Variable | Default | Purpose |
|---|---:|---|
| `PETCAM_V4L2_MMAP_BUFFERS` | `3` | USB V4L2 MMAP capture buffers (2–32) |
| `PETCAM_V4L2_STREAM_POLL` | off | Add V4L2 non-blocking poll-based capture |
| `PETCAM_V4L2_CONTROLS` | unset | Optional integer controls passed to `v4l2-ctl --set-ctrl`, e.g. `video_gop_size=30,video_bitrate=6000000` |
| `PETCAM_CSI_INTRA` | `30` | CSI H.264 I-frame interval in frames |
| `PETCAM_CSI_BITRATE` | `8000000` | CSI H.264 target bitrate in bits per second |
| `PETCAM_LIVE555_FRAMER` | `stream` | Select `stream` (Annex-B byte-stream parser) or experimental `discrete` NAL framing |

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

USB H.264 is captured
directly with V4L2; CSI H.264 is captured with `rpicam-vid`. Both paths pass Annex-B
NAL units to Live555 without decoding or transcoding. Only the IDR NAL (type 5) carries
the `is_idr` marker. When a client joins or the queue loses data, Live555 uses that IDR
marker to locate the associated SPS/PPS entries immediately before it and resumes from
that complete recovery sequence.

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
