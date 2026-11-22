#!/usr/bin/env bash
set -Eeuo pipefail

log() { printf '[petcam-gstreamer] %s\n' "$*"; }
fail() { printf '[petcam-gstreamer] ERROR: %s\n' "$*" >&2; exit 1; }

[[ "$EUID" -eq 0 ]] || fail "Run this helper as root."
for tool in apt-get dpkg dpkg-deb dpkg-query ldd ldconfig; do
  command -v "$tool" >/dev/null 2>&1 || fail "Required package tool is missing: $tool"
done

architecture="$(dpkg --print-architecture)"
case "$architecture" in
  armhf) multiarch="arm-linux-gnueabihf" ;;
  arm64) multiarch="aarch64-linux-gnu" ;;
  amd64) multiarch="x86_64-linux-gnu" ;;
  i386) multiarch="i386-linux-gnu" ;;
  *) fail "Unsupported Debian architecture for the GStreamer runtime: $architecture" ;;
esac

runtime_available() {
  /usr/bin/python3 - <<'PY' >/dev/null 2>&1
import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstRtspServer", "1.0")
from gi.repository import Gst, GstRtspServer
Gst.init(None)
required = ("appsrc", "h264parse", "queue", "rtph264pay")
raise SystemExit(0 if GstRtspServer.RTSPServer is not None and all(
    Gst.ElementFactory.find(name) is not None for name in required
) else 1)
PY
}

installed_rtsp_status="$(dpkg-query -W -f='${Status}' petcam-gstreamer-rtsp-server 2>/dev/null || true)"
installed_parser_status="$(dpkg-query -W -f='${Status}' petcam-gstreamer-h264parse 2>/dev/null || true)"
installed_rtsp_version="$(dpkg-query -W -f='${Version}' petcam-gstreamer-rtsp-server 2>/dev/null || true)"
installed_parser_version="$(dpkg-query -W -f='${Version}' petcam-gstreamer-h264parse 2>/dev/null || true)"

work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT

log "Downloading official Debian GStreamer runtime packages without installing their broad metapackage dependencies."
(
  cd "$work_dir"
  apt-get download \
    "libgstrtspserver-1.0-0:$architecture" \
    "gir1.2-gst-rtsp-server-1.0:$architecture" \
    "gstreamer1.0-plugins-bad:$architecture" \
    "libgstreamer-plugins-bad1.0-0:$architecture"
)

find_deb() {
  local wanted="$1"
  local archive
  for archive in "$work_dir"/*.deb; do
    [[ -f "$archive" ]] || continue
    if [[ "$(dpkg-deb -f "$archive" Package")" == "$wanted" ]]; then
      printf '%s\n' "$archive"
      return 0
    fi
  done
  return 1
}

rtsp_deb="$(find_deb libgstrtspserver-1.0-0)" || fail "The RTSP server shared-library package was not downloaded."
typelib_deb="$(find_deb gir1.2-gst-rtsp-server-1.0)" || fail "The RTSP server typelib package was not downloaded."
plugin_deb="$(find_deb gstreamer1.0-plugins-bad)" || fail "The H.264 parser plugin package was not downloaded."
codec_deb="$(find_deb libgstreamer-plugins-bad1.0-0)" || fail "The codec-parser library package was not downloaded."

rtsp_version="$(dpkg-deb -f "$rtsp_deb" Version)"
typelib_version="$(dpkg-deb -f "$typelib_deb" Version)"
plugin_version="$(dpkg-deb -f "$plugin_deb" Version)"
codec_version="$(dpkg-deb -f "$codec_deb" Version)"
[[ "$rtsp_version" == "$typelib_version" ]] || fail "The RTSP server library and typelib versions do not match."
[[ "$plugin_version" == "$codec_version" ]] || fail "The H.264 parser plugin and codec-parser library versions do not match."

if [[ "$installed_rtsp_status" == "install ok installed" \
  && "$installed_parser_status" == "install ok installed" \
  && "$installed_rtsp_version" == "$rtsp_version" \
  && "$installed_parser_version" == "$plugin_version" ]] && runtime_available; then
  log "The minimal GStreamer RTSP server and H.264 parser runtime are already current."
  exit 0
fi

rtsp_root="$work_dir/rtsp"
typelib_root="$work_dir/typelib"
plugin_root="$work_dir/plugin"
codec_root="$work_dir/codec"
dpkg-deb -x "$rtsp_deb" "$rtsp_root"
dpkg-deb -x "$typelib_deb" "$typelib_root"
dpkg-deb -x "$plugin_deb" "$plugin_root"
dpkg-deb -x "$codec_deb" "$codec_root"

rtsp_library="$(find "$rtsp_root/usr/lib/$multiarch" -maxdepth 1 -type f \
  -name 'libgstrtspserver-1.0.so.0.*' -print -quit)"
rtsp_typelib="$typelib_root/usr/lib/$multiarch/girepository-1.0/GstRtspServer-1.0.typelib"
parser_plugin="$plugin_root/usr/lib/$multiarch/gstreamer-1.0/libgstvideoparsersbad.so"
codec_library="$(find "$codec_root/usr/lib/$multiarch" -maxdepth 1 -type f \
  -name 'libgstcodecparsers-1.0.so.0.*' -print -quit)"

[[ -s "$rtsp_library" ]] || fail "The RTSP server shared library is missing."
[[ -s "$rtsp_typelib" ]] || fail "The RTSP server GObject typelib is missing."
[[ -s "$parser_plugin" ]] || fail "The H.264 video parser plugin is missing."
[[ -s "$codec_library" ]] || fail "The codec-parser shared library is missing."

# Debian's RTSP server package declares a dependency on the complete plugins-bad
# bundle even though this application only needs the RTSP server library itself.
# The curated package below records its actual shared-library runtime dependencies.
package_root="$work_dir/package"
install -d -m 0755 \
  "$package_root/DEBIAN" \
  "$package_root/usr/lib/$multiarch/girepository-1.0" \
  "$package_root/usr/lib/$multiarch"
install -m 0644 "$rtsp_library" "$package_root/usr/lib/$multiarch/$(basename "$rtsp_library")"
ln -s "$(basename "$rtsp_library")" \
  "$package_root/usr/lib/$multiarch/libgstrtspserver-1.0.so.0"
install -m 0644 "$rtsp_typelib" \
  "$package_root/usr/lib/$multiarch/girepository-1.0/GstRtspServer-1.0.typelib"
cat > "$package_root/DEBIAN/control" <<EOF
Package: petcam-gstreamer-rtsp-server
Version: $rtsp_version
Section: video
Priority: optional
Architecture: $architecture
Maintainer: H264 Cam Project
Depends: libc6, libglib2.0-0t64 | libglib2.0-0, libgstreamer1.0-0, libgstreamer-plugins-base1.0-0
Description: Minimal GStreamer RTSP server runtime for H264 Cam
 Contains the official GStreamer RTSP server shared library and typelib,
 with only the shared-library dependencies used by the RTSP server.
EOF

parser_root="$work_dir/parser-package"
install -d -m 0755 \
  "$parser_root/DEBIAN" \
  "$parser_root/usr/lib/$multiarch/gstreamer-1.0" \
  "$parser_root/usr/lib/$multiarch"
install -m 0644 "$parser_plugin" \
  "$parser_root/usr/lib/$multiarch/gstreamer-1.0/libgstvideoparsersbad.so"
install -m 0644 "$codec_library" "$parser_root/usr/lib/$multiarch/$(basename "$codec_library")"
ln -s "$(basename "$codec_library")" \
  "$parser_root/usr/lib/$multiarch/libgstcodecparsers-1.0.so.0"
cat > "$parser_root/DEBIAN/control" <<EOF
Package: petcam-gstreamer-h264parse
Version: $plugin_version
Section: video
Priority: optional
Architecture: $architecture
Maintainer: H264 Cam Project
Depends: libc6, libglib2.0-0t64 | libglib2.0-0, libgstreamer1.0-0, libgstreamer-plugins-base1.0-0
Description: Minimal GStreamer H.264 parser runtime for H264 Cam
 Contains only the H.264 parser plugin and its codec-parser library.
EOF

# Verify the packaged RTSP library's ELF dependencies before dpkg installation.
if ldd "$rtsp_library" 2>&1 | grep -Fq "not found"; then
  ldd "$rtsp_library" >&2
  fail "The RTSP server library has unresolved shared-library dependencies."
fi
if LD_LIBRARY_PATH="$codec_root/usr/lib/$multiarch" ldd "$parser_plugin" 2>&1 | grep -Fq "not found"; then
  LD_LIBRARY_PATH="$codec_root/usr/lib/$multiarch" ldd "$parser_plugin" >&2
  fail "The H.264 parser plugin has unresolved shared-library dependencies."
fi

rtsp_package="$work_dir/petcam-gstreamer-rtsp-server.deb"
parser_package="$work_dir/petcam-gstreamer-h264parse.deb"
dpkg-deb --build --root-owner-group "$package_root" "$rtsp_package" >/dev/null
dpkg-deb --build --root-owner-group "$parser_root" "$parser_package" >/dev/null

log "Installing the minimal GStreamer RTSP server runtime ($rtsp_version)."
dpkg -i "$rtsp_package"
log "Installing the minimal GStreamer H.264 parser runtime ($plugin_version)."
dpkg -i "$parser_package"
ldconfig

runtime_available || fail "The minimal GStreamer RTSP server or H.264 parser could not be loaded."
log "The minimal GStreamer RTSP server and H.264 parser runtime are available."
