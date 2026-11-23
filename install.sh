#!/usr/bin/env bash
set -Eeuo pipefail
REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="${PETCAM_INSTALL_ROOT:-/opt/petcam}"
log() { printf '[petcam-install] %s\n' "$*"; }
fail() { printf '[petcam-install] ERROR: %s\n' "$*" >&2; exit 1; }
[[ "$EUID" -eq 0 ]] || fail "Run as root: sudo $0"
command -v apt-get >/dev/null 2>&1 || fail "Raspberry Pi OS / Debian with apt is required."
command -v dpkg >/dev/null 2>&1 || fail "dpkg is required."
[[ -s "$REPO_ROOT/src/petcam/cli.py" && -f "$REPO_ROOT/requirements.apt" && -x "$REPO_ROOT/petcam" ]] || fail "Run this installer from a complete PetCam source checkout with a non-empty CLI module."
if EMPTY_PYTHON_FILE="$(find "$REPO_ROOT/src/petcam" -type f -name '*.py' -size 0 -print -quit)" && [[ -n "$EMPTY_PYTHON_FILE" ]]; then
  fail "PetCam Python source file is empty: $EMPTY_PYTHON_FILE"
fi
[[ -s "$REPO_ROOT/petcam" ]] || fail "The PetCam launcher source is empty."
grep -Fq 'exec /usr/bin/python3 -m petcam.cli' "$REPO_ROOT/petcam" || fail "The PetCam launcher source does not invoke the application CLI."
[[ -s "$REPO_ROOT/src/petcam/infrastructure/streaming/gstreamer.py" && -s "$REPO_ROOT/src/petcam/infrastructure/camera/capture.py" ]] || fail "GStreamer streaming or camera capture source files are missing."
[[ -s "$REPO_ROOT/scripts/install-gstreamer-runtime.sh" ]] || fail "The minimal GStreamer runtime installer is missing."
[[ -s "$REPO_ROOT/scripts/smoke-test-installed-launcher.py" ]] || fail "The installed-launcher pseudo-terminal smoke test is missing."
[[ -s "$REPO_ROOT/scripts/smoke-test-installed-help.py" ]] || fail "The exact installed-help smoke test is missing."
package_arch="$(dpkg --print-architecture)"
[[ "$package_arch" == "armhf" ]] || fail "PetCam requires ARMHF; detected '$package_arch'."
long_bits="$(getconf LONG_BIT)"
[[ "$long_bits" == "32" ]] || fail "PetCam requires a 32-bit userspace; detected '$long_bits'-bit."
cpuinfo="${PETCAM_CPUINFO_FILE:-/proc/cpuinfo}"
cpu_arch=""
if [[ -r "$cpuinfo" ]]; then
  cpu_arch="$(awk -F: '/^[[:space:]]*CPU architecture[[:space:]]*:/ { gsub(/[[:space:]]/, "", $2); print $2; exit }' "$cpuinfo")"
fi
if [[ "$cpu_arch" =~ ^[0-9]+$ ]]; then
  (( cpu_arch >= 6 )) || fail "PetCam requires ARMv6 or newer; detected ARM architecture $cpu_arch."
else
  case "$(uname -m)" in
    armv6l|armv7l|armv8l|aarch64) ;;
    *) fail "Unable to confirm an ARMv6-or-newer CPU; detected '$(uname -m)'." ;;
  esac
fi
log "Refreshing the Raspberry Pi OS package indexes."
apt-get update
APT_PACKAGES=()
while IFS= read -r package || [[ -n "$package" ]]; do
  package="${package%%#*}"
  package="$(sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' <<< "$package")"
  [[ -z "$package" ]] && continue
  [[ "$package" =~ ^[a-z0-9][a-z0-9+.-]*$ ]] || fail "Invalid apt package name in requirements.apt: $package"
  APT_PACKAGES+=("$package")
done < "$REPO_ROOT/requirements.apt"
(("${#APT_PACKAGES[@]}" > 0)) || fail "No apt packages were declared in requirements.apt."
log "Installing native H.264 capture and GStreamer RTSP dependencies."
apt-get install -y --no-install-recommends --no-upgrade "${APT_PACKAGES[@]}"
log "Installing the minimal GStreamer RTSP, appsrc, RTP and H.264 runtime."
bash "$REPO_ROOT/scripts/install-gstreamer-runtime.sh"
log "Installing the Python application source."
install -d -m 0755 "$APP_ROOT" /usr/local/bin
rm -rf "$APP_ROOT/src"
cp -a "$REPO_ROOT/src" "$APP_ROOT/src"
if ! diff -qr "$REPO_ROOT/src/petcam" "$APP_ROOT/src/petcam"; then
  fail "The installed PetCam Python package does not match the installer source."
fi
chown -R root:root "$APP_ROOT"
log "Compiling and smoke-testing the installed Python application."
/usr/bin/python3 -m compileall -q "$APP_ROOT/src/petcam"
if ! HELP_OUTPUT="$(PYTHONPATH="$APP_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" /usr/bin/python3 -m petcam.cli -h 2>&1)"; then
  printf '%s\n' "$HELP_OUTPUT" >&2
  fail "The installed PetCam Python CLI failed its startup check."
fi
grep -Fq "usage: petcam" <<< "$HELP_OUTPUT" || fail "The installed PetCam Python CLI did not produce its expected help output."
log "Validating the installed GStreamer RTSP server and H.264 pipeline elements."
PYTHONPATH="$APP_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" /usr/bin/python3 -c 'from petcam.infrastructure.streaming.gstreamer import validate_runtime; validate_runtime(); print("GStreamer RTSP server and H.264 elements loaded successfully.")'
log "Leaving build dependencies installed. Do not purge packages or run autoremove on the Pi."
LAUNCHER_TMP="/usr/local/bin/.petcam.${BASHPID}"
log "Installing and validating the PetCam command launcher."
install -m 0755 "$REPO_ROOT/petcam" "$LAUNCHER_TMP"
if ! cmp -s "$REPO_ROOT/petcam" "$LAUNCHER_TMP"; then
  rm -f "$LAUNCHER_TMP"
  fail "The installed PetCam launcher failed its integrity check."
fi
if ! HELP_OUTPUT="$("$LAUNCHER_TMP" -h 2>&1)"; then
  rm -f "$LAUNCHER_TMP"
  fail "The PetCam CLI help command failed to start."
fi
if ! grep -Fq "usage: petcam" <<< "$HELP_OUTPUT"; then
  rm -f "$LAUNCHER_TMP"
  fail "The PetCam CLI did not produce its expected help output; refusing to install a silent no-op launcher."
fi
mv -f "$LAUNCHER_TMP" /usr/local/bin/petcam
[[ -s /usr/local/bin/petcam && -x /usr/local/bin/petcam ]] || fail "The PetCam command launcher was not installed correctly."
log "Verifying the installed command's exact help output."
if ! /usr/bin/python3 "$REPO_ROOT/scripts/smoke-test-installed-help.py"; then
  fail "The installed /usr/local/bin/petcam -h output did not match exactly."
fi
log "Verifying the installed command opens the interactive setup UI."
if ! TERM=xterm-256color /usr/bin/python3 "$REPO_ROOT/scripts/smoke-test-installed-launcher.py"; then
  fail "The installed /usr/local/bin/petcam command did not remain interactive."
fi
log "Installed PetCam at $APP_ROOT."
log "Verified the installed launcher, interactive setup UI, Python package, and GStreamer RTSP runtime."
log "Run 'petcam' for guided setup or 'petcam -h' for command help."
