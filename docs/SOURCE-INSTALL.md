# Installing and running from source

The prebuilt AppImage is the easiest way to use Nadare (see the [README](../README.md)).
This page is for running from a checkout, for FGO, and for the per-game requirements.

## Requirements

**Host:** Linux x86_64, Python **3.12+**, a running Wayland or X11 session.

**For Chaos Zero Nightmare** you also need:

- A working Vulkan driver (NVIDIA, or Mesa for AMD/Intel) including its 32-bit Vulkan
  support. No other 32-bit libraries are needed on most systems (see below). A Secret Service keyring (GNOME Keyring or KWallet) is optional:
  without it *Remember me* is unavailable and you log in again each session.
- Nothing else: Nadare downloads its own Proton runner, ships the PCSDK stub
  prebuilt, and runs Proton directly. `umu-launcher` is optional (used if
  found); when it is absent and the host lacks the 32-bit system libraries Proton needs
  (FreeType, udev, Xext, xkbcommon), Nadare downloads a pinned, verified copy of umu, whose
  runtime brings its own. `mingw-w64` is only
  needed to rebuild the stub from source.
  On Ubuntu 24.04+ that downloaded runtime needs one AppArmor profile (one command,
  see `docs/TESTERS.md`); `nadare doctor` shows it. Linux Mint turns that
  restriction off and is not affected.

**For Fate/Grand Order (NA)** the heavy lifting is in **lepton-fgo**;
its `lepton/check-host.sh`
reports the exact list. In short: a live Wayland session, `podman` with the
`crun` runtime, `zstd`, binderfs, `~30 GiB` free, and an NVIDIA (proprietary/Vulkan) or
Mesa GPU. Nadare downloads a pinned, sha256-checked `lepton-fgo` release on the
first install, unless you point the profile (or `$LEPTON_FGO_REPO`) at your own
checkout. FGO support is **experimental** (tested on NVIDIA + CachyOS only).

## Install system packages (source installs)

The AppImage needs none of this for CZN. These packages are for
running Nadare from source; `umu-launcher` is optional (used when present) and
`mingw-w64` is only needed to rebuild the PCSDK stub.

**Arch / CachyOS / Manjaro**
```sh
sudo pacman -S --needed python git curl umu-launcher mingw-w64-gcc
```
(`umu-launcher` lives in `multilib`; enable it in `/etc/pacman.conf` if needed.)

**Fedora**
```sh
sudo dnf install python3 git curl mingw64-gcc
```
`umu-launcher` is not in the main Fedora repositories: install it from its
[releases page](https://github.com/Open-Wine-Components/umu-launcher/releases)
or a COPR that packages it.

**Debian / Ubuntu**
```sh
sudo dpkg --add-architecture i386 && sudo apt update
sudo apt install python3 python3-venv git curl gcc-mingw-w64-x86-64
```
Then install `umu-launcher` from its
[releases](https://github.com/Open-Wine-Components/umu-launcher/releases). It
ships as two `.deb`s for your release; install both at once:
```sh
sudo apt install ./umu-launcher_*.deb ./python3-umu-launcher_*.deb
```
The i386 step is required by umu's 32-bit graphics libraries.

Source installs need Python **3.12 or newer**. Debian 12 (3.11) and Ubuntu 22.04
(3.10) do not provide it by default: use the AppImage, or install a newer
interpreter (for example with the deadsnakes PPA on Ubuntu, or pyenv/uv) and
create the venv with that interpreter.

## Quick start

```sh
git clone https://github.com/frostlinuxdev/nadare.git nadare
cd nadare

# 1. Python environment
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e .          # provides the `nadare` command

# 2. Sanity check
.venv/bin/nadare --version
```

If you would rather not install the project, every command below also works as
`.venv/bin/python -m nadare ...`.

### Configure a profile

Nadare is profile-based. There is no profile editor in the CLI yet, so create
`~/.config/nadare/config.json` by hand once:

**Chaos Zero Nightmare** (installs under Nadare's own data directory; the
`dwproton` runner, the tested DW-Proton 11.0-15 release, is downloaded and
checksum-verified on first install; `"cachyos"` and `"ge"` are alternatives):
```json
{
  "schema": 1,
  "active_profile": "czn",
  "profiles": {
    "czn": {
      "backend": "stove-czn",
      "title": "Chaos Zero Nightmare",
      "runner": "dwproton"
    }
  }
}
```

**Fate/Grand Order (NA)** (source checkouts only; the beta AppImage is built
without FGO. Point `repo` at your `lepton-fgo` checkout; see its README to
install the stack first):
```json
{
  "schema": 1,
  "active_profile": "fgo",
  "profiles": {
    "fgo": {
      "backend": "fgo-lepton",
      "title": "Fate/Grand Order (NA)",
      "context": "fgo",
      "repo": "~/lepton-fgo",
      "mode": "strict",
      "accept_account_notice": true
    }
  }
}
```

`accept_account_notice` records that you have read lepton-fgo's account-recovery
notice. The FGO installer refuses to skip it by design; unattended installs must
set it.

### Run

The CLI is the most reliable entry point in 0.1:

```sh
.venv/bin/nadare list                  # show profiles
.venv/bin/nadare status -p czn         # or -p fgo
.venv/bin/nadare install -p czn        # first-time setup + download
.venv/bin/nadare login   -p czn        # QR login for STOVE (CZN)
.venv/bin/nadare launch  -p czn        # renew token + update + play
.venv/bin/nadare stop    -p czn
.venv/bin/nadare verify  -p czn
.venv/bin/nadare repair  -p czn        # re-check and re-download damaged files
.venv/bin/nadare remake-prefix -p czn --yes  # rebuild a broken Wine prefix (game files kept)
```

> **CLI note:** `login` and `launch` are separate processes, so the in-memory
> session from `login` does not carry over. To use the CLI, set
> `"remember_me": true` on the profile and have a working OS keyring, so
> `launch` can restore the saved session. Otherwise `nadare launch` exits `3`
> with `log in first, or enable remember_me`. The GUI keeps the session for
> the lifetime of the window, so remember-me is optional there.

Add `-q` to suppress log lines and keep progress, or `-v` for debug logging. Both
work before or after the command, for example `nadare -v status -p czn`.

The PyQt6 GUI is available from a source checkout:

```sh
./scripts/dev.sh gui
```

`scripts/dev.sh gui` starts the window directly; `nadare gui` runs the same
entry point (`app.run_gui` → `nadare.gui.run`).

### First launch

- **CZN:** INSTALL creates the prefix, installs Microsoft's WebView2 Runtime
  (downloaded from Microsoft), fetches the game (~270 MB) and builds the PCSDK
  stub. LOG IN shows a QR code — approve it in the STOVE mobile app. LAUNCH
  refreshes the game token, applies updates, then starts the game, which
  downloads its own several-GB content on first run.
  - **Remember me / Log out:** *Settings → Account → Remember me* keeps the STOVE
    login in your OS keyring between runs (off by default; needs a Secret
    Service provider such as GNOME Keyring or KWallet — without one you'll see
    *"Remember me unavailable: system keyring not usable"* — `nadare doctor`
    prints the reason on its `keyring:` line).
    *Log out* on the game page forgets the saved login and the in-memory session.
- **FGO:** `install` calls `lepton-fgo`'s `install.sh`, which builds the stack,
  boots the context and installs the APKs. The first in-game launch downloads
  ~1.5 GB. The window is a plain Wayland client (no gamescope mode yet).

You log in once per profile. A launch anywhere else can retire the game session
even while its expiry still looks valid; Nadare mints a fresh one on every
launch, so this normally repairs itself.


## Updating

**Source checkout**
```sh
cd nadare
git pull
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e .
```
The CZN PCSDK stub is rebuilt automatically when missing; force a rebuild with
`rm -rf ~/.local/share/nadare/profiles/*/stub` before the next install/launch.

**Downloaded package** (AppImage): download the new artifact and
replace the old one. Nadare does not update itself. (A crash-safe update journal
exists in the code but game updaters do not use it yet.)
Direct-mode games (Arknights with a manifest source, always Stella Sora) show an UPDATE button when the published manifest version differs from the version last synced; this check honours the *Check for launcher and game updates* setting.

FGO's runtime stack is updated by `lepton-fgo update`; Nadare calls it as part of
the backend's `update`.

---
