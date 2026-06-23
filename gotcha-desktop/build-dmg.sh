#!/usr/bin/env bash
# Build the Gotcha .dmg for local/alpha distribution (ad-hoc signed).
#
# Why ad-hoc signing: macOS will NOT reliably grant Screen Recording (or persist
# Microphone) to a bundled helper whose parent app is fully unsigned — capture
# loops on the permission prompt. Ad-hoc signing gives Gotcha.app + the embedded
# mac-recorder a stable code identity so the grant sticks. We sign WITHOUT the
# hardened runtime on purpose: hardened runtime would require entitlements
# (com.apple.security.device.audio-input) we don't need for a non-notarized local
# build, and getting them wrong silently breaks the mic. Plain ad-hoc = capture
# gated purely by TCC, which is what we want here.
#
# (The real distribution fix is Developer ID signing + notarization — a $99/yr
# Apple account — which also removes the right-click→Open step. This script is the
# free local path.)
#
# Prereqs: Rust on PATH (`. "$HOME/.cargo/env"`), Xcode/Swift, Node.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "→ Building the Swift recorder…"
swift build --package-path mac_recorder -c release

TRIPLE="$(rustc -Vv | sed -n 's/host: //p')"
echo "→ Staging sidecar for $TRIPLE"
mkdir -p gotcha-desktop/src-tauri/binaries
cp mac_recorder/.build/release/mac-recorder \
   "gotcha-desktop/src-tauri/binaries/mac-recorder-$TRIPLE"

echo "→ Building the app bundle (no dmg; we sign + package ourselves)…"
( cd gotcha-desktop && npm install && npm run tauri build -- --bundles app )

APP="gotcha-desktop/src-tauri/target/release/bundle/macos/Gotcha.app"
[ -d "$APP" ] || { echo "build failed: $APP not found"; exit 1; }

echo "→ Ad-hoc signing (deep, no hardened runtime)…"
# Sign inside-out: the helper first, then the app, so nested code is sealed.
codesign --force -s - "$APP/Contents/MacOS/mac-recorder"
codesign --force --deep -s - "$APP"
codesign --verify --deep --strict "$APP" && echo "  signature OK"

echo "→ Packaging the .dmg…"
DMG="gotcha-desktop/src-tauri/target/release/bundle/dmg/Gotcha.dmg"
mkdir -p "$(dirname "$DMG")"
STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
rm -f "$DMG"
hdiutil create -volname "Gotcha" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
rm -rf "$STAGE"

echo "→ Done: $DMG"
echo "  Install: open the dmg, drag Gotcha to Applications."
echo "  First open (ad-hoc, not notarized): right-click Gotcha → Open → Open."
echo "  If the recorder is blocked by quarantine: xattr -dr com.apple.quarantine /Applications/Gotcha.app"
