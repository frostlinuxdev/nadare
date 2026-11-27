#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
#
# Turn a directory staged by stage_repo.py into a signed Flatpak repository plus the files
# users open: index.flatpakrepo (adds the repo), nadare.flatpakref (installs Nadare) and
# nadare.flatpak (a single-file bundle for stores that ignore a .flatpakref's repository, like
# Bazaar on Bazzite, #1410; it carries the repository address and key, so updates still come
# from the repository).
#
#   NADARE_FLATPAK_URL=https://flatpak.example/ NADARE_FLATPAK_KEY=<fingerprint> \
#     packaging/flatpak/build_repo.sh build/flatpak-root build/flatpak-repo
#
# Needs flatpak, appstreamcli (with appstream-compose) and gpg holding the signing key
# (GNUPGHOME may point at it). Runs offline. Only Nadare goes into the repository:
# runtimes and the 32-bit extensions come from Flathub (RuntimeRepo below), and Nadare
# installs missing extensions itself. Re-running against an existing repository adds a
# new commit, so users get it as an update.
set -euo pipefail

staged="${1:?usage: build_repo.sh <staged dir> <repo dir>}"
repo="${2:?usage: build_repo.sh <staged dir> <repo dir>}"
url="${NADARE_FLATPAK_URL:?set NADARE_FLATPAK_URL to the public address of the repository}"
key="${NADARE_FLATPAK_KEY:?set NADARE_FLATPAK_KEY to the signing key fingerprint}"
app_id="io.github.frostlinuxdev.Nadare"
[[ "$url" == */ ]] || url="$url/"
gpg_args=(--gpg-sign="$key")
[[ -n "${GNUPGHOME:-}" ]] && gpg_args+=(--gpg-homedir="$GNUPGHOME")

files="$staged/files"
[[ -f "$staged/finish-argv.json" ]] || { echo "[!] $staged is not staged (run stage_repo.py)" >&2; exit 1; }

# The store catalogue (name, description, screenshots, icons) for software centres.
# appstream 1.1+ writes JPEG XL icons by default, which flatpak never exports: ask for PNG.
# (1.0 has no --image-format and writes PNG anyway.)
compose_args=(--prefix=/ --origin="$app_id" --result-root="$files"
              --data-dir="$files/share/app-info/xmls")
if appstreamcli compose --help 2>/dev/null | grep -q -- '--image-format'; then
    compose_args+=(--image-format=png)
fi
appstreamcli compose "${compose_args[@]}" "$files"
# appstream 1.x writes icons to swcatalog/; flatpak's export reads app-info/icons/flatpak/.
for size in 64x64 128x128; do
    icon="$files/share/swcatalog/icons/$app_id/$size/$app_id.png"
    if [[ -f "$icon" ]]; then
        install -Dm644 "$icon" "$files/share/app-info/icons/flatpak/$size/$app_id.png"
    fi
done
# Without it the bundle and the repo carry no icon (software centres show a generic one).
[[ -f "$files/share/app-info/icons/flatpak/64x64/$app_id.png" ]] || {
    echo "[!] appstream compose wrote no 64x64 PNG icon for $app_id" >&2; exit 1; }

# build-finish writes the permissions into metadata ([Context]); run it only once.
if ! grep -q '^\[Context\]' "$staged/metadata"; then
    (cd "$(dirname "$staged")" && python3 -c 'import json,subprocess,sys; subprocess.run(json.load(open(sys.argv[1])), check=True)' \
        "$(basename "$staged")/finish-argv.json")
fi

flatpak build-export --disable-sandbox "${gpg_args[@]}" "$repo" "$staged"
flatpak build-update-repo "${gpg_args[@]}" --title="Nadare" \
    --homepage=https://frostlinuxdev.github.io/nadare/ "$repo"

keyfile="$(mktemp)"
trap 'rm -f "$keyfile"' EXIT
gpg --export "$key" > "$keyfile"
[[ -s "$keyfile" ]] || { echo "[!] gpg could not export key $key" >&2; exit 1; }
key_b64="$(base64 -w0 < "$keyfile")"
cat > "$repo/index.flatpakrepo" <<EOF
[Flatpak Repo]
Title=Nadare
Url=$url
Homepage=https://frostlinuxdev.github.io/nadare/
Comment=Open-source Linux launcher for anime games
Icon=https://raw.githubusercontent.com/frostlinuxdev/nadare/main/packaging/nadare.svg
GPGKey=$key_b64
EOF
cat > "$repo/nadare.flatpakref" <<EOF
[Flatpak Ref]
Name=$app_id
Branch=master
Title=Nadare
Url=$url
RuntimeRepo=https://dl.flathub.org/repo/flathub.flatpakrepo
IsRuntime=false
SuggestRemoteName=nadare
GPGKey=$key_b64
EOF
flatpak build-bundle --repo-url="$url" \
    --runtime-repo=https://dl.flathub.org/repo/flathub.flatpakrepo \
    --gpg-keys="$keyfile" "$repo" "$repo/nadare.flatpak" "$app_id" master
flatpak repo --branches "$repo"
echo "[+] $repo ready for $url (index.flatpakrepo, nadare.flatpakref, nadare.flatpak)"
