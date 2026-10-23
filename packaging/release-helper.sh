#!/bin/sh
# Build the release SquawkPTT.app: signed with the maintainer's Developer ID,
# notarized by Apple, ticket stapled, zipped for upload as a GitHub release
# asset. The Homebrew formula installs this prebuilt app instead of compiling
# one, which is what lets macOS keep the Microphone / Accessibility / Input
# Monitoring grants across upgrades (see helper/build.sh).
#
# Usage: packaging/release-helper.sh <version>        e.g. 0.1.5
# Env:   SQUAWK_SIGN_IDENTITY  (default: the first "Developer ID Application" identity)
#        SQUAWK_NOTARY_PROFILE (default: squawk-notary — `xcrun notarytool store-credentials`)
# Output: dist/SquawkPTT-<version>.zip and its sha256.
set -eu

VERSION="${1:?usage: release-helper.sh <version>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$ROOT/dist"
APP="$DIST/SquawkPTT.app"
ZIP="$DIST/SquawkPTT-$VERSION.zip"
PROFILE="${SQUAWK_NOTARY_PROFILE:-squawk-notary}"

# The helper's self-update compares this bundle version with the installed
# one, so a release whose Info.plist was not bumped would never be picked up.
PLIST_VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$ROOT/helper/Info.plist")"
if [ "$PLIST_VERSION" != "$VERSION" ]; then
    echo "release-helper: helper/Info.plist CFBundleVersion is $PLIST_VERSION, not $VERSION — bump it first" >&2
    exit 1
fi

IDENTITY="${SQUAWK_SIGN_IDENTITY:-$(security find-identity -p codesigning -v \
    | sed -n 's/.*"\(Developer ID Application: [^"]*\)".*/\1/p' | head -1)}"
if [ -z "$IDENTITY" ]; then
    echo "release-helper: no Developer ID Application identity in the keychain" >&2
    exit 1
fi

rm -rf "$DIST" && mkdir -p "$DIST"
SQUAWK_SIGN_IDENTITY="$IDENTITY" /bin/sh "$ROOT/helper/build.sh" "$APP"
codesign --verify --strict --deep "$APP"

# notarytool takes a zip; the stapled app is then re-zipped for distribution so
# the ticket travels with it and Gatekeeper never needs to go online.
ditto -c -k --keepParent "$APP" "$ZIP"
xcrun notarytool submit "$ZIP" --keychain-profile "$PROFILE" --wait
xcrun stapler staple "$APP"
xcrun stapler validate "$APP"
spctl --assess --type execute --verbose "$APP"
rm -f "$ZIP"
ditto -c -k --keepParent "$APP" "$ZIP"

echo
echo "Release asset: $ZIP"
echo "sha256: $(shasum -a 256 "$ZIP" | cut -d' ' -f1)"
