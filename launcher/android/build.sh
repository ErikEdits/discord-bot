#!/usr/bin/env bash
# Builds launcher/android/BotPanel.apk with the plain Android SDK tools (no Gradle).
# Runs in GitHub Actions (.github/workflows/android-apk.yml), where the SDK is installed.
# Signing: the keystore from $ANDROID_KEYSTORE_B64 (+ $ANDROID_KEYSTORE_PASSWORD) if set,
# otherwise a new one for this build (then installing an update needs the old app removed first).
set -euo pipefail
cd "$(dirname "$0")"

SDK="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-}}"
[ -d "$SDK" ] || { echo "Android SDK not found (set ANDROID_HOME)"; exit 1; }
BT="$(ls -d "$SDK"/build-tools/*/ | sort -V | tail -1)"
PLATFORM="$(ls -d "$SDK"/platforms/android-*/ | grep -E 'android-[0-9]+/$' | sort -V | tail -1)"
JAR="${PLATFORM}android.jar"
MIN_SDK=26
TARGET_SDK=34
VERSION_CODE="${VERSION_CODE:-1}"
VERSION_NAME="${VERSION_NAME:-1.0}"
echo "build-tools: $BT"
echo "platform:    $PLATFORM"

rm -rf build && mkdir -p build/classes build/dex
"$BT/aapt2" compile --dir res -o build/res.zip
"$BT/aapt2" link -o build/base.apk -I "$JAR" --manifest AndroidManifest.xml \
    --min-sdk-version "$MIN_SDK" --target-sdk-version "$TARGET_SDK" \
    --version-code "$VERSION_CODE" --version-name "$VERSION_NAME" build/res.zip
javac -encoding UTF-8 -source 8 -target 8 -Xlint:-options -classpath "$JAR" -d build/classes \
    $(find src -name '*.java')
"$BT/d8" --release --min-api "$MIN_SDK" --lib "$JAR" --output build/dex $(find build/classes -name '*.class')
cp build/base.apk build/unsigned.apk
(cd build/dex && zip -q -X ../unsigned.apk classes.dex)
"$BT/zipalign" -f -p 4 build/unsigned.apk build/aligned.apk

PASS="${ANDROID_KEYSTORE_PASSWORD:-botpanel-build}"
if [ -n "${ANDROID_KEYSTORE_B64:-}" ]; then
    echo "$ANDROID_KEYSTORE_B64" | base64 -d > build/release.jks
else
    keytool -genkeypair -keystore build/release.jks -storepass "$PASS" -keypass "$PASS" -alias botpanel \
        -keyalg RSA -keysize 2048 -validity 10000 -dname "CN=Bot Panel" >/dev/null 2>&1
fi
ALIAS="$(keytool -list -keystore build/release.jks -storepass "$PASS" | awk -F, '/PrivateKeyEntry/{print $1; exit}')"
"$BT/apksigner" sign --ks build/release.jks --ks-pass "pass:$PASS" --key-pass "pass:$PASS" \
    --ks-key-alias "$ALIAS" --out BotPanel.apk build/aligned.apk
"$BT/apksigner" verify --verbose BotPanel.apk
ls -l BotPanel.apk
