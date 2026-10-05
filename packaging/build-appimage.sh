#!/bin/sh
# Make one file, dist/File_Finder-x86_64.AppImage, that runs with no install, no Python
# and no pip. Needs curl once, to fetch the AppImage packer into tools/.
set -e
cd "$(dirname "$0")/.."

[ -x dist/filefinder/filefinder ] || ./build.sh

work="build/appimage"
rm -rf "$work"
mkdir -p "$work/AppDir/usr/lib"
cp -r dist/filefinder "$work/AppDir/usr/lib/filefinder"
cp packaging/filefinder.desktop "$work/AppDir/"
cp packaging/icons/filefinder-256.png "$work/AppDir/filefinder.png"
cp packaging/icons/filefinder-256.png "$work/AppDir/.DirIcon"
mkdir -p "$work/AppDir/usr/share/icons/hicolor/256x256/apps"
cp packaging/icons/filefinder-256.png "$work/AppDir/usr/share/icons/hicolor/256x256/apps/filefinder.png"

cat > "$work/AppDir/AppRun" <<'RUN'
#!/bin/sh
here="$(dirname "$(readlink -f "$0")")"
exec "$here/usr/lib/filefinder/filefinder" "$@"
RUN
chmod +x "$work/AppDir/AppRun"

tool="tools/appimagetool"
if [ ! -x "$tool" ]; then
    mkdir -p tools
    curl -L --fail -o "$tool" \
        https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
    chmod +x "$tool"
fi

ARCH=x86_64 "$tool" --appimage-extract-and-run "$work/AppDir" dist/File_Finder-x86_64.AppImage
echo "Built: dist/File_Finder-x86_64.AppImage"
