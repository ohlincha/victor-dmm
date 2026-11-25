#!/bin/sh
# Build an architecture-independent .deb: dist/victor-dmm_<version>_all.deb
# Requires dpkg-deb (and fakeroot when not run as root).
set -eu
cd "$(dirname "$0")/../.."
VERSION=$(python3 -c "import re;print(re.search(r'__version__ = \"(.+?)\"', open('victor_dmm.py').read()).group(1))")
PKG=build/deb/victor-dmm_${VERSION}_all
rm -rf "$PKG"
install -Dm644 victor_dmm.py        "$PKG/usr/lib/victor-dmm/victor_dmm.py"
install -Dm644 victor_dmm_gui.py    "$PKG/usr/lib/victor-dmm/victor_dmm_gui.py"
install -Dm644 70-victor-dmm.rules  "$PKG/usr/lib/udev/rules.d/70-victor-dmm.rules"
install -Dm644 icon.png             "$PKG/usr/lib/victor-dmm/icon.png"
install -Dm644 docs/MANUAL.html     "$PKG/usr/lib/victor-dmm/docs/MANUAL.html"
install -Dm644 docs/VictorDMM-Manual.pdf "$PKG/usr/share/doc/victor-dmm/VictorDMM-Manual.pdf"
install -Dm644 icon.png             "$PKG/usr/share/icons/hicolor/256x256/apps/victor-dmm.png"
install -Dm644 packaging/icon.svg   "$PKG/usr/share/icons/hicolor/scalable/apps/victor-dmm.svg"
install -Dm644 packaging/linux/victor-dmm.desktop "$PKG/usr/share/applications/victor-dmm.desktop"
install -Dm644 LICENSE              "$PKG/usr/share/doc/victor-dmm/copyright"
install -Dm644 README.md            "$PKG/usr/share/doc/victor-dmm/README.md"
mkdir -p "$PKG/usr/bin" "$PKG/DEBIAN"
printf '#!/bin/sh\nexec python3 /usr/lib/victor-dmm/victor_dmm_gui.py "$@"\n' > "$PKG/usr/bin/victor-dmm"
printf '#!/bin/sh\nexec python3 /usr/lib/victor-dmm/victor_dmm.py "$@"\n'     > "$PKG/usr/bin/victor-dmm-cli"
chmod 755 "$PKG/usr/bin/victor-dmm" "$PKG/usr/bin/victor-dmm-cli"
cat > "$PKG/DEBIAN/control" <<CTRL
Package: victor-dmm
Version: $VERSION
Architecture: all
Maintainer: victor-dmm maintainers <noreply@github.com>
Depends: python3 (>= 3.7), python3-tk
Section: electronics
Priority: optional
Homepage: https://github.com/ohlincha/victor-dmm
Description: Real-time display and logging for VICTOR USB multimeters
 Reads VICTOR 70C/86C/86D and similar multimeters through their USB-HID
 cable (USB ID 1244:d237), shows a live reading and plot, and logs to CSV.
 Includes a udev rule so the meter can be used without root.
CTRL
cat > "$PKG/DEBIAN/postinst" <<'POST'
#!/bin/sh
set -e
if [ "$1" = configure ] && command -v udevadm >/dev/null 2>&1; then
    udevadm control --reload || true
    udevadm trigger --subsystem-match=hidraw || true
fi
POST
chmod 755 "$PKG/DEBIAN/postinst"
mkdir -p dist
fakeroot dpkg-deb --build --root-owner-group "$PKG" dist/ 2>/dev/null || dpkg-deb --build --root-owner-group "$PKG" dist/
ls -l dist/*.deb
