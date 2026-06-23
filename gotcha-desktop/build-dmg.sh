#!/usr/bin/env bash
# Build the Gotcha .dmg (unsigned, alpha).
#
# Rebuilds the Swift recorder, stages it as the Tauri sidecar for THIS host's
# target triple, then bundles the app + .dmg. The sidecar travels inside
# Gotcha.app (Contents/MacOS/mac-recorder) so it runs with Gotcha's own TCC
# identity. Output: src-tauri/target/release/bundle/dmg/Gotcha_<ver>_<arch>.dmg
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

cd gotcha-desktop
npm install
npm run tauri build

echo "→ Done. .dmg is under src-tauri/target/release/bundle/dmg/"
echo "  (Unsigned: first open is right-click → Open. If the recorder is blocked"
echo "   by Gatekeeper quarantine, run: xattr -dr com.apple.quarantine /Applications/Gotcha.app)"
