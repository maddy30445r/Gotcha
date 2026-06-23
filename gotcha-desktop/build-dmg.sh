#!/usr/bin/env bash
# Build the Gotcha .dmg for local/alpha distribution (self-signed, stable identity).
#
# Why a STABLE signing identity (not ad-hoc): macOS attributes the helper's screen
# capture to Gotcha.app and keys the TCC (Screen Recording / Microphone) grant to the
# app's code identity. Ad-hoc signatures (`codesign -s -`) have NO stable identity —
# the code hash changes on every rebuild — so a prior grant stops matching the new
# binary and the user must remove + re-add the permission on each reinstall. Signing
# with a stable self-signed certificate ("Gotcha Dev") gives a constant identity, so
# the grant persists across rebuilds.
#
# We sign WITHOUT the hardened runtime on purpose: hardened runtime would require
# entitlements (com.apple.security.device.audio-input) we don't need for a
# non-notarized local build, and getting them wrong silently breaks the mic.
#
# ── One-time setup: create the "Gotcha Dev" certificate ────────────────────────────
#   Easiest:  run  ./gotcha-desktop/make-signing-cert.sh   (scripts the steps below)
#   Manual:   Keychain Access → Certificate Assistant → Create a Certificate…
#             Name: Gotcha Dev   Identity Type: Self-Signed Root
#             Certificate Type: Code Signing → Create.
# Override the identity name with GOTCHA_SIGN_ID=... if you named it differently.
# If no matching identity is found this script FALLS BACK to ad-hoc (`-`) and warns —
# the build still works, but the permission grant won't persist across rebuilds.
#
# ── One-time migration off old ad-hoc builds ───────────────────────────────────────
# Old ad-hoc installs left a stale TCC entry that won't match the new identity. Once,
# after installing the first self-signed build, run (or remove the entries in Settings):
#   tccutil reset ScreenCapture com.gotcha.desktop
#   tccutil reset Microphone   com.gotcha.desktop
# then grant Screen Recording + Microphone once. After that, rebuilds keep the grant.
#
# (The real distribution fix is Developer ID signing + notarization — a $99/yr Apple
# account — which also removes the right-click→Open step. This is the free local path.)
#
# Prereqs: Rust on PATH (`. "$HOME/.cargo/env"`), Xcode/Swift, Node.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Stable signing identity (self-signed). Falls back to ad-hoc if it isn't in the keychain.
# NB: no `-v` (valid-only) — a self-signed cert is untrusted by Gatekeeper, so it's not
# "valid", but codesign signs with it fine and that's all we need for a stable identity.
SIGN_ID="${GOTCHA_SIGN_ID:-Gotcha Dev}"
if security find-identity -p codesigning 2>/dev/null | grep -qF "$SIGN_ID"; then
  echo "→ Signing identity: \"$SIGN_ID\" (stable — grant persists across rebuilds)"
else
  echo "⚠️  Signing identity \"$SIGN_ID\" not found in the keychain — falling back to ad-hoc (-)."
  echo "    Run ./gotcha-desktop/make-signing-cert.sh once so permissions persist across rebuilds."
  SIGN_ID="-"
fi

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

echo "→ Signing (deep, no hardened runtime) with \"$SIGN_ID\"…"
# Sign inside-out: the helper first, then the app, so nested code is sealed.
codesign --force -s "$SIGN_ID" "$APP/Contents/MacOS/mac-recorder"
codesign --force --deep -s "$SIGN_ID" "$APP"
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
