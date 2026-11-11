#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
#
# Turn a directory staged by stage_repo.py into a signed Flatpak repository plus the files
# users open: index.flatpakrepo (adds the repo) and nadare.flatpakref (installs Nadare).
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
appstreamcli compose --prefix=/ --origin="$app_id" --result-root="$files" \
    --data-dir="$files/share/app-info/xmls" "$files"
# appstream 1.x writes icons to swcatalog/; flatpak's export reads app-info/icons/flatpak/.
for size in 64x64 128x128; do
    icon="$files/share/swcatalog/icons/$app_id/$size/$app_id.png"
    if [[ -f "$icon" ]]; then
        install -Dm644 "$icon" "$files/share/app-info/icons/flatpak/$size/$app_id.png"
    fi
done

# build-finish writes the permissions into metadata ([Context]); run it only once.
if ! grep -q '^\[Context\]' "$staged/metadata"; then
    (cd "$(dirname "$staged")" && python3 -c 'import json,subprocess,sys; subprocess.run(json.load(open(sys.argv[1])), check=True)' \
        "$(basename "$staged")/finish-argv.json")
fi

flatpak build-export --disable-sandbox "${gpg_args[@]}" "$repo" "$staged"
flatpak build-update-repo "${gpg_args[@]}" --title="Nadare" \
    --homepage=https://frostlinuxdev.github.io/nadare/ "$repo"

key_b64="$(gpg --export "$key" | base64 -w0)"
[[ -n "$key_b64" ]] || { echo "[!] gpg could not export key $key" >&2; exit 1; }
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
GPGKey=$key_b64
EOF
flatpak repo --branches "$repo"
echo "[+] $repo ready for $url (index.flatpakrepo, nadare.flatpakref)"
