#!/usr/bin/env bash
# Build Dota2-Workshop-Tools-x86_64.AppImage into the current folder.
#
#   pip install python-appimage && packaging/appimage/build.sh
#
# python-appimage makes the AppDir: a relocatable Python 3.12 (manylinux_2_28),
# the requirements in requirements.txt, this package, the desktop entry and
# icon. The parts of Qt the windows never load (QML and Quick, Designer,
# translations, the Qt tools) are pruned, and appimagetool packs the rest.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
out="$PWD"
name="Dota2-Workshop-Tools${VERSION:+-$VERSION}-x86_64.AppImage"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cd "$work"

PYTHONPATH="$repo" python-appimage build app --no-packaging -p 3.12 -l manylinux_2_28_x86_64 "$here"
mv ./*-x86_64 AppDir

site=(AppDir/opt/python3.*/lib/python3.*/site-packages)
qt="$site/PySide6"
rm -rf "$qt"/Qt/qml "$qt"/Qt/translations "$qt"/Qt/metatypes "$qt"/Qt/libexec \
       "$qt"/assistant "$qt"/designer "$qt"/linguist "$qt"/lrelease "$qt"/lupdate "$qt"/qml* "$qt"/*.pyi \
       "$qt"/Qt/lib/libQt6Quick* "$qt"/Qt/lib/libQt6Qml* "$qt"/Qt/lib/libQt6Designer* \
       "$qt"/Qt/lib/libQt6LabsStyleKit* "$qt"/Qt/lib/libQt6WaylandCompositor* \
       "$qt"/QtQuick* "$qt"/QtQml* "$qt"/QtDesigner* \
       "$site"/pip "$site"/numpy/*/tests "$site"/numpy/tests
find AppDir -name __pycache__ -prune -exec rm -rf {} +

tool="${APPIMAGETOOL:-}"
if [ -z "$tool" ]; then
  tool="$work/appimagetool"
  curl -fsSL -o "$tool" https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
  chmod +x "$tool"
fi
ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 "$tool" --no-appstream AppDir "$out/$name"
echo "$out/$name"
