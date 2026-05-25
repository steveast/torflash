#!/bin/bash
set -euo pipefail

# Build TorFlash AppImage
# Prerequisites: built binary at dist/TorFlash

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
APPDIR="$PROJECT_DIR/TorFlash.AppDir"

echo "==> Preparing AppDir..."
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin"
mkdir -p "$APPDIR/usr/share/icons/hicolor/scalable/apps"
mkdir -p "$APPDIR/usr/share/applications"

# Copy binary
cp "$PROJECT_DIR/dist/TorFlash" "$APPDIR/usr/bin/TorFlash"
chmod +x "$APPDIR/usr/bin/TorFlash"

# Copy desktop file and icon
cp "$PROJECT_DIR/torflash.desktop" "$APPDIR/usr/share/applications/"
cp "$PROJECT_DIR/torflash.desktop" "$APPDIR/"
cp "$PROJECT_DIR/assets/torflash.svg" "$APPDIR/usr/share/icons/hicolor/scalable/apps/"
cp "$PROJECT_DIR/assets/torflash.svg" "$APPDIR/torflash.svg"

# Create AppRun
cat > "$APPDIR/AppRun" << 'APPRUN'
#!/bin/bash
SELF="$(readlink -f "$0")"
HERE="${SELF%/*}"
exec "$HERE/usr/bin/TorFlash" "$@"
APPRUN
chmod +x "$APPDIR/AppRun"

# Download appimagetool if not present
TOOL="$PROJECT_DIR/scripts/appimagetool"
if [ ! -f "$TOOL" ]; then
    echo "==> Downloading appimagetool..."
    curl -Lo "$TOOL" "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
    chmod +x "$TOOL"
fi

echo "==> Building AppImage..."
ARCH=x86_64 "$TOOL" "$APPDIR" "$PROJECT_DIR/dist/TorFlash-x86_64.AppImage"

rm -rf "$APPDIR"
echo "==> Done: dist/TorFlash-x86_64.AppImage"
