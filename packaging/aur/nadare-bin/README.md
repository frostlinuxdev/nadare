# AUR `nadare-bin`

Wraps the upstream AppImage: unpacked into `/opt/nadare`, `/usr/bin/nadare`
symlinks to it. Builds with plain `makepkg`; no source package because
`python-curl-cffi` is not in `extra`.

Per release:

1. Bump `pkgver`, reset `pkgrel=1`.
2. `updpkgsums` (or copy the hash from the release's `SHA256SUMS`).
3. `makepkg --printsrcinfo > .SRCINFO`
4. Build in a clean chroot (`extra-x86_64-build`), run `namcap`, then push the
   `PKGBUILD` and `.SRCINFO` to the AUR repo.

`namcap` reports `ELF files outside of a valid path ('opt/')` and RELRO/PIE
warnings: expected for a prebuilt bundle. It also reports `libpcre.so.3` /
`libselinux.so.1` for the bundled glib; the app starts anyway on Arch (checked
with `nadare --version` and an offscreen GUI start), but re-check on each bump.
