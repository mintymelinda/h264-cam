#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR="${1:-$REPO_ROOT/dist/image}"
RASPIOS_IMAGE_URL="${PETCAM_RASPIOS_IMAGE_URL:-https://downloads.raspberrypi.com/raspios_lite_armhf_latest}"
VERSION="${PETCAM_VERSION:-development}"

log() { printf '[petcam-image] %s\\n' "$*"; }
fail() { printf '[petcam-image] ERROR: %s\\n' "$*" >&2; exit 1; }

[[ "$EUID" -eq 0 ]] || fail "Run this image customizer as root (for example: sudo $0 ...)."
[[ -s "$REPO_ROOT/install.sh" && -s "$REPO_ROOT/requirements.apt" && -s "$REPO_ROOT/requirements.txt" && -s "$REPO_ROOT/src/petcam/cli.py" ]] || fail "Run this script from a complete PetCam source checkout; required installer and application files must be non-empty."
bash -n "$REPO_ROOT/install.sh" || fail "The source installer has invalid shell syntax."
[[ -s "$REPO_ROOT/petcam" && -x "$REPO_ROOT/petcam" ]] || fail "The source PetCam launcher is missing, empty, or not executable."
[[ -s "$REPO_ROOT/scripts/smoke-test-installed-launcher.py" && -s "$REPO_ROOT/scripts/smoke-test-installed-help.py" ]] || fail "Required installed-launcher smoke tests are missing or empty."
for tool in curl xz losetup mount umount blkid chroot sha256sum; do
  command -v "$tool" >/dev/null 2>&1 || fail "Required host tool is missing: $tool"
done
CHROOT_EMULATOR=""
if [[ -x /lib/ld-linux-armhf.so.3 ]] && /lib/ld-linux-armhf.so.3 --help >/dev/null 2>&1; then
  log "Native ARMHF execution is available."
elif command -v qemu-arm-static >/dev/null 2>&1; then
  CHROOT_EMULATOR="$(command -v qemu-arm-static)"
  log "ARMHF chroot will run through QEMU user-mode emulation."
else
  fail "ARMHF execution is unavailable; use a native ARMHF-capable host or install qemu-user-static."
fi

run_chroot() {
  if [[ -n "$CHROOT_EMULATOR" ]]; then
    chroot "$ROOTFS" /usr/bin/qemu-arm-static -L / "$@"
  else
    chroot "$ROOTFS" "$@"
  fi
}

[[ "$VERSION" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]*$ ]] || fail "Invalid image version."

mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"
BUILD_ROOT="$(mktemp -d)"
ROOTFS="$BUILD_ROOT/rootfs"
BOOT_MOUNT=""
LOOP_DEVICE=""
RESOLV_LINK=""
RESOLV_BACKUP=""
RESOLV_WAS_REGULAR=0
RESOLV_REPLACED=0

cleanup() {
  set +e
  if (( RESOLV_REPLACED )); then
    rm -f "$ROOTFS/etc/resolv.conf"
    if [[ -n "$RESOLV_LINK" ]]; then ln -s "$RESOLV_LINK" "$ROOTFS/etc/resolv.conf"; elif (( RESOLV_WAS_REGULAR )); then cp -a "$RESOLV_BACKUP" "$ROOTFS/etc/resolv.conf"; fi
  fi
  for mount_path in "$ROOTFS/run" "$ROOTFS/sys" "$ROOTFS/proc" "$ROOTFS/dev" "$BOOT_MOUNT" "$ROOTFS"; do
    if [[ -n "$mount_path" ]] && mountpoint -q "$mount_path"; then
      umount -R "$mount_path" 2>/dev/null || umount -lf "$mount_path" 2>/dev/null || true
    fi
  done
  if [[ -n "$LOOP_DEVICE" ]]; then losetup -d "$LOOP_DEVICE" 2>/dev/null || true; fi
  rm -rf "$BUILD_ROOT"
}
trap cleanup EXIT

BASE_ARCHIVE="$BUILD_ROOT/raspios-lite-armhf.img.xz"
BASE_IMAGE="$BUILD_ROOT/raspios-lite-armhf.img"
BASE_CHECKSUM="$BUILD_ROOT/raspios-lite-armhf.img.xz.sha256"

log "Downloading the official Raspberry Pi OS Lite 32-bit image."
curl --fail --location --retry 5 --retry-all-errors --silent --show-error "$RASPIOS_IMAGE_URL" --output "$BASE_ARCHIVE"
curl --fail --location --retry 5 --retry-all-errors --silent --show-error "${RASPIOS_IMAGE_URL}.sha256" --output "$BASE_CHECKSUM"

BASE_SHA256="$(awk 'NR == 1 { print $1 }' "$BASE_CHECKSUM")"
[[ "$BASE_SHA256" =~ ^[a-fA-F0-9]{64}$ ]] || fail "Raspberry Pi returned an invalid SHA-256 sidecar."
printf '%s  %s\\n' "$BASE_SHA256" "$BASE_ARCHIVE" | sha256sum --check --status || fail "Official Raspberry Pi OS image checksum verification failed."
log "Verified upstream image SHA-256: $BASE_SHA256"

log "Decompressing the verified stock image."
xz --decompress --threads=0 --keep "$BASE_ARCHIVE"
[[ -s "$BASE_IMAGE" ]] || fail "The decompressed Raspberry Pi OS image is empty."

LOOP_DEVICE="$(losetup --find --show --partscan "$BASE_IMAGE")"
if [[ ! -b "${LOOP_DEVICE}p1" || ! -b "${LOOP_DEVICE}p2" ]]; then
  udevadm settle || true
  for _ in $(seq 1 30); do
    [[ -b "${LOOP_DEVICE}p1" && -b "${LOOP_DEVICE}p2" ]] && break
    sleep 1
  done
fi
[[ -b "${LOOP_DEVICE}p1" && -b "${LOOP_DEVICE}p2" ]] || fail "Expected boot and root partitions were not found in the official image."
[[ "$(blkid -s TYPE -o value "${LOOP_DEVICE}p1")" == vfat ]] || fail "The first image partition is not the expected FAT boot partition."
[[ "$(blkid -s TYPE -o value "${LOOP_DEVICE}p2")" == ext4 ]] || fail "The second image partition is not the expected ext4 root partition."

mkdir -p "$ROOTFS"
mount "${LOOP_DEVICE}p2" "$ROOTFS"
BOOT_PATH="$(awk '$1 !~ /^#/ && ($2 == "/boot/firmware" || $2 == "/boot") { print $2; exit }' "$ROOTFS/etc/fstab")"
[[ "$BOOT_PATH" == "/boot/firmware" || "$BOOT_PATH" == "/boot" ]] || fail "Could not determine the boot partition mount point from the stock image fstab."
BOOT_MOUNT="$ROOTFS$BOOT_PATH"
mkdir -p "$BOOT_MOUNT"
mount "${LOOP_DEVICE}p1" "$BOOT_MOUNT"

log "Preparing ARMHF chroot and network access."
if [[ -L "$ROOTFS/etc/resolv.conf" ]]; then
  RESOLV_LINK="$(readlink "$ROOTFS/etc/resolv.conf")"
  rm "$ROOTFS/etc/resolv.conf"
elif [[ -e "$ROOTFS/etc/resolv.conf" ]]; then
  RESOLV_BACKUP="$BUILD_ROOT/resolv.conf.original"
  cp -a "$ROOTFS/etc/resolv.conf" "$RESOLV_BACKUP"
  RESOLV_WAS_REGULAR=1
fi
RESOLV_REPLACED=1
cp -L /etc/resolv.conf "$ROOTFS/etc/resolv.conf"
RESOLV_REPLACED=1

mount --rbind /dev "$ROOTFS/dev"
mount --make-rslave "$ROOTFS/dev"
mount --rbind /proc "$ROOTFS/proc"
mount --make-rslave "$ROOTFS/proc"
mount --rbind /sys "$ROOTFS/sys"
mount --make-rslave "$ROOTFS/sys"
mkdir -p "$ROOTFS/run"
mount --rbind /run "$ROOTFS/run"
mount --make-rslave "$ROOTFS/run"

log "Installing PetCam into the existing Raspberry Pi OS root filesystem."
if [[ -n "$CHROOT_EMULATOR" ]]; then
  install -D -m 0755 "$CHROOT_EMULATOR" "$ROOTFS/usr/bin/qemu-arm-static"
fi
mkdir -p "$ROOTFS/tmp/petcam-installer"
cp -a "$REPO_ROOT/install.sh" "$REPO_ROOT/requirements.apt" "$REPO_ROOT/requirements.txt" "$REPO_ROOT/petcam" "$REPO_ROOT/src" "$REPO_ROOT/scripts" "$ROOTFS/tmp/petcam-installer/"
run_chroot /bin/bash -e -c '
  test "$(dpkg --print-architecture)" = armhf
  test "$(getconf LONG_BIT)" = 32
  . /etc/os-release
  test "$VERSION_CODENAME" = trixie
  test -s /tmp/petcam-installer/install.sh
  bash -n /tmp/petcam-installer/install.sh
  /tmp/petcam-installer/install.sh
' || fail "PetCam installation in the Raspberry Pi OS Trixie image failed."

log "Preserving the complete recovery installer on the image."
install -d -m 0755 "$ROOTFS/opt/petcam/installer"
cp -a "$ROOTFS/tmp/petcam-installer/." "$ROOTFS/opt/petcam/installer/"
cmp -s "$REPO_ROOT/install.sh" "$ROOTFS/opt/petcam/installer/install.sh" || fail "The retained recovery installer differs from the source installer."
[[ -s "$ROOTFS/opt/petcam/installer/install.sh" ]] || fail "The retained recovery installer is empty."
cat > "$ROOTFS/opt/petcam/install.sh" <<'PETCAM_INSTALL_WRAPPER'
#!/bin/sh
set -eu
exec /opt/petcam/installer/install.sh "$@"
PETCAM_INSTALL_WRAPPER
chmod 0755 "$ROOTFS/opt/petcam/install.sh"
EXPECTED_WRAPPER="$BUILD_ROOT/expected-recovery-wrapper"
cat > "$EXPECTED_WRAPPER" <<'PETCAM_INSTALL_WRAPPER'
#!/bin/sh
set -eu
exec /opt/petcam/installer/install.sh "$@"
PETCAM_INSTALL_WRAPPER
cmp -s "$EXPECTED_WRAPPER" "$ROOTFS/opt/petcam/install.sh" || fail "The on-device recovery wrapper contents are incorrect."
rm -rf "$ROOTFS/tmp/petcam-installer"

if (( RESOLV_REPLACED )); then
  rm -f "$ROOTFS/etc/resolv.conf"
  if [[ -n "$RESOLV_LINK" ]]; then ln -s "$RESOLV_LINK" "$ROOTFS/etc/resolv.conf"; elif (( RESOLV_WAS_REGULAR )); then cp -a "$RESOLV_BACKUP" "$ROOTFS/etc/resolv.conf"; fi
  RESOLV_REPLACED=0
fi
log "Cleaning package caches and temporary installer files."
run_chroot /bin/bash -e -c 'apt-get clean; rm -rf /var/lib/apt/lists/*'

log "Verifying the final root filesystem after installer staging and cleanup."
run_chroot /bin/bash -e -c '
  test -s /opt/petcam/install.sh
  test -x /opt/petcam/install.sh
  test -s /opt/petcam/installer/install.sh
  test -x /opt/petcam/installer/install.sh
  bash -n /opt/petcam/install.sh
  bash -n /opt/petcam/installer/install.sh
  test -s /opt/petcam/installer/requirements.apt
  test -s /opt/petcam/installer/requirements.txt
  test -x /opt/petcam/installer/petcam
  test -s /opt/petcam/installer/src/petcam/cli.py
  test -s /opt/petcam/installer/scripts/smoke-test-installed-launcher.py
  test -s /opt/petcam/installer/scripts/smoke-test-installed-help.py
  cmp -s /opt/petcam/installer/install.sh /opt/petcam/install.sh && exit 1 || :
  test -x /usr/local/bin/petcam
  test -s /opt/petcam/src/petcam/cli.py
  test -s /opt/petcam/lib/libpetcam_live555.so
  /usr/bin/python3 /opt/petcam/installer/scripts/smoke-test-installed-help.py
  TERM=xterm-256color /usr/bin/python3 /opt/petcam/installer/scripts/smoke-test-installed-launcher.py
' || fail "The final image root filesystem failed its installed-launcher or recovery-installer checks."

if [[ -n "$CHROOT_EMULATOR" ]]; then
  rm -f "$ROOTFS/usr/bin/qemu-arm-static"
fi

log "Unmounting the customized stock image."
for mount_path in "$ROOTFS/run" "$ROOTFS/sys" "$ROOTFS/proc" "$ROOTFS/dev" "$BOOT_MOUNT" "$ROOTFS"; do
  if mountpoint -q "$mount_path"; then umount -R "$mount_path"; fi
done
losetup -d "$LOOP_DEVICE"
LOOP_DEVICE=""

FINAL_IMAGE="$OUTPUT_DIR/h264-cam-$VERSION-raspios-lite-armhf.img.xz"
log "Compressing the customized image."
xz --threads=0 --compress --fast --force --keep --stdout "$BASE_IMAGE" > "$FINAL_IMAGE"
[[ -s "$FINAL_IMAGE" ]] || fail "The customized image archive is empty."
xz --test "$FINAL_IMAGE" || fail "The customized image archive failed its integrity check."
(cd "$OUTPUT_DIR" && sha256sum "$(basename "$FINAL_IMAGE")" > "$(basename "$FINAL_IMAGE").sha256")
log "Created $FINAL_IMAGE"
log "Created $FINAL_IMAGE.sha256"
