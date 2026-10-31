# Packaging and release channels

One version string, everywhere: `0.1.0bN` from `pyproject.toml` (mirrored in
`src/nadare/__init__.py`). Git tag `v<version>`, AppImage
`Nadare-<version>-x86_64.AppImage`, checksums in `SHA256SUMS` next to it. See
[CONTRIBUTING.md](../CONTRIBUTING.md#release-artifacts-and-versioning).

## Channels

| Channel | Artifact | Built by | Updates owned by |
| --- | --- | --- | --- |
| AppImage | `Nadare-<version>-x86_64.AppImage` + `SHA256SUMS` (+ optional `.zsync`) | `scripts/build_appimage.sh`, uploaded to the GitHub release | Nadare's in-app update check |
| Flatpak | local `.flatpak` bundle (not published) | `scripts/build_flatpak.sh` + `packaging/flatpak/` | manual reinstall; Flatpak owns the sandbox update |
| AUR `nadare-bin` | the release AppImage, unpacked into `/opt/nadare` | `packaging/aur/nadare-bin/PKGBUILD` | pacman |
| `.deb` | `dist/nadare_<version>_<arch>.deb` (built, not published) | `scripts/build_deb.sh` | dpkg/apt |
| Source | this checkout | `docs/SOURCE-INSTALL.md` | the user's own `git pull` |

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
  `scripts/fetch_7zip.sh` first; the spec embeds whatever is in `assets/`).
- `LICENSE` under the package's licence directory.
- The desktop file, icon and (Flatpak/AppImage) the AppStream metainfo.
- The `.deb` derives its `libc6 (>= ...)` floor from `readelf -V` on the bundle
  instead of assuming the build host's glibc.

## Maintainer timers

`packaging/systemd/nadare-index-refresh.{service,timer}` and
`nadare-pin-publisher.{service,timer}` are maintainer jobs, not part of any package. They
are installed by hand on a node, as **system** units that run as the checkout owner
(`User=` in the unit), and they read their secrets from a root-only environment file
(`/etc/nadare-index-refresh.env`, `/etc/nadare-pin-publisher.env`, mode 600). A `systemctl
--user` unit cannot read those files, so they are not user units. The unit files in the
repo use `%h`, which in a system unit is root's home: set `User=` and the paths to the
checkout owner's before installing, as the nodes do. See [ARCHIVE-PINS.md](ARCHIVE-PINS.md)
for the pin job.

The refresh script is a source-checkout job (it imports `scripts/*.py` and
`src/nadare`), which is why it is not part of the regular AppImage install.

## Release checks

- CI (`ci.yml`) runs the unit suite on every push to `master`/`main` and on every pull request. Weekly (or by hand) it also rebuilds
  the CZN PCSDK stub in the pinned container and compares it with the committed binary,
  and audits the lock with `pip-audit`. Those two jobs need a container engine or the
  network and have not run in CI yet.
- In-place updates: after building the release AppImage, record its hash in the index with
  `python scripts/release_hash.py <nadare-index checkout> <version> dist/Nadare-<version>-x86_64.AppImage`
  and push the index (`app/releases.json`). Nadare checks the downloaded file against that hash
  and against the release's `SHA256SUMS`; without the index entry it falls back to
  `SHA256SUMS` alone.
- Before publishing, the AppImage must pass `scripts/test_appimage.sh` in a
  throwaway distrobox and `appstreamcli validate` on the metainfo.
- Not yet automated: third-party licence texts for the bundled Qt/PyQt6 and
  system libraries (issue #587/#621). Until then the release artifact carries
  Nadare's `LICENSE` plus the 7-Zip and Inter font licences from `assets/`, and none of the
  Qt/PyQt6 or system-library texts.
