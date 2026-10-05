#!/bin/sh
# Make dist/filefinder_<version>_<arch>.deb, which installs with: sudo apt install ./that.deb
set -e
cd "$(dirname "$0")/.."

[ -x dist/filefinder/filefinder ] || ./build.sh

version="$(cat VERSION)"
arch="$(dpkg --print-architecture)"
root="build/deb/filefinder_${version}_${arch}"

rm -rf build/deb
mkdir -p "$root/DEBIAN" "$root/opt" "$root/usr/bin" "$root/usr/share/applications"
cp -r dist/filefinder "$root/opt/filefinder"
ln -s /opt/filefinder/filefinder "$root/usr/bin/filefinder"
cp packaging/filefinder.desktop "$root/usr/share/applications/filefinder.desktop"
for n in 16 24 32 48 64 128 256 512; do
    mkdir -p "$root/usr/share/icons/hicolor/${n}x${n}/apps"
    cp "packaging/icons/filefinder-$n.png" "$root/usr/share/icons/hicolor/${n}x${n}/apps/filefinder.png"
done

size="$(du -sk "$root" | cut -f1)"
cat > "$root/DEBIAN/control" <<CONTROL
Package: filefinder
Version: $version
Architecture: $arch
Maintainer: Dev <dev@localhost>
Installed-Size: $size
Section: utils
Priority: optional
Depends: libc6, libegl1, libgl1, libxkbcommon-x11-0, libxcb-cursor0
Recommends: poppler-utils
Description: Find files and folders on this computer
 Searches by name, folder and the text inside your own files. Forgives typos
 and learns from what you open. Everything stays on this computer.
CONTROL

find "$root" -type d -exec chmod 755 {} +
dpkg-deb --root-owner-group --build "$root" "dist/filefinder_${version}_${arch}.deb"
echo "Built: dist/filefinder_${version}_${arch}.deb"
