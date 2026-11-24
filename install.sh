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
[[ -f "$REPO_ROOT/src/petcam/infrastructure/streaming/live555_server.cpp" ]] || fail "Live555 installer source files are missing."
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
log "Installing native H.264 capture and Live555 build dependencies."
apt-get install -y --no-install-recommends "${APT_PACKAGES[@]}"
log "Installing the Python application source."
install -d -m 0755 "$APP_ROOT" /usr/local/bin "$APP_ROOT/lib"
rm -rf "$APP_ROOT/src"
cp -a "$REPO_ROOT/src" "$APP_ROOT/src"
if ! diff -qr "$REPO_ROOT/src/petcam" "$APP_ROOT/src/petcam"; then
  fail "The installed PetCam Python package does not match the installer source."
fi
chown -R root:root "$APP_ROOT"
log "Compiling and checking the installed Python application."
/usr/bin/python3 -m compileall -q "$APP_ROOT/src/petcam"
if ! HELP_OUTPUT="$(PYTHONPATH="$APP_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" /usr/bin/python3 -m petcam.cli -h 2>&1)"; then
  printf '%s\n' "$HELP_OUTPUT" >&2
  fail "The installed PetCam Python CLI failed its help check."
fi
grep -Fq "usage: petcam" <<< "$HELP_OUTPUT" || fail "The installed PetCam Python CLI did not produce its expected help output."
log "Compiling the Live555 RTSP/RTP server for ARMHF."
NATIVE_TMP="$(mktemp "$APP_ROOT/lib/.libpetcam_live555.so.XXXXXX")"
trap 'rm -f "$NATIVE_TMP"' EXIT
g++ -std=c++17 -O2 -fPIC -shared \
  -I/usr/include/liveMedia -I/usr/include/groupsock \
  -I/usr/include/BasicUsageEnvironment -I/usr/include/UsageEnvironment \
  "$APP_ROOT/src/petcam/infrastructure/streaming/live555_server.cpp" \
  -o "$NATIVE_TMP" \
  -lliveMedia -lgroupsock -lBasicUsageEnvironment -lUsageEnvironment -lopus -pthread
[[ -s "$NATIVE_TMP" ]] || fail "The Live555 compiler produced an empty shared library."
ELF_HEADER="$(readelf -h "$NATIVE_TMP")" || fail "The compiled Live555 library is not a valid ELF shared library."
grep -Eq 'Class:[[:space:]]+ELF32' <<< "$ELF_HEADER" || fail "The Live555 library is not a 32-bit ELF binary."
grep -Eq 'Machine:[[:space:]]+ARM' <<< "$ELF_HEADER" || fail "The Live555 library was not compiled for ARM."
NATIVE_DEPS="$(ldd "$NATIVE_TMP" 2>&1)" || fail "Unable to inspect Live555 library dependencies."
if grep -Fq "not found" <<< "$NATIVE_DEPS"; then
  printf '%s\n' "$NATIVE_DEPS" >&2
  fail "The compiled Live555 library has unresolved runtime dependencies."
fi
chmod 0755 "$NATIVE_TMP"
chown root:root "$NATIVE_TMP"
mv -f "$NATIVE_TMP" "$APP_ROOT/lib/libpetcam_live555.so"
trap - EXIT
[[ -s "$APP_ROOT/lib/libpetcam_live555.so" ]] || fail "The installed Live555 shared library is empty."
log "Checking that the installed Python binding can load the compiled Live555 library."
PYTHONPATH="$APP_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" /usr/bin/python3 -c 'from petcam.infrastructure.streaming.live555 import Live555Server; Live555Server(); print("Live555 native library loaded successfully.")'
log "Leaving build dependencies installed. Do not purge packages or run autoremove on the Pi."
LAUNCHER_TMP="/usr/local/bin/.petcam.${BASHPID}"
log "Installing the PetCam command launcher."
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
[[ -s "$REPO_ROOT/systemd/petcam.service" ]] || fail "The persistent PetCam systemd service unit is missing."
command -v systemctl >/dev/null 2>&1 || fail "systemd systemctl is required."
log "Installing the persistent RTSP service definition."
install -d -m 0755 /etc/petcam /var/log/petcam
SERVICE_TMP="$(mktemp /etc/systemd/system/.petcam.service.XXXXXX)"
trap 'rm -f "$SERVICE_TMP"' EXIT
sed "s|/opt/petcam|$APP_ROOT|g" "$REPO_ROOT/systemd/petcam.service" > "$SERVICE_TMP"
install -o root -g root -m 0644 "$SERVICE_TMP" /etc/systemd/system/petcam.service
rm -f "$SERVICE_TMP"
trap - EXIT
systemctl daemon-reload
log "Installed PetCam at $APP_ROOT."
log "Verified the Python package, CLI help, and native Live555 library."
log "Camera detection, capture, and RTSP streaming are not tested during installation."
log "Run 'petcam' and complete camera setup once; it saves the config and enables the RTSP service at boot."
log "After setup, 'petcam' opens the saved stream status; use 'petcam --configure' to change settings."
log "Run 'petcam -h' for command help."
