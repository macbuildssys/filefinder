#!/bin/sh
# Run inside a clean Debian family container. Installs the .deb the way a person
# would, then checks that File Finder starts. Usage: smoke-test.sh /path/to/file.deb
set -e
deb="$1"
export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
# fonts-dejavu-core is only here because a bare container has no fonts at all.
apt-get install -y -qq "$deb" fonts-dejavu-core

command -v filefinder

# Every library the window system plugin needs must be found.
plugin="$(find /opt/filefinder -name libqxcb.so | head -n 1)"
if [ -n "$plugin" ]; then
    missing="$(LD_LIBRARY_PATH=/opt/filefinder/_internal:/opt/filefinder/_internal/PySide6/Qt/lib \
        ldd "$plugin" | grep 'not found' || true)"
    if [ -n "$missing" ]; then
        echo "Missing libraries:"
        echo "$missing"
        exit 1
    fi
fi

# First run opens the folder chooser and waits for a person, so a healthy start
# is one that is still running when the timer stops it (exit code 124).
export QT_QPA_PLATFORM=offscreen
export HOME=/tmp/ffhome
mkdir -p "$HOME"
set +e
timeout 15 filefinder
code=$?
set -e

if [ "$code" -eq 124 ]; then
    echo "OK: File Finder started and kept running"
    exit 0
fi
echo "File Finder stopped early with exit code $code"
exit 1

