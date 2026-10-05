#!/bin/sh
# Build a standalone copy of File Finder. Needs python3-venv on this machine once.
# The result in dist/filefinder/ runs on any similar Linux with no Python and no pip.
set -e
cd "$(dirname "$0")"

python3 -m venv .buildenv
.buildenv/bin/pip install --quiet -r requirements.txt pyinstaller
.buildenv/bin/pyinstaller --noconfirm --name filefinder \
    --add-data "filefinder/data:filefinder/data" \
    run.py

echo "Built: dist/filefinder/filefinder"
