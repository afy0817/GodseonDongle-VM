#!/bin/sh
# SPDX-FileCopyrightText: 2026 GodSeon
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Builds the VM image in an Alpine aarch64 container (Docker Desktop on Apple Silicon runs it
# natively, as does an arm64 Linux host):
#   dist/vm_assets/                  Image-lts, initramfs-thrurndis-wifi, manifest.json,
#                                    SHA256SUMS and compliance/ (license notices)
#   dist/vm_assets.zip               that folder zipped: what the apps download and bundle
#   dist/vm_assets-sources.tar.gz    the corresponding source (compliance/SOURCE.md)
#   dist/SHA256SUMS                  of the two release files
#
#   ./build.sh                  release build
#   ./build.sh --quick          image only, without notices or sources: local tests, never publish
#   ./build.sh --sources-hash   prints the hash of this repository that the manifest keeps
#
# GODSEON_VM_VERSION names the image (default: a date stamp). Downloads (distfiles, aports
# recipes, rtw88 sources) are kept in dist/cache between builds.

set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)

# Every file of the repository but dist/ and .git; an app's build script rebuilds or downloads
# again when the manifest has another hash.
sources_hash() {
    (cd "$ROOT" && find . \( -path ./dist -o -path ./.git -o -name __pycache__ \) -prune -o \
        -type f ! -name '.*' -print | LC_ALL=C sort | xargs shasum -a 256) | shasum -a 256 | cut -c1-16
}

QUICK=""
case "${1:-}" in
    --sources-hash) sources_hash; exit 0 ;;
    --quick) QUICK=1 ;;
    "") ;;
    *) echo "usage: $0 [--quick | --sources-hash]" >&2; exit 2 ;;
esac

DIST=$ROOT/dist
ALPINE_IMAGE=${ALPINE_IMAGE:-alpine:edge}
VERSION=${GODSEON_VM_VERSION:-$(date +%Y%m%d.%H%M)}
COMMIT=$(git -C "$ROOT" rev-parse --verify -q HEAD 2>/dev/null || true)
if [ -n "$COMMIT" ] && [ -n "$(git -C "$ROOT" status --porcelain 2>/dev/null)" ]; then
    COMMIT="$COMMIT-dirty"
fi

mkdir -p "$DIST/vm_assets"
CACHE=${GODSEON_BUILD_CACHE:-$DIST/cache}
mkdir -p "$CACHE"
CACHE=$(CDPATH='' cd -- "$CACHE" && pwd)

# Firmware packages named in drivers.conf (pkg=a,b) go into the builder image.
FIRMWARE_PACKAGES=$(grep -v '^[[:space:]]*#' "$ROOT/drivers.conf" |
    sed -n 's/.*[[:space:]]pkg=\([^[:space:]]*\).*/\1/p' | tr ',' '\n' | sort -u | tr '\n' ' ')
case "$FIRMWARE_PACKAGES" in
    *[!a-z0-9_.\ -]*) echo "drivers.conf: invalid firmware package name in: $FIRMWARE_PACKAGES" >&2; exit 1 ;;
esac

# GODSEON_VM_BUILDER_PULL=1 refreshes the cached kernel/firmware packages.
docker build --platform linux/arm64 --build-arg ALPINE_IMAGE="$ALPINE_IMAGE" \
    --build-arg FIRMWARE_PACKAGES="$FIRMWARE_PACKAGES" \
    ${GODSEON_VM_BUILDER_PULL:+--pull --no-cache} -t godseon-vm-builder "$ROOT/builder"

rm -f "$DIST/vm_assets.zip" "$DIST/vm_assets-sources.tar.gz" "$DIST/SHA256SUMS"
COMPLIANCE_ARGS="--sources-out /dist/vm_assets-sources.tar.gz"
[ -n "$QUICK" ] && COMPLIANCE_ARGS=--no-compliance
docker run --rm --platform linux/arm64 -e PYTHONDONTWRITEBYTECODE=1 \
    -v "$ROOT:/src:ro" -v "$DIST:/dist" -v "$CACHE:/cache" godseon-vm-builder \
    python3 /src/builder/build_assets.py --src /src --out /dist/vm_assets --cache /cache \
    --version "$VERSION" --sources "$(sources_hash)" --commit "$COMMIT" \
    $COMPLIANCE_ARGS \
    ${GODSEON_RTW88_PATCHES:+$( [ "$GODSEON_RTW88_PATCHES" = 0 ] && echo --no-kernel-patches)}

if [ -n "$QUICK" ]; then
    echo "VM assets (quick build, no notices or sources; do not publish): $DIST/vm_assets"
    exit 0
fi

(cd "$DIST" && zip -qrX vm_assets.zip vm_assets &&
    shasum -a 256 vm_assets.zip vm_assets-sources.tar.gz >SHA256SUMS)
echo "VM assets: $DIST/vm_assets"
cat "$DIST/SHA256SUMS"
