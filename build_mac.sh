#!/bin/bash
# Construit « Kacou Audio.app » puis « dist/KacouAudio-mac.dmg »
set -euo pipefail
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
if [ ! -d .venv-build ]; then
  "$PY" -m venv .venv-build
fi
.venv-build/bin/pip install --quiet --upgrade pip pyinstaller certifi

rm -rf build "dist/Kacou Audio" "dist/Kacou Audio.app" dist/KacouAudio-mac.dmg
.venv-build/bin/pyinstaller --noconfirm --clean kacou_audio.spec

# Image disque : l'application + un raccourci vers « Applications »
STAGE=$(mktemp -d)
cp -R "dist/Kacou Audio.app" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "Kacou Audio" -srcfolder "$STAGE" -ov -format UDZO dist/KacouAudio-mac.dmg
rm -rf "$STAGE"

echo
echo "✓ Terminé : dist/KacouAudio-mac.dmg"
