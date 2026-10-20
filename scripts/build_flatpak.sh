#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
#
# Build a single-file Flatpak bundle from the PyInstaller onedir bundle.
#
#   scripts/build_appimage.sh      # first: produces dist/nadare/ (Ubuntu 22.04 box)
#   scripts/build_flatpak.sh       # then:  dist/Nadare-<version>.flatpak
#
# Needs: flatpak, rsvg-convert (librsvg), org.flatpak.Builder (flatpak install flathub org.flatpak.Builder),
# org.freedesktop.Sdk//25.08. Everything is built and exported locally; nothing
# is uploaded.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
app_id="io.github.nadare_launcher.Nadare"
manifest_dir="packaging/flatpak"
bundle_src="${BUNDLE_DIR:-dist/nadare}"
version="$(grep -m1 '^__version__' src/nadare/__init__.py | cut -d'"' -f2)"
[[ -n "$version" ]] || { echo "[!] cannot read __version__ from src/nadare/__init__.py" >&2; exit 1; }
work="${WORK_DIR:-build/flatpak}"

[[ -x "$bundle_src/nadare" ]] || { echo "no PyInstaller bundle at $bundle_src (run build_appimage.sh)" >&2; exit 1; }

staging="$manifest_dir/staging"
rm -rf "$staging"
mkdir -p "$staging"
cp -a "$bundle_src" "$staging/bundle"
cp packaging/nadare.desktop "$staging/"
cp LICENSE "$staging/LICENSE"
# Raster icons only: flatpak's icon validator rejects the SVG on hosts whose
# gdk-pixbuf has no SVG loader ("Format not recognized").
for size in 128 256 512; do
    rsvg-convert -w "$size" -h "$size" packaging/nadare.svg -o "$staging/nadare-$size.png"
done
cp "$manifest_dir/$app_id.metainfo.xml" "$staging/"

mkdir -p "$work" dist
flatpak run --env=FLATPAK_USER_DIR="$HOME/.local/share/flatpak" --command=flatpak-builder org.flatpak.Builder \
    --user --install-deps-from=flathub --force-clean --disable-rofiles-fuse \
    --repo="$work/repo" --state-dir="$work/state" \
    "$work/build" "$manifest_dir/$app_id.yml"
flatpak build-bundle --runtime-repo=https://dl.flathub.org/repo/flathub.flatpakrepo \
    "$work/repo" "dist/Nadare-$version.flatpak" "$app_id"
rm -rf "$staging"
echo "[+] Built dist/Nadare-$version.flatpak"
echo "    install: flatpak install --user dist/Nadare-$version.flatpak"
