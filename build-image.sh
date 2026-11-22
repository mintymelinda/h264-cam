#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR="${1:-$REPO_ROOT/dist/image}"
RASPIOS_IMAGE_URL="${PETCAM_RASPIOS_IMAGE_URL:-https://downloads.raspberrypi.com/raspios_lite_armhf_latest}"
VERSION="${PETCAM_VERSION:-development}"
SOURCE_COMMIT="${PETCAM_SOURCE_COMMIT:-}"
SOURCE_REF="${PETCAM_SOURCE_REF:-$VERSION}"
SOURCE_REPOSITORY="${PETCAM_SOURCE_REPOSITORY:-https://github.com/mintymelinda/h264-cam.git}"

log() { printf '[petcam-image] %s\n' "$*"; }
fail() { printf '[petcam-image] ERROR: %s\n' "$*" >&2; exit 1; }

[[ "$EUID" -eq 0 ]] || fail "Run this image customizer as root (for example: sudo $0 ...)."

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
[[ "$SOURCE_COMMIT" =~ ^[a-fA-F0-9]{40}$ ]] || fail "PETCAM_SOURCE_COMMIT must be the full 40-character release commit SHA."
[[ -n "$SOURCE_REF" && "$SOURCE_REF" != -* ]] || fail "PETCAM_SOURCE_REF must be a Git branch or tag."

mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"
BUILD_ROOT="$(mktemp -d)"
ROOTFS="$BUILD_ROOT/rootfs"
BOOT_MOUNT=""
LOOP_DEVICE=""
VERIFY_LOOP_DEVICE=""
VERIFY_ROOTFS="$BUILD_ROOT/verify-rootfs"
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
  for mount_path in "$VERIFY_ROOTFS" "$ROOTFS/run" "$ROOTFS/sys" "$ROOTFS/proc" "$ROOTFS/dev" "$BOOT_MOUNT" "$ROOTFS"; do
    if [[ -n "$mount_path" ]] && mountpoint -q "$mount_path"; then
      umount -R "$mount_path" 2>/dev/null || umount -lf "$mount_path" 2>/dev/null || true
    fi
  done
  if [[ -n "$VERIFY_LOOP_DEVICE" ]]; then losetup -d "$VERIFY_LOOP_DEVICE" 2>/dev/null || true; fi
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
printf '%s  %s\n' "$BASE_SHA256" "$BASE_ARCHIVE" | sha256sum --check --status || fail "Official Raspberry Pi OS image checksum verification failed."
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

if [[ -n "$CHROOT_EMULATOR" ]]; then
  install -D -m 0755 "$CHROOT_EMULATOR" "$ROOTFS/usr/bin/qemu-arm-static"
fi

log "Installing Git in the image, cloning the exact release tag, and running its top-level installer."
run_chroot /usr/bin/env SOURCE_COMMIT="$SOURCE_COMMIT" SOURCE_REF="$SOURCE_REF" SOURCE_REPOSITORY="$SOURCE_REPOSITORY" /bin/bash -e -c '
  test "$(dpkg --print-architecture)" = armhf
  test "$(getconf LONG_BIT)" = 32
  . /etc/os-release
  test "$VERSION_CODENAME" = trixie
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends git sudo ca-certificates
  install -d -m 0755 /opt/petcam
  rm -rf /opt/petcam/source
  GIT_TERMINAL_PROMPT=0 git clone --depth 1 --branch "$SOURCE_REF" --single-branch "$SOURCE_REPOSITORY" /opt/petcam/source
  actual_commit="$(git -C /opt/petcam/source rev-parse HEAD)"
  test "$actual_commit" = "$SOURCE_COMMIT"
  cd /opt/petcam/source
  sudo ./install.sh
' || fail "The real Git clone and top-level install.sh process failed inside the Raspberry Pi OS Trixie ARMHF environment."

INSTALLER_SHA256="$(sha256sum "$ROOTFS/opt/petcam/source/install.sh" | awk '{print $1}')"
[[ "$INSTALLER_SHA256" =~ ^[a-fA-F0-9]{64}$ ]] || fail "Could not calculate the cloned install.sh checksum."
log "Recording exact build provenance; retaining the cloned release source for on-device recovery."
install -d -m 0755 "$ROOTFS/opt/petcam"
printf 'PETCAM_VERSION=%s\nSOURCE_COMMIT=%s\nINSTALLER_SHA256=%s\n' "$VERSION" "$SOURCE_COMMIT" "$INSTALLER_SHA256" > "$ROOTFS/opt/petcam/build-info"
chmod 0644 "$ROOTFS/opt/petcam/build-info"

if (( RESOLV_REPLACED )); then
  rm -f "$ROOTFS/etc/resolv.conf"
  if [[ -n "$RESOLV_LINK" ]]; then ln -s "$RESOLV_LINK" "$ROOTFS/etc/resolv.conf"; elif (( RESOLV_WAS_REGULAR )); then cp -a "$RESOLV_BACKUP" "$ROOTFS/etc/resolv.conf"; fi
  RESOLV_REPLACED=0
fi
log "Cleaning package caches and verifying the installed application in the final root filesystem."
run_chroot /bin/bash -e -c '
  apt-get clean
  rm -rf /var/lib/apt/lists/*
  test -s /opt/petcam/build-info
  test -s /opt/petcam/src/petcam/cli.py
  test -s /opt/petcam/lib/libpetcam_live555.so
  test -x /usr/local/bin/petcam
  /usr/local/bin/petcam -h
  PYTHONPATH=/opt/petcam/src /usr/bin/python3 -c "from petcam.infrastructure.streaming.live555 import Live555Server; Live555Server(); print(\"Live555 native library loaded successfully.\")"
' || fail "The final image failed installed application or native Live555 verification."

if [[ -n "$CHROOT_EMULATOR" ]]; then
  rm -f "$ROOTFS/usr/bin/qemu-arm-static"
fi

log "Unmounting the customized stock image."
for mount_path in "$ROOTFS/run" "$ROOTFS/sys" "$ROOTFS/proc" "$ROOTFS/dev" "$BOOT_MOUNT" "$ROOTFS"; do
  if mountpoint -q "$mount_path"; then umount -R "$mount_path"; fi
done
losetup -d "$LOOP_DEVICE"
LOOP_DEVICE=""
sync

log "Reattaching the uncompressed image to verify filesystem changes survived unmount."
VERIFY_LOOP_DEVICE="$(losetup --find --show --partscan "$BASE_IMAGE")"
for _ in $(seq 1 30); do
  [[ -b "${VERIFY_LOOP_DEVICE}p2" ]] && break
  udevadm settle || true
  sleep 1
done
[[ -b "${VERIFY_LOOP_DEVICE}p2" ]] || fail "Could not reattach the customized image for persistence verification."
mkdir -p "$VERIFY_ROOTFS"
mount "${VERIFY_LOOP_DEVICE}p2" "$VERIFY_ROOTFS"
if [[ ! -s "$VERIFY_ROOTFS/opt/petcam/build-info" ]]; then
  log "The provenance file is missing after unmount. Contents of /opt:"
  find "$VERIFY_ROOTFS/opt" -maxdepth 4 -printf '%M %u:%g %p\\n' 2>/dev/null || true
  fail "Build provenance did not persist to the customized image filesystem."
fi
grep -Fxq "PETCAM_VERSION=$VERSION" "$VERIFY_ROOTFS/opt/petcam/build-info" || fail "Persisted image version does not match the build version."
grep -Fxq "SOURCE_COMMIT=$SOURCE_COMMIT" "$VERIFY_ROOTFS/opt/petcam/build-info" || fail "Persisted source commit does not match the build commit."
VERIFY_INSTALLER_SHA256="$(awk -F= '$1 == "INSTALLER_SHA256" { print $2 }' "$VERIFY_ROOTFS/opt/petcam/build-info")"
[[ "$VERIFY_INSTALLER_SHA256" =~ ^[a-fA-F0-9]{64}$ ]] || fail "Persisted installer checksum is invalid."
umount "$VERIFY_ROOTFS"
losetup -d "$VERIFY_LOOP_DEVICE"
VERIFY_LOOP_DEVICE=""
sync

FINAL_IMAGE="$OUTPUT_DIR/h264-cam-$VERSION-raspios-lite-armhf.img.xz"
log "Compressing the customized image."
xz --threads=0 --compress --fast --force --keep --stdout "$BASE_IMAGE" > "$FINAL_IMAGE"
[[ -s "$FINAL_IMAGE" ]] || fail "The customized image archive is empty."
xz --test "$FINAL_IMAGE" || fail "The customized image archive failed its integrity check."
(cd "$OUTPUT_DIR" && sha256sum "$(basename "$FINAL_IMAGE")" > "$(basename "$FINAL_IMAGE").sha256")
log "Created $FINAL_IMAGE"
log "Created $FINAL_IMAGE.sha256"
