# Nadare roadmap

Where Nadare stands at the 0.1.0b2 beta, what is limited, and what comes next.

## What works in 0.1.0b2

- **Chaos Zero Nightmare:** install, update, QR login, launch on Proton, with an
  optional *Remember me* in your OS keyring. Tested on NVIDIA and AMD.
- **Arknights (EN) and Stella Sora (EN):** direct, verified installs from the
  publisher's servers, updates and launch through Proton.
- **The app:** a PyQt6 window with a library, per-game pages with news,
  settings, and crash-safe, resumable downloads and updates. The AppImage is
  the supported way to run it.
- **Experimental:** Fate/Grand Order (NA) through the lepton-fgo Android
  container. It does not install in 0.1.0b2.
- **Scaffolding only:** generic `wine`, `steam`, `script` and `waydroid`
  backends. They work, but are not a supported product.

## Known limits

- Intel GPUs are untested. AMD is verified for Chaos Zero Nightmare only.
- Fate/Grand Order is NA only, needs podman, binderfs and a Wayland session,
  and was tested on NVIDIA.
- STOVE allows one valid login per account at a time.
- The interface is English only, and the profile editor does not yet cover every
  backend option.
- No Flatpak or `.deb` is published yet; the recipes are in `packaging/`.

## Next

- Fate/Grand Order install: publish the lepton-fgo release in the public index
  (planned for a later beta).
- A guided first-run flow (add a game, configure, install).
- A GUI profile editor for every backend option, and tested uninstall and
  "move installation" flows.
- More translations and a completeness check.
- CI that builds the AppImage on tag and publishes SHA-256 sums; a published
  Flatpak and `.deb`.
- A public compatibility page (distro, GPU and driver, runner versions).

Ideas that are parked: library locations on other drives with an *Import
existing* action, per-game Wine presets for other Windows clients, a runner
selection UI, playing past the 6 h CZN game-token expiry, and a memory-safe
rewrite of the CZN PCSDK stub.

## Open decisions

1. **Publish identity.** The project is Nadare, hosted on GitHub
   (`frostlinuxdev/nadare`), with security reports through GitHub's private
   vulnerability reporting. Still open: the package id (`org.nadare.launcher`
   is a placeholder in the AppImage recipe) and whether the project later moves
   to its own GitHub organisation.
2. **Browser "Game Start" / SSO login for CZN is skipped.** It would need
   STOVE's own launcher `client_secret`, which Nadare will not ship or extract.
   QR login stays the main path.
3. **Generic backends stay scaffolding.** The README says so on purpose.
