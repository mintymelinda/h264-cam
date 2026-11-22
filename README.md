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

The installer uses the Live555 packages provided by Raspberry Pi OS, compiles the native
RTSP/RTP server against the system libraries, then removes the compiler and development
headers. No GStreamer or Python package dependencies are required.

Run the guided terminal workflow:

```sh
petcam
```

To select a camera mode directly:

```sh
petcam -c "USB;1920x1080;30"
petcam -c "USB;1280x720;30"
```

The RTSP endpoint is `rtsp://DEVICE-HOSTNAME:8554/petcam`. USB H.264 is captured
directly with V4L2; CSI H.264 is captured with `rpicam-vid`. Both paths pass Annex-B
NAL units to Live555 without decoding or transcoding. New clients start at a cached
SPS/PPS + IDR boundary.

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
