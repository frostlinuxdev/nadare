# Troubleshooting and uninstall

## Nadare does not start?

Start it from a terminal, so you see what it prints, and run its own check:

| You use | Start it | Check what is missing | Its log |
|---|---|---|---|
| Flatpak | `flatpak run io.github.frostlinuxdev.Nadare` | `flatpak run io.github.frostlinuxdev.Nadare doctor` | `~/.var/app/io.github.frostlinuxdev.Nadare/.local/state/nadare/logs/nadare.log` |
| AppImage | `~/Downloads/Nadare-*-x86_64.AppImage` | `~/Downloads/Nadare-*-x86_64.AppImage doctor` | `~/.local/state/nadare/logs/nadare.log` |

`doctor` ends with one verdict line and, for each problem, what to install. The usual ones:

- **The AppImage does nothing on double-click:** it is not allowed to run yet; see
  [INSTALL.md](INSTALL.md).
- **The AppImage says FUSE is missing** (containers, some hardened systems): add
  `--appimage-extract-and-run`, or use the Flatpak.
- **Old or immutable system** (an older glibc, SteamOS, Bazzite, NixOS): use the Flatpak; it
  brings its own libraries.
- **NixOS: the AppImage stops with `execv error: No such file or directory`:** NixOS has no
  standard program loader. Use the Flatpak (`services.flatpak.enable = true;`). The AppImage
  also starts with `appimage-run`, but games then need `pkgs.umu-launcher` in
  `environment.systemPackages` too (NixOS has no `/usr/bin/python3` for Nadare's own copy).
- **"Vulkan unavailable" or "software-only":** install your GPU's Vulkan driver, including its
  32-bit part (games need it); `doctor` prints the command for Debian/Ubuntu, Fedora and Arch.
- **Two copies:** the Flatpak and the AppImage keep separate folders. Settings → **Move to the
  Flatpak…** moves your games and settings over.

When asking for help, attach the log or the support report (**Get support report** in the Logs
window), not whole folders.

## A game or a feature does not work

Start with the log:
`~/.local/state/nadare/logs/nadare.log` (rotated, secrets masked).

Share logs or the Support report (both leave sign-ins out), not whole folders. If you must share
Nadare's data folder or a game's folder, delete any `session.json`, `session.enc.json` or
`session.json.expired-bak` in it first: a run that was killed can leave a live sign-in there, and
the Support report warns when one is on disk.

| Symptom | What to do |
|---|---|
| `nadare: command not found` | Use `.venv/bin/nadare`, or run `.venv/bin/python -m nadare`. |
| GUI fails to start (AppImage) | Run `./Nadare-*.AppImage doctor`. On Wayland install `libwayland-cursor` and `libwayland-egl`; also install `xkeyboard-config`. If FUSE is unavailable, use `--appimage-extract-and-run`. |
| GUI fails to start (source) | Install the GUI extras: `.venv/bin/pip install -e ".[dev]"`. On X11 also install `libxcb-cursor0` (Debian/Ubuntu) or `xcb-util-cursor` (Fedora). PyQt6 must load on your display server. |
| CZN `exit 2` / "cannot reach the update server" | Network or STOVE CDN issue. Retry; check DNS/proxy. Proxies come from the standard `HTTPS_PROXY`/`HTTP_PROXY` environment variables (there is no `proxy_url` setting). |
| CZN "renew failed code=44006" / "Registered OTP user" | Your STOVE account uses two-step (OTP) login. Update to 0.1.0b20 or newer: Nadare then renews through STOVE's second route. |
| CZN `exit 5` / "has not accepted ... terms of service" | The STOVE account never agreed to the game's terms. Nadare opens a window with the terms; press **Agree and continue**, or open https://policy.onstove.com/?inflow_path=LAUNCHER&show_play_button=Y&game_id=STOVE_CHAOSZERO&lang=en in your browser while signed in to the same account. |
| CZN `exit 3` / "login expired" | Run `nadare login -p <profile>` and scan the QR again. |
| CZN `exit 6` / "no-connect" | The PCSDK handshake changed or the runner can't run the game. Try another runner (`"runner": "ge"`), or update Nadare. |
| CZN `exit 7` / "handshake-rejected" | The login session was retired. Log in again; make sure only one client is using the account. |
| CZN `exit 8` | The game is already running in this profile. Stop it first. |
| CZN `exit 9` | A runner download failed its checksum. Delete `~/.local/share/nadare/runners` and retry. |
| Arknights / Stella Sora freezes on the first frame (window drawn, CPU idle), native Wayland, NVIDIA | DXVK 3.1.1 (bundled with DW-Proton 11.0-14, Proton-CachyOS 20261005, GE-Proton11-9) can hang a game on its first present through its `VK_EXT_present_timing` path. Nadare sets `DXVK_CONFIG="dxvk.enablePresentTiming = False"` automatically for every game in the catalog, so no profile edit is needed; a profile that already sets `DXVK_CONFIG` keeps its own value. To avoid the path another way, pin a runner with DXVK 3.1 (DW-Proton 11.0-13, GE-Proton11-7, Proton-CachyOS 20260703); `"display": "x11"` also avoids it. The launch log line `runner: <build>, display: <driver>` shows what is in use. Do not point a profile at a moving `... Latest` runner directory: it can change under you. |
| Stella Sora: the gacha swipe (mouse drag) does nothing on Wayland | Wine's native Wayland driver does not deliver that drag; XWayland does. Stella Sora therefore defaults to XWayland (`PROTON_ENABLE_WAYLAND=0`). To go back to native Wayland set `"display": "wayland"` on the profile in `~/.config/nadare/config.json`; `"x11"` / `"auto"` keep XWayland. Arknights defaults to native Wayland. The launch log line `runner: <build>, display: <driver>` shows which is in use. |
| "the shipped PCSDK stub … is missing or damaged" | Reinstall Nadare (the stub ships with it; nothing is built on your machine). |
| WebView2 install fails | Delete `~/.cache/nadare/czn/MicrosoftEdgeWebView2RuntimeInstallerX64.exe` and retry; the prefix must be writable. |
| FGO host check fails | Run `lepton-fgo`'s `check-host.sh` and install what it lists. |
| FGO + SELinux | Run the printed one-time `sudo .../setup-host-selinux.sh` as root, then retry. |
| "keyring unavailable" notice | Account logins are keyring-only. Install a Secret Service provider (gnome-keyring, KWallet) to enable **Remember me**; otherwise log in again each session — tokens are never written in plaintext. See [PRIVACY.md](../PRIVACY.md). |
| A game's login or web-page button does nothing (the game says it opened your browser, but no window appears) | The game asks `xdg-desktop-portal` to open the browser. If that service started before your desktop session (a lingering user manager, or a compositor config that does not export the display), it has no `DISPLAY`/`WAYLAND_DISPLAY` and the browser exits at once with "no DISPLAY environment variable specified". Check with `journalctl --user -b \| grep "no DISPLAY"`, then run `dbus-update-activation-environment --systemd WAYLAND_DISPLAY DISPLAY` followed by `systemctl --user restart xdg-desktop-portal`, and click the button again. To make it stick on Hyprland, Sway or niri, put the first command in your compositor's startup section. Plasma and GNOME normally do this for you. |

## Is a game safe?

Press **Safety check** on the game's page. It looks, read-only, for signs of known attacks in
that game's own files. **Clean** only means nothing matched the list Nadare currently has: a
reinstalled game folder can wipe old evidence, and an attack that is not listed yet is not
found. If it says **"No indicator list yet"**, no signed list has arrived, so nothing was
checked. See [ACCOUNT-SAFETY.md](ACCOUNT-SAFETY.md#safety-check-read-only) for what the check
does and does not prove.

Until manifest signing lands, Nadare's index and integrity trust ultimately rests
on the host CA store. Nadare verifies HTTPS with a bundled CA list; set
`NADARE_CA_BUNDLE=/path/to/ca.pem` (or `SSL_CERT_FILE`) to use your own, e.g.
behind a company proxy. If that store trusts an extra root or intermediate (some
distro images and corporate proxies ship one), an HTTPS fetch can be intercepted
even though the certificate looks valid. Run `nadare doctor --tls` to check the
two known forged-chain test hosts; it is opt-in and never runs as part of a
normal `doctor`.

## Move a game to another drive

Open the game's page and choose **⋯ → Move game…**, or open **Settings → Game** and use
**Move game and prefix…** (or **Move prefix only…** for a game whose files you manage
yourself). Nadare moves the files and updates the game's settings. It refuses a folder
that belongs to another game or is not safe to use, and says why.

## Uninstall

`nadare uninstall-all` removes every profile and, unless you ask otherwise, Nadare's
runners as well as its settings, logs, cache and data. Run it without `--yes` first to
see the plan; it deletes nothing until you add `--yes`. Game folders, prefixes and old
prefix backups are kept unless you also pass `--with-games` or `--with-backups`, and
`--keep-runtimes` leaves the downloaded Proton/Wine runners in place.

umu's Steam Runtime downloads in `~/.local/share/umu` (1.5-4 GB) are kept by default,
because Steam, Lutris, Heroic and other umu apps share that folder. Add `--with-umu` to
delete it too. It is skipped while a game runs from it, and any app that needs it
downloads it again.

On a build that includes Fate/Grand Order, if its SELinux policy module
(`lepton_binder`) is loaded, the plan also prints the command that removes it. Add
`--remove-selinux-module` to run it; that asks for your password. Without the flag the
module is kept.

**From source:** delete the checkout and its venv. Then remove Nadare's data
(its own data lives only under XDG directories; shortcuts are listed below):

```sh
rm -rf ~/.local/share/nadare \
       ~/.config/nadare \
       ~/.local/state/nadare \
       ~/.cache/nadare
```

**Downloaded package:** remove the AppImage, then run the data removal above.

**Shortcuts:** shortcuts you asked Nadare to create live outside XDG data: the menu entry
`~/.local/share/applications/nadare-*.desktop`, optionally a copy on your Desktop, and,
for Steam, entries in each Steam account's `userdata/*/config/shortcuts.vdf` (native and
Flatpak Steam). Remove them with `nadare shortcut remove`, or delete them by hand.

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
