#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
#
# Build a Debian/Ubuntu .deb for Nadare.
#
#   scripts/build_deb.sh
#
# Produces dist/nadare_<version>_amd64.deb from the PyInstaller onedir bundle.
# The Python runtime and PyQt6 are bundled under /opt/nadare; the package
# Depends line lists only the system libraries the bundle still needs.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

PY="${PYTHON:-python3}"
if [[ -x .venv/bin/python ]]; then
    PY=".venv/bin/python"
fi

# dpkg-deb is the normal packer. On non-Debian hosts (Arch, Fedora, ...) it is
# often absent; fall back to assembling the ar archive by hand with tar. Both
# paths produce a standard `debian-binary` + `control.tar.gz` + `data.tar.gz`
# member layout that dpkg/apt understand.
HAVE_DPKG_DEB=1
command -v dpkg-deb >/dev/null 2>&1 || HAVE_DPKG_DEB=0

version="$("$PY" -c 'import nadare; print(nadare.__version__)' 2>/dev/null || true)"
if [[ ! "$version" =~ ^[0-9]+\.[0-9]+(\.[0-9]+)?([A-Za-z0-9.+-]*)$ ]]; then
    echo "[!] cannot read nadare.__version__ with ${PY} (got '${version}'): refusing to build." >&2
    exit 1
fi
arch="$(dpkg --print-architecture 2>/dev/null || uname -m | sed -e 's/x86_64/amd64/' -e 's/aarch64/arm64/')"
pkg="dist/nadare_${version}_${arch}"

echo "[*] Nadare ${version}: building PyInstaller onedir bundle"
"$PY" -m PyInstaller --clean -y packaging/nadare.spec

echo "[*] Staging ${pkg}"
rm -rf "$pkg"
install -d "$pkg/DEBIAN" \
           "$pkg/opt/nadare" \
           "$pkg/usr/bin" \
           "$pkg/usr/share/applications" \
           "$pkg/usr/share/icons/hicolor/256x256/apps" \
           "$pkg/usr/share/icons/hicolor/scalable/apps" \
           "$pkg/usr/share/doc/nadare"

# The onedir bundle: keep the internal layout intact.
cp -a dist/nadare/. "$pkg/opt/nadare/"
chmod 755 "$pkg/opt/nadare/nadare"

ln -sf /opt/nadare/nadare "$pkg/usr/bin/nadare"
cp packaging/nadare.desktop "$pkg/usr/share/applications/nadare.desktop"

# Icon is optional: Nadare ships a generated placeholder (packaging/nadare.svg).
# Drop an owned/licensed PNG at packaging/nadare.png and it takes precedence.
if [[ -f packaging/nadare.png ]]; then
    cp packaging/nadare.png \
       "$pkg/usr/share/icons/hicolor/256x256/apps/nadare.png"
elif [[ -f packaging/nadare.svg ]]; then
    cp packaging/nadare.svg \
       "$pkg/usr/share/icons/hicolor/scalable/apps/nadare.svg"
fi

cat > "$pkg/usr/share/doc/nadare/copyright" <<EOF
Nadare ${version}

Copyright (C) 2026 Nadare contributors.
Licensed under the GNU General Public License v3.0 or later (GPL-3.0-or-later).
Except src/nadare/backends/czn/auth.py, which is GPL-3.0-only.

The complete license text is in /opt/nadare/LICENSE after installation and in
the source repository.
EOF
cp LICENSE "$pkg/opt/nadare/LICENSE"

cat > "$pkg/DEBIAN/control" <<EOF
Package: nadare
Version: ${version}
Section: games
Priority: optional
Architecture: ${arch}
Maintainer: Nadare contributors <nadare@example.invalid>
Depends: libc6 (>= 2.35), libgl1, libegl1, libxkbcommon0, libdbus-1-3, libfontconfig1, libxcb-cursor0
Recommends: umu-launcher, mingw-w64, wine
Homepage: https://example.invalid/nadare
Description: open-source Linux launcher for anime gacha games
 Nadare is a profile-based desktop launcher for anime gacha games. It currently
 ships first-class backends for Chaos Zero Nightmare (native STOVE login under
 Proton) and Fate/Grand Order NA (via lepton-fgo), plus generic Wine, Steam,
 script and Waydroid backends.
 .
 This package bundles Python and PyQt6; the host must provide the graphics,
 audio and (for Chaos Zero Nightmare) Wine/umu runtime stack.
EOF

cat > "$pkg/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1 && [ -d /usr/share/icons/hicolor ]; then
    gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
fi
exit 0
EOF
chmod 755 "$pkg/DEBIAN/postinst"

cat > "$pkg/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
# Do not delete the user's data on upgrade/removal; it lives under XDG dirs.
exit 0
EOF
chmod 755 "$pkg/DEBIAN/prerm"

echo "[*] Building the package"
if (( HAVE_DPKG_DEB )); then
    dpkg-deb --build --root-owner-group "$pkg" "${pkg}.deb"
else
    echo "    (dpkg-deb not found; assembling the .deb with ar + tar)"
    deb="$root/${pkg}.deb"
    tmpdeb="$(mktemp -d)"
    trap 'rm -rf "$tmpdeb"' EXIT
    printf '2.0\n' > "$tmpdeb/debian-binary"
    tar -C "$pkg/DEBIAN" --format=gnu --owner=0 --group=0 --numeric-owner \
        -czf "$tmpdeb/control.tar.gz" .
    tar -C "$pkg" --exclude=./DEBIAN --format=gnu --owner=0 --group=0 \
        --numeric-owner -czf "$tmpdeb/data.tar.gz" .
    rm -f "$deb"
    ( cd "$tmpdeb" && ar rc "$deb" debian-binary control.tar.gz data.tar.gz )
    # ar may leave a symbol table; .deb readers ignore it.
    trap - EXIT
    rm -rf "$tmpdeb"
fi
echo "[+] Built: ${pkg}.deb"
echo "    sha256 $(sha256sum "${pkg}.deb" | cut -d' ' -f1)"
