#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VERSION="${1:-}"
OUTPUT_DIR="${2:-$REPO_ROOT/dist/release}"
RELEASE_BASE_URL="${3:-https://github.com/mintymelinda/h264-cam/releases/download/$VERSION}"

log() { printf '[h264-cam-release] %s\n' "$*"; }
fail() { printf '[h264-cam-release] ERROR: %s\n' "$*" >&2; exit 1; }

[[ "$EUID" -eq 0 ]] || fail "Run as root: sudo ./release.sh <version> [output-directory]."
[[ -n "$VERSION" ]] || fail "A release version is required (for example: v1.0.0)."
[[ "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.-]+)?$ ]] || fail "Version must look like v1.2.3 or v1.2.3-rc.1."
[[ "$RELEASE_BASE_URL" == https://* ]] || fail "Release asset base URL must use HTTPS."
[[ -f "$REPO_ROOT/build-image.sh" && -f "$REPO_ROOT/create-imager-manifest.py" ]] || fail "Release scripts are missing."

OUTPUT_DIR="$(mkdir -p "$OUTPUT_DIR" && cd "$OUTPUT_DIR" && pwd)"
IMAGE_NAME="h264-cam-$VERSION-raspios-lite-armhf.img.xz"
IMAGE_PATH="$OUTPUT_DIR/$IMAGE_NAME"
CHECKSUM_PATH="$IMAGE_PATH.sha256"
MANIFEST_PATH="$OUTPUT_DIR/h264-cam-imager-manifest.json"
RELEASE_DATE="$(date -u +%F)"
SOURCE_COMMIT="$(git -C "$REPO_ROOT" rev-parse HEAD)"
IMAGE_URL="$RELEASE_BASE_URL/$IMAGE_NAME"

log "Building the final Raspberry Pi OS Lite Trixie ARMHF image ($VERSION)."
PETCAM_VERSION="$VERSION" PETCAM_SOURCE_COMMIT="$SOURCE_COMMIT" PETCAM_SOURCE_REF="$VERSION" "$REPO_ROOT/build-image.sh" "$OUTPUT_DIR"

[[ -s "$IMAGE_PATH" ]] || fail "Expected final image was not produced: $IMAGE_PATH"
[[ -s "$CHECKSUM_PATH" ]] || fail "Image checksum sidecar was not produced: $CHECKSUM_PATH"
xz --test "$IMAGE_PATH"
(cd "$OUTPUT_DIR" && sha256sum --check "$(basename "$CHECKSUM_PATH")")

log "Inspecting the exact compressed release image and verifying its installed application."
bash "$REPO_ROOT/scripts/verify-image-artifact.sh" "$IMAGE_PATH" "$VERSION" "$SOURCE_COMMIT"

log "Generating the Raspberry Pi Imager OS-list manifest."
python3 "$REPO_ROOT/create-imager-manifest.py" \
  "$IMAGE_PATH" "$MANIFEST_PATH" \
  --image-url "$IMAGE_URL" \
  --release-date "$RELEASE_DATE"

python3 -m json.tool "$MANIFEST_PATH" >/dev/null
log "Release image: $IMAGE_PATH"
log "SHA-256: $CHECKSUM_PATH"
log "Raspberry Pi Imager manifest: $MANIFEST_PATH"

if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  {
    printf 'image_path=%s\n' "$IMAGE_PATH"
    printf 'checksum_path=%s\n' "$CHECKSUM_PATH"
    printf 'manifest_path=%s\n' "$MANIFEST_PATH"
    printf 'image_url=%s\n' "$IMAGE_URL"
    printf 'release_date=%s\n' "$RELEASE_DATE"
    printf 'source_commit=%s\n' "$SOURCE_COMMIT"
  } >> "$GITHUB_OUTPUT"
fi
