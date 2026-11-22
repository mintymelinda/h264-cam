#!/usr/bin/env bash
set -Eeuo pipefail

log() { printf '[petcam-gstreamer] %s\n' "$*"; }
fail() { printf '[petcam-gstreamer] ERROR: %s\n' "$*" >&2; exit 1; }

[[ "$EUID" -eq 0 ]] || fail "Run this helper as root."
for tool in apt-get dpkg dpkg-deb; do
  command -v "$tool" >/dev/null 2>&1 || fail "Required package tool is missing: $tool"
done

architecture="$(dpkg --print-architecture)"
case "$architecture" in
  armhf) multiarch="arm-linux-gnueabihf" ;;
  arm64) multiarch="aarch64-linux-gnu" ;;
  amd64) multiarch="x86_64-linux-gnu" ;;
  i386) multiarch="i386-linux-gnu" ;;
  *) fail "Unsupported Debian architecture for the GStreamer H.264 parser: $architecture" ;;
esac

parser_available() {
  /usr/bin/python3 - <<'PY' >/dev/null 2>&1
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst
Gst.init(None)
raise SystemExit(0 if Gst.ElementFactory.find("h264parse") is not None else 1)
PY
}

if parser_available; then
  log "GStreamer h264parse is already available."
  exit 0
fi

work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT

log "Downloading only the Debian H.264 parser plugin and its codec-parser library."
(
  cd "$work_dir"
  apt-get download \
    "gstreamer1.0-plugins-bad:$architecture" \
    "libgstreamer-plugins-bad1.0-0:$architecture"
)

plugin_deb=""
codec_deb=""
for archive in "$work_dir"/*.deb; do
  [[ -f "$archive" ]] || continue
  case "$(dpkg-deb -f "$archive" Package")" in
    gstreamer1.0-plugins-bad) plugin_deb="$archive" ;;
    libgstreamer-plugins-bad1.0-0) codec_deb="$archive" ;;
  esac
done
[[ -n "$plugin_deb" && -n "$codec_deb" ]] || fail "The parser plugin packages were not downloaded."
plugin_version="$(dpkg-deb -f "$plugin_deb" Version)"
codec_version="$(dpkg-deb -f "$codec_deb" Version)"
[[ "$plugin_version" == "$codec_version" ]] || fail "The parser plugin and codec library package versions do not match."

plugin_root="$work_dir/plugin"
codec_root="$work_dir/codec"
dpkg-deb -x "$plugin_deb" "$plugin_root"
dpkg-deb -x "$codec_deb" "$codec_root"

plugin_source="$plugin_root/usr/lib/$multiarch/gstreamer-1.0/libgstvideoparsersbad.so"
[[ -s "$plugin_source" ]] || fail "The Debian H.264 video parser plugin is missing."
mapfile -t codec_libraries < <(
  find "$codec_root/usr/lib/$multiarch" -maxdepth 1 -type f \
    -name 'libgstcodecparsers-1.0.so.0.*' -print
)
(("${#codec_libraries[@]}" == 1)) || fail "The Debian codec-parser shared library is missing or ambiguous."
codec_source="${codec_libraries[0]}"
codec_filename="$(basename "$codec_source")"

package_root="$work_dir/package"
install -d -m 0755 \
  "$package_root/DEBIAN" \
  "$package_root/usr/lib/$multiarch/gstreamer-1.0" \
  "$package_root/usr/lib/$multiarch"
install -m 0644 "$plugin_source" \
  "$package_root/usr/lib/$multiarch/gstreamer-1.0/libgstvideoparsersbad.so"
install -m 0644 "$codec_source" "$package_root/usr/lib/$multiarch/$codec_filename"
ln -s "$codec_filename" \
  "$package_root/usr/lib/$multiarch/libgstcodecparsers-1.0.so.0"

cat > "$package_root/DEBIAN/control" <<EOF
Package: petcam-gstreamer-h264parse
Version: $plugin_version
Section: video
Priority: optional
Architecture: $architecture
Maintainer: H264 Cam Project
Depends: libc6, libglib2.0-0t64 | libglib2.0-0, libgstreamer1.0-0, libgstreamer-plugins-base1.0-0
Description: Minimal GStreamer H.264 parser runtime for H264 Cam
 Includes only the H.264 parser plugin and its codec-parser library.
EOF

local_package="$work_dir/petcam-gstreamer-h264parse.deb"
dpkg-deb --build --root-owner-group "$package_root" "$local_package" >/dev/null
log "Installing the minimal H.264 parser package ($plugin_version)."
dpkg -i "$local_package"
ldconfig

parser_available || fail "GStreamer did not load the installed h264parse element."
log "The GStreamer H.264 parser is available without installing the full plugins-bad bundle."
