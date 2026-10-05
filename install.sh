#!/bin/sh
# Install the built app for the current user and add it to the application menu.
set -e
cd "$(dirname "$0")"

[ -x dist/filefinder/filefinder ] || { echo "Run ./build.sh first"; exit 1; }

target="$HOME/.local/opt/filefinder"
mkdir -p "$HOME/.local/opt" "$HOME/.local/share/applications" \
    "$HOME/.local/share/icons/hicolor/256x256/apps"
rm -rf "$target"
cp -r dist/filefinder "$target"
cp packaging/icons/filefinder-256.png "$HOME/.local/share/icons/hicolor/256x256/apps/filefinder.png"

cat > "$HOME/.local/share/applications/filefinder.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=File Finder
Exec=$target/filefinder
Icon=filefinder
Categories=Utility;
DESKTOP

echo "Installed. Search for File Finder in the application menu."
