#!/usr/bin/env bash
set -Eeuo pipefail
IMAGE_ARCHIVE="${1:-}"
EXPECTED_VERSION="${2:-}"
EXPECTED_COMMIT="${3:-}"
log() { printf '[petcam-artifact] %s\n' "$*"; }
fail() { printf '[petcam-artifact] ERROR: %s\n' "$*" >&2; exit 1; }
[[ "$EUID" -eq 0 ]] || fail "Run as root."
[[ -s "$IMAGE_ARCHIVE" ]] || fail "Compressed image archive is missing or empty."
[[ -n "$EXPECTED_VERSION" && "$EXPECTED_VERSION" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]*$ ]] || fail "Expected image version is invalid."
[[ "$EXPECTED_COMMIT" =~ ^[a-fA-F0-9]{40}$ ]] || fail "Expected source commit must be a full Git SHA."
for tool in xz losetup mount umount mountpoint blkid chroot sha256sum udevadm; do
  command -v "$tool" >/dev/null 2>&1 || fail "Required host tool is missing: $tool"
done
CHROOT_EMULATOR=""
if [[ -x /lib/ld-linux-armhf.so.3 ]] && /lib/ld-linux-armhf.so.3 --help >/dev/null 2>&1; then
  log "Native ARMHF execution is available."
elif command -v qemu-arm-static >/dev/null 2>&1; then
  CHROOT_EMULATOR="$(command -v qemu-arm-static)"
else
  fail "ARMHF execution is unavailable; install qemu-user-static."
fi
WORK_ROOT="$(mktemp -d)"
ROOTFS="$WORK_ROOT/rootfs"
IMAGE_FILE="$WORK_ROOT/release-image.img"
LOOP_DEVICE=""
RESOLV_BACKUP=""
RESOLV_REPLACED=0
cleanup() {
  set +e
  if (( RESOLV_REPLACED )); then
    rm -f "$ROOTFS/etc/resolv.conf"
    cp -a "$RESOLV_BACKUP" "$ROOTFS/etc/resolv.conf"
  fi
  for path in "$ROOTFS/run" "$ROOTFS/sys" "$ROOTFS/proc" "$ROOTFS/dev" "$ROOTFS"; do
    if [[ -n "$path" ]] && mountpoint -q "$path"; then
      umount -R "$path" 2>/dev/null || umount -lf "$path" 2>/dev/null || true
    fi
  done
  if [[ -n "$LOOP_DEVICE" ]]; then losetup -d "$LOOP_DEVICE" 2>/dev/null || true; fi
  rm -rf "$WORK_ROOT"
}
trap cleanup EXIT
log "Decompressing the exact release archive for filesystem inspection."
xz --test "$IMAGE_ARCHIVE" || fail "The release image failed XZ integrity verification."
xz --decompress --stdout "$IMAGE_ARCHIVE" > "$IMAGE_FILE"
[[ -s "$IMAGE_FILE" ]] || fail "Decompressed release image is empty."
LOOP_DEVICE="$(losetup --find --show --partscan "$IMAGE_FILE")"
for _ in $(seq 1 30); do
  [[ -b "${LOOP_DEVICE}p1" && -b "${LOOP_DEVICE}p2" ]] && break
  udevadm settle || true
  sleep 1
done
[[ -b "${LOOP_DEVICE}p1" && -b "${LOOP_DEVICE}p2" ]] || fail "Release image does not contain expected boot and root partitions."
[[ "$(blkid -s TYPE -o value "${LOOP_DEVICE}p2")" == ext4 ]] || fail "Release root partition is not ext4."
mkdir -p "$ROOTFS"
mount "${LOOP_DEVICE}p2" "$ROOTFS"
log "Checking provenance and installed PetCam files inside the compressed artifact."
[[ -s "$ROOTFS/opt/petcam/build-info" ]] || fail "Image provenance file is missing."
grep -Fxq "PETCAM_VERSION=$EXPECTED_VERSION" "$ROOTFS/opt/petcam/build-info" || fail "Image version does not match the release tag."
grep -Fxq "SOURCE_COMMIT=$EXPECTED_COMMIT" "$ROOTFS/opt/petcam/build-info" || fail "Image source commit does not match the checked-out release tag."
INSTALLER_SHA256="$(awk -F= '$1 == "INSTALLER_SHA256" { print $2 }' "$ROOTFS/opt/petcam/build-info")"
[[ "$INSTALLER_SHA256" =~ ^[a-fA-F0-9]{64}$ ]] || fail "Image provenance contains an invalid installer checksum."
printf '%s  %s\n' "$INSTALLER_SHA256" "$ROOTFS/opt/petcam/installer/install.sh" | sha256sum --check --status || fail "Retained installer checksum does not match image provenance."
for file in \
  /opt/petcam/install.sh \
  /opt/petcam/installer/install.sh \
  /opt/petcam/installer/requirements.apt \
  /opt/petcam/installer/requirements.txt \
  /opt/petcam/installer/petcam \
  /opt/petcam/installer/src/petcam/cli.py \
  /opt/petcam/src/petcam/cli.py \
  /opt/petcam/lib/libpetcam_live555.so \
  /usr/local/bin/petcam; do
  [[ -s "$ROOTFS$file" ]] || fail "Required installed image file is missing or empty: $file"
done
[[ -x "$ROOTFS/opt/petcam/install.sh" && -x "$ROOTFS/opt/petcam/installer/install.sh" && -x "$ROOTFS/usr/local/bin/petcam" ]] || fail "An installed PetCam entry point is not executable."
grep -Fq 'exec /opt/petcam/installer/install.sh "$@"' "$ROOTFS/opt/petcam/install.sh" || fail "Recovery wrapper does not invoke the retained installer."
if [[ -n "$CHROOT_EMULATOR" ]]; then
  install -D -m 0755 "$CHROOT_EMULATOR" "$ROOTFS/usr/bin/qemu-arm-static"
fi
cp -a "$ROOTFS/etc/resolv.conf" "$WORK_ROOT/resolv.conf.original"
RESOLV_BACKUP="$WORK_ROOT/resolv.conf.original"
rm -f "$ROOTFS/etc/resolv.conf"
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
run_chroot() {
  if [[ -n "$CHROOT_EMULATOR" ]]; then
    chroot "$ROOTFS" /usr/bin/qemu-arm-static -L / "$@"
  else
    chroot "$ROOTFS" "$@"
  fi
}
run_chroot /bin/bash -e -c '
  test "$(dpkg --print-architecture)" = armhf
  test "$(getconf LONG_BIT)" = 32
  . /etc/os-release
  test "$VERSION_CODENAME" = trixie
' || fail "Extracted release image is not Raspberry Pi OS Trixie ARMHF."
log "Executing the recovery wrapper from the extracted release image."
run_chroot /opt/petcam/install.sh || fail "The retained recovery installer failed when executed from the release image."
log "Rechecking the extracted image after the recovery installation."
run_chroot /bin/bash -e -c '
  test "$(dpkg --print-architecture)" = armhf
  test "$(getconf LONG_BIT)" = 32
  test -s /opt/petcam/build-info
  test -s /opt/petcam/installer/install.sh
  test -x /opt/petcam/install.sh
  test -x /opt/petcam/installer/install.sh
  test -x /usr/local/bin/petcam
  test -s /opt/petcam/src/petcam/cli.py
  test -s /opt/petcam/lib/libpetcam_live555.so
  /usr/bin/python3 /opt/petcam/installer/scripts/smoke-test-installed-help.py
  TERM=xterm-256color /usr/bin/python3 /opt/petcam/installer/scripts/smoke-test-installed-launcher.py
' || fail "The extracted release image failed post-recovery installation checks."
if [[ -n "$CHROOT_EMULATOR" ]]; then
  rm -f "$ROOTFS/usr/bin/qemu-arm-static"
fi
log "The compressed release artifact contains a complete PetCam install, and its retained recovery installer passed."
