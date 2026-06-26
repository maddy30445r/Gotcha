#!/usr/bin/env bash
# clean-deeplink.sh — flush stale gotcha:// deep-link registrations on macOS.
#
# Why: macOS Launch Services keeps a gotcha:// handler registration for EVERY copy of
# Gotcha.app it has ever seen — build artifacts under target/, plus previously-mounted
# DMG volumes. Deleting the app from /Applications does NOT remove those, so clicking a
# gotcha:// link can "ghost-launch" a leftover build. Run this after a rebuild (or anytime
# the wrong/old app keeps opening) to remove the artifacts and prune the registrations.
#
# Usage:  ./clean-deeplink.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE="$HERE/src-tauri/target/release/bundle"
APP="$BUNDLE/macos/Gotcha.app"
DMG="$BUNDLE/dmg/Gotcha.dmg"
LSREG="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"

echo "→ quitting any running Gotcha"
osascript -e 'quit app "Gotcha"' 2>/dev/null || true
pkill -f gotcha-desktop 2>/dev/null || true

echo "→ ejecting mounted Gotcha DMG volumes (named only)"
for vol in /Volumes/Gotcha*; do
  [ -d "$vol" ] || continue
  hdiutil detach "$vol" -quiet 2>/dev/null || diskutil unmount "$vol" 2>/dev/null || true
  echo "   ejected $vol"
done

echo "→ removing leftover build artifacts"
rm -rf "$APP"; echo "   rm $APP"
rm -f  "$DMG"; echo "   rm $DMG"

echo "→ unregistering every Gotcha.app path Launch Services still knows about"
# Pull the distinct registered paths from the LS dump and unregister each (twice — a path
# can carry duplicate claim ids). Paths may contain spaces, so read line-by-line.
"$LSREG" -dump 2>/dev/null | sed -n 's/^[[:space:]]*path:[[:space:]]*\(.*Gotcha\.app\)$/\1/p' \
  | sort -u | while IFS= read -r p; do
      "$LSREG" -u "$p" 2>/dev/null || true
      "$LSREG" -u "$p" 2>/dev/null || true
      echo "   unregistered $p"
    done

echo "→ rebuilding the Launch Services database"
"$LSREG" -r -domain local -domain user 2>/dev/null || true

remaining="$("$LSREG" -dump 2>/dev/null | grep -ic 'claimed schemes:.*gotcha' || true)"
echo "✓ done — gotcha:// scheme claims remaining: ${remaining:-0}"
if [ "${remaining:-0}" != "0" ]; then
  echo "  (any remaining entries point at non-existent paths and cannot launch;"
  echo "   reinstalling Gotcha will re-register the real one.)"
fi
