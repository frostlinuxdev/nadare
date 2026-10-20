# Nadare beta — tester guide

Thanks for testing. This is a beta: installs, logins and updates can break.
Use an account you can afford to lose (see [Safety and ToS](../README.md#safety-and-tos)).

## 1. Get it

Download **one** file from the releases page and check its SHA-256 against the
digest published for that artifact on the releases page:

```sh
sha256sum Nadare-*-x86_64.AppImage
chmod +x Nadare-*-x86_64.AppImage
./Nadare-*-x86_64.AppImage
```

The beta ships as an AppImage only (no Flatpak yet). AppImages need FUSE 2
(`libfuse2` / `fuse2`); without it run
`./Nadare-*.AppImage --appimage-extract-and-run`.

On an X11-only session (or with `QT_QPA_PLATFORM=xcb`) Qt also needs
`libxcb-cursor` (`apt install libxcb-cursor0`, `dnf install xcb-util-cursor`);
Wayland sessions do not.

On a minimal install or a container (a stock distrobox image, for example) the
AppImage also expects a few host packages: `xkeyboard-config` (keyboard layouts),
and on Wayland `libwayland-cursor` and `libwayland-egl`. If the window does not
open and the terminal names a missing Qt platform plugin or library, install
these first.

glibc older than 2.35 (Rocky/RHEL 9, Debian 11, Ubuntu 20.04) is not supported:
the AppImage fails at start with a raw `GLIBC_2.35 not found` style error from the
bundled Python, not a Nadare message.

## 2. What your system needs (Chaos Zero Nightmare)

| Need | Check |
|---|---|
| A working Vulkan driver, with 32-bit libraries | `vulkaninfo --summary` shows your real GPU, not `llvmpipe` |
| A Secret Service keyring (GNOME Keyring or KWallet) | only needed for *Remember me* |
| ~15 GB free (about 12 GB once the game has pulled its own data, plus the Proton runner under 1 GB) | |
| Network on first run | Nadare downloads Proton, WebView2 and the game |
| glibc 2.35 or newer | Ubuntu 22.04+, Debian 12+, Fedora 36+, current Arch |

Nadare brings its own Proton runner and the STOVE helper stub. You do **not** need
Wine or `mingw` installed. **Install `umu-launcher` if you can** (see below): it is the
path the game was developed and tested on. Either install `umu-launcher` (`umu-run` on your PATH, which
brings its own runtime), **or** have your distro's 32-bit libraries (FreeType, udev,
Xext, xkbcommon): on a stock Debian/Ubuntu without them Nadare stops with a message
naming what to install, and `nadare doctor` shows a `32-bit libs:` line.

If Nadare stops with "Missing 32-bit libraries", this is the fix for your distro
(the message in the app shows the same command):

- Debian/Ubuntu/Mint: `sudo dpkg --add-architecture i386 && sudo apt update && sudo apt install libfreetype6:i386 libxext6:i386 libxkbcommon0:i386`
- Fedora: `sudo dnf install freetype.i686 libXext.i686 libxkbcommon.i686`
  (if dnf reports a file conflict, run `sudo dnf upgrade` first)
- Arch/CachyOS: `sudo pacman -S --needed lib32-freetype2 lib32-libxext lib32-libxkbcommon`
  (needs the `[multilib]` repository enabled)
- openSUSE: `sudo zypper install libfreetype6-32bit libXext6-32bit libxkbcommon0-32bit`

### umu-launcher (recommended)

Without `umu-run`, Nadare runs the Proton build directly against your system
libraries. That works on many setups, but it is the less-tested path: on a
Fedora 43 container with an NVIDIA GPU the game stopped at
`GLFW ... WGL: The driver does not appear to support OpenGL` until
`umu-launcher` was installed, after which it launched and stopped normally. If
the game window shows an OpenGL/WGL error, install `umu-launcher` first.

- Arch/CachyOS: `sudo pacman -S umu-launcher`
- Fedora: the release RPM, e.g.
  `sudo dnf install https://github.com/Open-Wine-Components/umu-launcher/releases/download/1.4.4/umu-launcher-1.4.4.fc43.x86_64.rpm`
  (it is not in the main Fedora repositories)
- Debian/Ubuntu/Mint: `sudo apt install ./python3-umu-launcher_*.deb` from the
  [releases page](https://github.com/Open-Wine-Components/umu-launcher/releases)
  (pick the file for your release; `sudo dpkg --add-architecture i386` first)

The first launch through umu downloads the Steam Linux Runtime (a few hundred
MB), so expect a pause.

Install 32-bit Vulkan on your distro if the check above fails:

- Arch/CachyOS: `lib32-vulkan-icd-loader` plus `lib32-nvidia-utils` / `lib32-vulkan-radeon` / `lib32-vulkan-intel`
- Debian/Ubuntu: `sudo dpkg --add-architecture i386 && sudo apt update`, then `mesa-vulkan-drivers:i386` (or the NVIDIA 32-bit libs)
- Fedora: `mesa-vulkan-drivers.i686` (NVIDIA: RPM Fusion `xorg-x11-drv-nvidia-libs.i686`)

## 3. First run

1. **Add game → Chaos Zero Nightmare**, accept the default prefix.
2. Log in by scanning the QR code **shown by your own Nadare window**.
3. Install. The first install fetches the Proton runner (~320 MB), WebView2 (~215 MB) and the STOVE client files (~270 MB); the game then downloads its own data on first launch.

If the install stops with a *WebView2 … SHA-256 check* message, Microsoft has just
published a new build: wait about 15 minutes and click install again.

## 4. When something breaks

Open an issue on the project's GitHub page (one problem per
issue; a short title naming the game and what failed). Run
`./Nadare-*.AppImage doctor` and attach its output, plus:

- your distro and version, GPU and driver version, desktop (KDE/GNOME, Wayland/X11)
- the log from **Open logs** (check it for tokens first; never post `session.json`,
  `secrets.json` or QR codes)
- what you clicked and what you expected

## 5. Please test (and tell us)

Tick off what you can; a one-line note per item is plenty:

- [ ] Install finishes, then **Log in** (QR) works
- [ ] **Launch** opens the game and you reach the lobby
- [ ] **Stop** closes the game and its helpers (nothing left in `ps`)
- [ ] Quit Nadare, start it again: still logged in (needs a keyring; `nadare doctor`
      prints a `keyring:` line explaining why if not)
- [ ] **Log out**, then log in again
- [ ] Close the Nadare window **while the game is running**: the game must keep
      running, and Nadare must quit by itself a moment after you exit the game.
      Starting Nadare again meanwhile should bring the window back
- [ ] Start an install, **Cancel** it part way, then install again: it should
      resume (not start from zero) and finish
- [ ] **Settings → Remove from library…**: only the choices you tick are deleted
      (try it on a game you don't mind losing)
- [ ] Second start while it is open: the existing window comes to the front
- [ ] Leave it a day, launch again: update or "up to date" works
- [ ] Anything that felt slow: how often it freezes, and for how long

Also useful: if you are on Fedora, Arch, openSUSE or anything that is not
Ubuntu/Debian, say so. The first-run downloads (HTTPS from inside the AppImage)
are the part that has differed between distros, so a clean install there is the
single most valuable test.

Known limits:
- Verified GPUs: NVIDIA, and AMD (RX 7900 XTX, Mesa, Debian 13 with umu-run). Intel is
  untested. The game itself freezes for a few seconds every so often on Linux (we traced it
  to the game's own main thread, not the network, GPU or runner); please report if yours
  is much worse than that.
- The first hours are the worst: the GPU driver's shader cache is cold, so new effects can
  hitch once and then not again. Judge smoothness after a few sessions, not the first.
- MangoHud can stop the game from starting under `umu-run`; leave it off.
- FGO (Fate/Grand Order) **does not install in 0.1.0b1**: its lepton-fgo release
  is not in the public index yet (planned for 0.1.0b2). It is experimental, needs
  podman and a binderfs kernel, and on SELinux
  distros (Fedora) it installs a system policy module that stays after you delete
  Nadare. Remove it with `sudo semodule -r lepton_binder`
  (or `setup-host-selinux.sh --remove`).
- The window disappearing while a game runs is intended (see the checklist above):
  Nadare keeps running in the background until the game exits.
