#!/bin/bash
# Builds the iOS mic agent and installs it on a connected iPhone. Runs on a Mac
# with Xcode and an "Apple Development" identity in the keychain.
#
#   ios/build.sh                    # build + install on the first available iPhone
#   PROFILE=path.mobileprovision ios/build.sh
#
# Needs a development provisioning profile for $BUNDLE that includes the phone;
# by default it's looked up in the usual profile folders. Optional defaults for
# the app's settings come from ~/.config/voxbutton/ios.json:
#   {"server": "https://your.domain", "token": "..."}
set -euo pipefail
cd "$(dirname "$0")"

BUNDLE="${BUNDLE:-com.brunji.voxbutton}"
MIN_OS=17.0
OUT=build
APP="$OUT/VoxButton.app"
rm -rf "$OUT"
mkdir -p "$APP"

SDK="$(xcrun --sdk iphoneos --show-sdk-path)"
xcrun --sdk iphoneos swiftc -parse-as-library -O \
    -target "arm64-apple-ios$MIN_OS" -sdk "$SDK" \
    -o "$APP/VoxButton" VoxAgent.swift

SDK_VER="$(xcrun --sdk iphoneos --show-sdk-version)"
SDK_BUILD="$(xcrun --sdk iphoneos --show-sdk-build-version)"
cat > "$APP/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key><string>VoxButton</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>CFBundleName</key><string>VoxButton</string>
  <key>CFBundleDisplayName</key><string>VoxButton</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>CFBundleVersion</key><string>$(date +%s)</string>
  <key>CFBundleSupportedPlatforms</key><array><string>iPhoneOS</string></array>
  <key>LSRequiresIPhoneOS</key><true/>
  <key>MinimumOSVersion</key><string>$MIN_OS</string>
  <key>UIDeviceFamily</key><array><integer>1</integer></array>
  <key>UIRequiredDeviceCapabilities</key><array><string>arm64</string></array>
  <key>UILaunchScreen</key><dict/>
  <key>UIBackgroundModes</key><array><string>audio</string></array>
  <key>NSMicrophoneUsageDescription</key><string>VoxButton records your voice when you click the button on your computer.</string>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsArbitraryLoads</key><true/></dict>
  <key>DTPlatformName</key><string>iphoneos</string>
  <key>DTPlatformVersion</key><string>$SDK_VER</string>
  <key>DTSDKName</key><string>iphoneos$SDK_VER</string>
  <key>DTSDKBuild</key><string>$SDK_BUILD</string>
  <key>DTPlatformBuild</key><string>$SDK_BUILD</string>
</dict>
</plist>
EOF
CFG=~/.config/voxbutton/ios.json
if [[ -f $CFG ]]; then
    for pair in server:VBServer token:VBToken; do
        v="$(plutil -extract "${pair%%:*}" raw -o - "$CFG" 2>/dev/null || true)"
        [[ -n $v ]] && /usr/libexec/PlistBuddy -c "Add :${pair#*:} string $v" "$APP/Info.plist"
    done
fi
plutil -lint "$APP/Info.plist" >/dev/null

if [[ -z "${PROFILE:-}" ]]; then
    for f in ~/Library/MobileDevice/Provisioning\ Profiles/*.mobileprovision \
             ~/Library/Developer/Xcode/UserData/Provisioning\ Profiles/*.mobileprovision; do
        [[ -f $f ]] || continue
        P="$(security cms -D -i "$f" 2>/dev/null)" || continue
        ID="$(echo "$P" | plutil -extract Entitlements.application-identifier raw -o - - 2>/dev/null || true)"
        GTA="$(echo "$P" | plutil -extract Entitlements.get-task-allow raw -o - - 2>/dev/null || true)"
        if [[ $ID == *".$BUNDLE" && $GTA == true ]]; then PROFILE="$f"; break; fi
    done
fi
[[ -n "${PROFILE:-}" ]] || { echo "no development profile for $BUNDLE"; exit 1; }
cp "$PROFILE" "$APP/embedded.mobileprovision"
security cms -D -i "$PROFILE" | plutil -extract Entitlements xml1 -o "$OUT/entitlements.plist" -

IDENT="$(security find-identity -v -p codesigning | awk '/Apple Development/ {print $2; exit}')"
codesign --force --timestamp=none --sign "$IDENT" --entitlements "$OUT/entitlements.plist" "$APP"
codesign --verify --strict "$APP"
echo "built $APP"

DEV="${DEVICE_ID:-$(xcrun devicectl list devices 2>/dev/null | awk '/available \(paired\)/ {print $3; exit}')}"
if [[ -z $DEV ]]; then
    echo "no iPhone available (unlock it, same Wi-Fi or USB); app left in $APP"
    exit 1
fi
xcrun devicectl device install app --device "$DEV" "$APP"
