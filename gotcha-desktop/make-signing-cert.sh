#!/usr/bin/env bash
# Create a stable, self-signed code-signing certificate named "Gotcha Dev" in the login
# keychain — run ONCE per build machine. build-dmg.sh signs Gotcha.app + the embedded
# mac-recorder with it so the macOS Screen Recording / Microphone grant PERSISTS across
# rebuilds (ad-hoc signatures change identity every build and lose the grant).
#
# This is free and local — NOT an Apple Developer ID. It does not remove the
# right-click→Open Gatekeeper step (that needs Developer ID + notarization), it only
# stabilises the code identity for TCC.
#
# Idempotent: if a "Gotcha Dev" code-signing identity already exists, it does nothing.
set -euo pipefail

NAME="${GOTCHA_SIGN_ID:-Gotcha Dev}"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"

if security find-identity -p codesigning 2>/dev/null | grep -qF "$NAME"; then
  echo "✓ Code-signing identity \"$NAME\" already exists — nothing to do."
  exit 0
fi

echo "→ Creating self-signed code-signing certificate \"$NAME\"…"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Self-signed cert with the Code Signing extended key usage. macOS codesign requires
# extendedKeyUsage=codeSigning; basicConstraints CA:false keeps it a leaf identity.
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout "$TMP/key.pem" -out "$TMP/cert.pem" \
  -subj "/CN=$NAME" \
  -addext "extendedKeyUsage=codeSigning" \
  -addext "basicConstraints=critical,CA:false" \
  -addext "keyUsage=critical,digitalSignature" >/dev/null 2>&1

# Bundle key + cert into a PKCS#12 so `security import` brings in the private key too.
# `-legacy` (OpenSSL 3.x) forces the older PBE/MAC algorithms Apple's importer accepts;
# without it macOS rejects the file with "MAC verification failed".
LEGACY=""
openssl pkcs12 -help 2>&1 | grep -q -- "-legacy" && LEGACY="-legacy"
openssl pkcs12 -export $LEGACY -inkey "$TMP/key.pem" -in "$TMP/cert.pem" \
  -out "$TMP/gotcha-dev.p12" -name "$NAME" -passout pass:gotcha >/dev/null 2>&1

# Import into the login keychain and let codesign use the key without a GUI prompt.
security import "$TMP/gotcha-dev.p12" -k "$KEYCHAIN" -P gotcha -T /usr/bin/codesign -A
# Allow the codesign tool to read the key non-interactively (no "allow access" popups).
security set-key-partition-list -S apple-tool:,apple: -s -k "" "$KEYCHAIN" >/dev/null 2>&1 || true

if security find-identity -p codesigning 2>/dev/null | grep -qF "$NAME"; then
  echo "✓ Created \"$NAME\". You can now run ./gotcha-desktop/build-dmg.sh."
  echo "  (One-time, after installing the first signed build, clear the old ad-hoc grant:"
  echo "     tccutil reset ScreenCapture com.gotcha.desktop"
  echo "     tccutil reset Microphone   com.gotcha.desktop"
  echo "   then grant Screen Recording + Microphone once.)"
else
  echo "✗ Certificate import did not register as a code-signing identity." >&2
  echo "  Create it manually: Keychain Access → Certificate Assistant → Create a Certificate" >&2
  echo "  (Name: $NAME, Identity Type: Self-Signed Root, Certificate Type: Code Signing)." >&2
  exit 1
fi
