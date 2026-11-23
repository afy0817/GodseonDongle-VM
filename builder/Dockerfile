# SPDX-FileCopyrightText: 2026 GodSeon
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Builder for the GodseonDongle-VM guest. linux-stable is used instead of the
# upstream linux-lts because rtw89's USB transmit flow control is broken in the
# 6.18 LTS series (bulk OUT stalls during large uploads); stable has the fix.
ARG ALPINE_IMAGE=alpine:edge
FROM ${ALPINE_IMAGE}
# Set by build.sh from the pkg= fields of drivers.conf.
ARG FIRMWARE_PACKAGES="linux-firmware-rtw89"
# linux-stable-dev and a compiler rebuild rtw88 with kernel/'s patches; installed in the same
# step as linux-stable so the two versions always match. zstd reads .tar.zst distfiles for the
# license texts (builder/compliance.py).
RUN apk add --no-progress --quiet python3 kmod linux-stable linux-stable-dev build-base patch \
    zstd wireless-regdb ${FIRMWARE_PACKAGES}
