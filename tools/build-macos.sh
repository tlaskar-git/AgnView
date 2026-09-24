#!/usr/bin/env bash
# Builds the AgnView desktop app for macOS.
#
#   tools/build-macos.sh app     builds dist/AgnView.app
#   tools/build-macos.sh dmg     packs dist/AgnView.app into dist/AgnView-macos.dmg
#   tools/build-macos.sh         both
#
# With MACOS_SIGNING_IDENTITY set (a "Developer ID Application: ..." identity
# in an unlocked keychain), the app and the disk image are signed with the
# hardened runtime. Without it the app is signed ad hoc, which runs on the
# Mac that built it and needs right-click Open on any other Mac.
# Notarisation needs Apple credentials and is done by
# .github/workflows/macos.yml. See docs/MACOS-SIGNING.md.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
VENV="${AGNVIEW_BUILD_VENV:-$ROOT/.venv-desktop-macos}"
BUILD="$ROOT/build/macos"
DIST="$ROOT/dist"
APP="$DIST/AgnView.app"
DMG="$DIST/AgnView-macos.dmg"
ENTITLEMENTS="$ROOT/tools/macos/entitlements.plist"
STEP="${1:-all}"

cd "$ROOT"

build_app() {
    if [ ! -x "$VENV/bin/python" ]; then
        "$PYTHON" -m venv "$VENV"
    fi
    local py="$VENV/bin/python"
    "$py" -m pip install --quiet --upgrade pip
    "$py" -m pip install --quiet -e ".[desktop]" "pyinstaller==6.22.3" "pillow==12.3.0"

    rm -rf "$BUILD" "$APP" "$DIST/AgnView"
    mkdir -p "$BUILD"

    "$py" tools/macos/make_iconset.py agent_relay/web/static/agnview-app-icon-dark.png "$BUILD/AgnView.iconset"
    iconutil -c icns "$BUILD/AgnView.iconset" -o "$BUILD/AgnView.icns"

    printf 'import sys\nfrom agent_relay.desktop.app import main\nsys.exit(main())\n' > "$BUILD/agnview_desktop.py"
    local version
    version="$("$py" -c "import re;print(re.search(r'^version = \"(.+)\"', open('pyproject.toml').read(), re.M).group(1))")"

    "$py" -m PyInstaller --noconfirm --clean --windowed \
        --name AgnView \
        --icon "$BUILD/AgnView.icns" \
        --osx-bundle-identifier com.agnview.desktop \
        --distpath "$DIST" \
        --workpath "$BUILD/work" \
        --specpath "$BUILD" \
        --collect-data agent_relay \
        --collect-submodules agent_relay \
        --collect-submodules uvicorn \
        --collect-all iroh \
        --collect-all sse_starlette \
        --collect-submodules webview \
        "$BUILD/agnview_desktop.py"

    # The onedir folder beside the bundle is not shipped.
    rm -rf "$DIST/AgnView"

    local plist="$APP/Contents/Info.plist"
    plutil -replace CFBundleName -string "AgnView" "$plist"
    plutil -replace CFBundleDisplayName -string "AgnView" "$plist"
    plutil -replace CFBundleShortVersionString -string "$version" "$plist"
    plutil -replace CFBundleVersion -string "$version" "$plist"
    plutil -replace LSMinimumSystemVersion -string "12.0" "$plist"
    plutil -replace NSHighResolutionCapable -bool YES "$plist"
    # Starts as a menu bar app with no Dock icon. The app shows a Dock icon
    # while its window is open. See docs/adr/ADR-MACOS-DESKTOP.md.
    plutil -replace LSUIElement -bool YES "$plist"
    # The window loads the hub over http on 127.0.0.1.
    plutil -replace NSAppTransportSecurity -json '{"NSAllowsLocalNetworking":true}' "$plist"
    plutil -replace NSLocalNetworkUsageDescription -string \
        "AgnView lets phones on your network reach it when you turn on Allow phones on my network." "$plist"

    sign_app
    echo "Built $APP"
}

sign_app() {
    if [ -z "${MACOS_SIGNING_IDENTITY:-}" ]; then
        # Info.plist changed after PyInstaller signed the bundle, so seal it
        # again. Ad hoc: fine on this Mac, not for distribution.
        codesign --force --deep --sign - "$APP"
        echo "Signed ad hoc. Set MACOS_SIGNING_IDENTITY to sign for distribution."
        return
    fi

    # Every Mach-O file inside the bundle first, then the bundle itself. A
    # bundle signature only seals what is already signed inside it.
    while IFS= read -r -d '' file; do
        if file -b "$file" | grep -q "Mach-O"; then
            codesign --force --timestamp --options runtime --sign "$MACOS_SIGNING_IDENTITY" "$file"
        fi
    done < <(find "$APP/Contents" -type f ! -path "$APP/Contents/MacOS/AgnView" -print0)

    codesign --force --timestamp --options runtime \
        --entitlements "$ENTITLEMENTS" \
        --sign "$MACOS_SIGNING_IDENTITY" "$APP"
    codesign --verify --deep --strict --verbose=2 "$APP"
    echo "Signed $APP with the hardened runtime"
}

build_dmg() {
    [ -d "$APP" ] || { echo "Build the app first: tools/build-macos.sh app" >&2; exit 1; }
    local stage="$BUILD/dmg"
    rm -rf "$stage" "$DMG"
    mkdir -p "$stage"
    cp -R "$APP" "$stage/"
    ln -s /Applications "$stage/Applications"

    # hdiutil sometimes reports the device busy on a fresh runner. It passes
    # on a retry.
    local attempt
    for attempt in 1 2 3 4 5; do
        if hdiutil create -volname "AgnView" -srcfolder "$stage" -ov -format UDZO "$DMG"; then
            break
        fi
        if [ "$attempt" = 5 ]; then
            echo "hdiutil failed five times" >&2
            exit 1
        fi
        sleep 5
    done

    if [ -n "${MACOS_SIGNING_IDENTITY:-}" ]; then
        codesign --force --timestamp --sign "$MACOS_SIGNING_IDENTITY" "$DMG"
        codesign --verify --verbose=2 "$DMG"
    fi
    echo "Packaged $DMG"
}

case "$STEP" in
    app) build_app ;;
    dmg) build_dmg ;;
    all) build_app; build_dmg ;;
    *) echo "Usage: $0 [app|dmg]" >&2; exit 2 ;;
esac
