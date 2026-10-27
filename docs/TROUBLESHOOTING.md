# Troubleshooting and uninstall

Start with the log:
`~/.local/state/nadare/logs/nadare.log` (rotated, secrets masked).

| Symptom | What to do |
|---|---|
| `nadare: command not found` | Use `.venv/bin/nadare`, or run `.venv/bin/python -m nadare`. |
| GUI fails to start (AppImage) | Run `./Nadare-*.AppImage doctor`. On Wayland install `libwayland-cursor` and `libwayland-egl`; also install `xkeyboard-config`. If FUSE is unavailable, use `--appimage-extract-and-run`. |
| GUI fails to start (source) | Install the GUI extras: `.venv/bin/pip install -e ".[dev]"`. On X11 also install `libxcb-cursor0` (Debian/Ubuntu) or `xcb-util-cursor` (Fedora). PyQt6 must load on your display server. |
| CZN `exit 2` / "cannot reach the update server" | Network or STOVE CDN issue. Retry; check DNS/proxy. Proxies come from the standard `HTTPS_PROXY`/`HTTP_PROXY` environment variables (there is no `proxy_url` setting). |
| CZN `exit 3` / "login expired" | Run `nadare login -p <profile>` and scan the QR again. |
| CZN `exit 6` / "no-connect" | The PCSDK handshake changed or the runner can't run the game. Try another runner (`"runner": "ge"`), or update Nadare. |
| CZN `exit 7` / "handshake-rejected" | The login session was retired. Log in again; make sure only one client is using the account. |
| CZN `exit 8` | The game is already running in this profile. Stop it first. |
| CZN `exit 9` | A runner download failed its checksum. Delete `~/.local/share/nadare/runners` and retry. |
| Arknights / Stella Sora freezes on the first frame (window drawn, CPU idle), native Wayland, NVIDIA | DXVK 3.1.1 (bundled with DW-Proton 11.0-14, Proton-CachyOS 20261005, GE-Proton11-9) can hang a game on its first present through its `VK_EXT_present_timing` path. Arknights sets `DXVK_CONFIG="dxvk.enablePresentTiming = False"` by default, so it is not affected; for another DXVK game add `"extra_env": {"DXVK_CONFIG": "dxvk.enablePresentTiming = False"}` to the profile, or pin a runner with DXVK 3.1 (DW-Proton 11.0-13, GE-Proton11-7, Proton-CachyOS 20260703). `"display": "x11"` also avoids it. The launch log line `runner: <build>, display: <driver>` shows what is in use. Do not point a profile at a moving `... Latest` runner directory: it can change under you. |
| Stella Sora: the gacha swipe (mouse drag) does nothing on Wayland | Wine's native Wayland driver does not deliver that drag; XWayland does. Stella Sora therefore defaults to XWayland (`PROTON_ENABLE_WAYLAND=0`). To go back to native Wayland set `"display": "wayland"` on the profile in `~/.config/nadare/config.json`; `"x11"` / `"auto"` keep XWayland. Arknights defaults to native Wayland. The launch log line `runner: <build>, display: <driver>` shows which is in use. |
| Stub build fails | Install `mingw-w64` (`gcc-mingw-w64-x86-64` / `mingw-w64-gcc`). |
| WebView2 install fails | Delete `~/.cache/nadare/czn/MicrosoftEdgeWebView2RuntimeInstallerX64.exe` and retry; the prefix must be writable. |
| FGO host check fails | Run `lepton-fgo`'s `check-host.sh` and install what it lists. |
| FGO + SELinux | Run the printed one-time `sudo .../setup-host-selinux.sh` as root, then retry. |
| "keyring unavailable" notice | Account logins are keyring-only. Install a Secret Service provider (gnome-keyring, KWallet) to enable **Remember me**; otherwise log in again each session — tokens are never written in plaintext. See [PRIVACY.md](../PRIVACY.md). |
| A game's login or web-page button does nothing (the game says it opened your browser, but no window appears) | The game asks `xdg-desktop-portal` to open the browser. If that service started before your desktop session (a lingering user manager, or a compositor config that does not export the display), it has no `DISPLAY`/`WAYLAND_DISPLAY` and the browser exits at once with "no DISPLAY environment variable specified". Check with `journalctl --user -b \| grep "no DISPLAY"`, then run `dbus-update-activation-environment --systemd WAYLAND_DISPLAY DISPLAY` followed by `systemctl --user restart xdg-desktop-portal`, and click the button again. To make it stick on Hyprland, Sway or niri, put the first command in your compositor's startup section. Plasma and GNOME normally do this for you. |

Until manifest signing lands, Nadare's index and integrity trust ultimately rests
on the host CA store. Nadare verifies HTTPS with a bundled CA list; set
`NADARE_CA_BUNDLE=/path/to/ca.pem` (or `SSL_CERT_FILE`) to use your own, e.g.
behind a company proxy. If that store trusts an extra root or intermediate (some
distro images and corporate proxies ship one), an HTTPS fetch can be intercepted
even though the certificate looks valid. Run `nadare doctor --tls` to check the
two known forged-chain test hosts; it is opt-in and never runs as part of a
normal `doctor`.

## Uninstall

**From source:** delete the checkout and its venv. Then remove Nadare's data
(its own data lives only under XDG directories; shortcuts are listed below):

```sh
rm -rf ~/.local/share/nadare \
       ~/.config/nadare \
       ~/.local/state/nadare \
       ~/.cache/nadare
```

**Downloaded package:** remove the AppImage, then run the data removal above.

**Shortcuts:** shortcuts you asked Nadare to create live outside XDG data:
`~/Desktop/*.desktop` and, for Steam, entries in
`~/.steam/steam/userdata/*/config/shortcuts.vdf`. Remove them with
`nadare shortcut remove`, or delete them by hand.

**Stored tokens:** account (CZN) logins live only in the keyring. Remove the
`nadare-launcher` service entries:
```sh
secret-tool clear service nadare-launcher token:czn   # one per profile
```
or delete them in Seahorse / KWallet. Non-account secrets may use the plaintext
fallback at `~/.config/nadare/secrets.json` when no keyring is present; delete
that file too if it exists.

**FGO's own data** (context, APKs, assets) is managed by lepton-fgo:
`lepton-fgo uninstall fgo [--purge]`. The game's `--purge` is explicit for a
reason — read its warning first.

From a checkout, `nadare remove` drops a profile from the library (its shortcuts
and config entry) and keeps the game files unless you ask otherwise:

```sh
nadare remove -p czn --yes                        # profile + shortcuts only
nadare remove -p czn --delete-data --yes          # also the profile's data folder
nadare remove -p czn --delete-install-dir --delete-prefix --yes
```

`--delete-data`, `--delete-install-dir` and `--delete-prefix` are each opt-in
(directories shared with another profile or outside Nadare's own folders are
refused); `--yes` is required to confirm. The GUI exposes the same action.
