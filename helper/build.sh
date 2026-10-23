#!/bin/sh
# Compile and ad-hoc-sign SquawkPTT.app. This is the single build step shared by
# the Homebrew formula, the curl installer, `helper/install.sh`, and
# `squawk install-agent` — so the helper is compiled exactly one way everywhere.
#
# Usage: build.sh <dest-app-path>
#
# See squawkptt.m for why this helper exists (it owns the CGEventTap that drives
# hold-to-talk and holds the mic/Accessibility TCC grants). Building locally is
# deliberate: a locally compiled binary carries no Gatekeeper quarantine, so no
# Apple notarization or Developer account is required.
set -eu

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
APP="${1:?usage: build.sh <dest-app-path>}"

command -v clang >/dev/null 2>&1 || {
    echo "build.sh: clang not found — install Xcode Command Line Tools:" >&2
    echo "  xcode-select --install" >&2
    exit 1
}

mkdir -p "$APP/Contents/MacOS"
cp "$SRC_DIR/Info.plist" "$APP/Contents/Info.plist"
clang -O2 -Wall -fobjc-arc \
    -framework AVFoundation -framework Foundation \
    -framework AppKit -framework ApplicationServices \
    -o "$APP/Contents/MacOS/SquawkPTT" "$SRC_DIR/squawkptt.m"
# Signing. With SQUAWK_SIGN_IDENTITY set (a "Developer ID Application: …"
# identity — what the release script uses), the app gets a real signature with
# the hardened runtime, a secure timestamp and the entitlements notarization
# needs. macOS then keys the Microphone / Accessibility / Input Monitoring
# grants to "identifier sh.squawk.ptt from this team", which every future build
# also satisfies — grants survive upgrades. Without it, an ad-hoc signature
# with a stable identifier; macOS keys ad-hoc grants to the CDHash, so any
# source change re-prompts once.
if [ -n "${SQUAWK_SIGN_IDENTITY:-}" ]; then
    codesign --force --options runtime --timestamp \
        --entitlements "$SRC_DIR/SquawkPTT.entitlements" \
        --sign "$SQUAWK_SIGN_IDENTITY" --identifier sh.squawk.ptt "$APP"
else
    codesign --force --sign - --identifier sh.squawk.ptt "$APP"
fi
echo "Built $APP"
