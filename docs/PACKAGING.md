# Packaging and release channels

One version string, everywhere: `0.1.0bN` from `pyproject.toml` (mirrored in
`src/nadare/__init__.py`). Git tag `v<version>`, AppImage
`Nadare-<version>-x86_64.AppImage`, checksums in `SHA256SUMS` next to it. See
[CONTRIBUTING.md](../CONTRIBUTING.md#release-artifacts-and-versioning).

## Channels

| Channel | Artifact | Built by | Updates owned by |
| --- | --- | --- | --- |
| AppImage | `Nadare-<version>-x86_64.AppImage` (signed) + `.zsync` + `SHA256SUMS` + detached `.asc` signatures | `scripts/build_appimage.sh`, signed with `scripts/sign_appimage.py`, uploaded to the GitHub release | Nadare's in-app update check; AppImageUpdate-based tools read the embedded update information |
| Flatpak | local `.flatpak` bundle (not published) | `scripts/build_flatpak.sh` + `packaging/flatpak/` | manual reinstall; Flatpak owns the sandbox update |
| AUR `nadare-bin` | the release AppImage, unpacked into `/opt/nadare` | `packaging/aur/nadare-bin/PKGBUILD` | pacman |
| `.deb` | `dist/nadare_<version>_<arch>.deb` (built, not published) | `scripts/build_deb.sh` | dpkg/apt |
| Source | this checkout | `docs/SOURCE-INSTALL.md` | the user's own `git pull` |

## AppImage signing and update information

From 0.1.0b22 on, release AppImages are signed with the Nadare release key:

- fingerprint `0F53 6F24 4C48 1212 A309  5B52 1E05 855E 99D9 18D5` (Ed25519, sign only), public key in
  [`packaging/signing/nadare-release-signing.asc`](../packaging/signing/nadare-release-signing.asc);
- the private key is kept on one offline-backed-up signing machine, never in CI or the build
  container. It is a different key from the Flatpak repository key.

Each release carries:

| File | What |
| --- | --- |
| `Nadare-<version>-x86_64.AppImage` | the AppImage, with an embedded signature (`.sha256_sig`, `.sig_key`) and update information (`.upd_info`: `gh-releases-zsync\|frostlinuxdev\|nadare\|latest\|Nadare-*-x86_64.AppImage.zsync`) |
| `Nadare-<version>-x86_64.AppImage.zsync` | block checksums, so an updater downloads only the changed parts |
| `SHA256SUMS` | sha256 of the AppImage and the `.zsync` |
| `*.asc` | detached signatures of the AppImage and of `SHA256SUMS` |

How a release is made: `scripts/build_appimage.sh` runs with `NADARE_UPDATE_INFO` set and
without any key. The unsigned image goes through the distro tests. It is then signed on the
signing machine with `scripts/sign_appimage.py sign`, which changes only the two signature
sections; `sign_appimage.py same-content UNSIGNED SIGNED` proves that, so the tested image is
the shipped image. The `.zsync`, `SHA256SUMS` and the detached signatures are made from the signed
file. The embedded signature follows `appimagetool --sign`: a text-mode detached signature of the
hex SHA-256 of the file with both signature sections read as zeros, which is what AppImageUpdate
checks.

To check a download yourself:

```sh
gpg --import nadare-release-signing.asc        # once
gpg --verify Nadare-<version>-x86_64.AppImage.asc Nadare-<version>-x86_64.AppImage
python3 scripts/sign_appimage.py verify Nadare-<version>-x86_64.AppImage \
    --fingerprint 0F536F244C481212A3095B521E05855E99D918D5
```

There is no published RPM recipe in this tree. Anything that maps the version
for RPM uses the `0.1.0~bN` spelling (hyphens are illegal in `Version`).

## Distro packages disable the update check

A package manager owns updates for `nadare-bin`, Flatpak and any distro build.
Those wrappers export `NADARE_MANAGED_UPDATES=1` (see
`packaging/aur/nadare-bin/PKGBUILD` and
`packaging/flatpak/io.github.frostlinuxdev.Nadare.yml`); `core/updatecheck.py`
then skips the release check. A new distro recipe must do the same, or Nadare
will offer a manual AppImage download that the package manager would overwrite.

## What each build must contain

- The PyInstaller onedir bundle, including `src/nadare/assets/7zzs` (run
  `scripts/fetch_7zip.sh` first; the spec embeds whatever is in `assets/`). That script builds
  7-Zip from its pinned source **without the RAR code** (unRAR's licence is non-free) and needs
  `make` and `g++`; the build is reproducible and `core/sevenzip.py` pins the result.
- `LICENSE` under the package's licence directory.
- The desktop file, icon and (Flatpak/AppImage) the AppStream metainfo.
- The `.deb` derives its `libc6 (>= ...)` floor from `readelf -V` on the bundle
  instead of assuming the build host's glibc.

## Maintainer timers

`packaging/systemd/nadare-index-refresh.{service,timer}` and
`nadare-pin-publisher.{service,timer}` are maintainer jobs, not part of any package. They
are installed by hand, as **system** units that run as the checkout owner (`User=` in the
unit), and they read their configuration from an environment file outside the repository
(mode 600). A `systemctl --user` unit cannot read such a file, so they are not user units.
The unit files in the repo use `%h`, which in a system unit is root's home: set `User=` and
the paths to the checkout owner's before installing. See [ARCHIVE-PINS.md](ARCHIVE-PINS.md)
for the pin job.

The refresh script is a source-checkout job (it imports `scripts/*.py` and
`src/nadare`), which is why it is not part of the regular AppImage install.

## Release checks

- CI (`ci.yml`) runs the unit suite on every push to `master`/`main` and on every pull request. Weekly (or by hand) it also rebuilds
  both committed CZN PCSDK stubs (the Go default and the C fallback) in pinned containers
  and compares them with the committed binaries, and audits the lock with `pip-audit`.
  Those two jobs need a container engine or the network and have not run in CI yet.
- In-place updates: after building the release AppImage, record its hash in the index with
  `python scripts/release_hash.py <nadare-index checkout> <version> dist/Nadare-<version>-x86_64.AppImage`
  and push the index (`app/releases.json`). Nadare checks the downloaded file against that hash
  and against the release's `SHA256SUMS`; without the index entry it falls back to
  `SHA256SUMS` alone.
- Before publishing, the AppImage must pass `scripts/test_appimage.sh` in a
  throwaway distrobox and `appstreamcli validate` on the metainfo.
- Third-party licence texts are written by the AppImage build to
  `usr/share/licenses/nadare/third-party/THIRD-PARTY-NOTICES.txt`, together with each
  bundled package's licence texts (Python wheels from their metadata, system libraries
  from the build distro's `/usr/share/doc/<pkg>/copyright` found via `dpkg -S`, and the
  bundled 7-Zip and Inter font texts from `src/nadare/assets/`). The
  notice also points at the Qt LGPL source. The Flatpak build is deferred and does not
  do this yet (#587).

## Our own Flatpak repository

Until Nadare is on Flathub, a signed repository of our own carries it (`packaging/flatpak/`):

```sh
scripts/build_appimage.sh                     # produces AppDir/
python3 packaging/flatpak/stage_repo.py --appdir AppDir --out build/flatpak-root
NADARE_FLATPAK_URL=https://<repo address>/ NADARE_FLATPAK_KEY=<key fingerprint> \
  packaging/flatpak/build_repo.sh build/flatpak-root build/flatpak-repo
```

The stage step also downloads the pinned Debian packages from `packaging/flatpak/proton-libs.json`
into `build/flatpak-debs` (override with `--deb-cache`), verifies their SHA-256, and unpacks the
Steam Runtime libraries Proton needs but the Freedesktop runtime lacks into
`files/lib/nadare-proton/{x86_64,i386}`. Only Proton is pointed at them via `LD_LIBRARY_PATH`;
Nadare keeps using the runtime's copies. `--no-proton-libs` skips this, and unlike the rest of the
step it needs the network on the first run.

`build_repo.sh` needs `flatpak`, `appstreamcli` (with appstream-compose) and the signing key in gpg; it
runs offline and writes `index.flatpakrepo` and `nadare.flatpakref` next to the repository. Re-running
against the same directory adds a commit, which users receive as an update.

Publish with `packaging/flatpak/upload.sh build/flatpak-repo` (`RCLONE` set to an rclone command that
knows the `r2:` remote). The repository is served at `https://flatpak.nadare.app/` from a Cloudflare R2
bucket; that address must never change. The upload puts objects first and the summary last, sets
`no-cache` on the files that change, and never deletes anything.

The signing key and the bucket credentials are kept off this repository and off CI.

Only Nadare goes into the repository (about 72 MB per release). The runtime comes from Flathub through
`RuntimeRepo`; the 32-bit `Compat.i386` and `GL32.*` extensions (and NVIDIA's per-driver packages) also come
from Flathub, and Nadare offers to install any that are missing (`core/flatpakgl.py`, banner and `doctor`).
The app id must never change: user data lives in `~/.var/app/io.github.frostlinuxdev.Nadare`.
