# Privacy

Nadare is a local launcher. It has **no telemetry, no analytics and no crash
reporting**, and it sends nothing about you to the Nadare project. Like any program
on the internet, every request carries your IP address and a User-Agent that names
Nadare; nothing else identifies you.

What Nadare contacts on its own, without you starting an install or a game:

- **the project host, at every start**: the update check (latest-release metadata) and
  the safety and service notices (`notices.json`). Turn them off with *Settings → Nadare
  (all games) → Check for launcher and game updates* and *Show safety and service notices*
  (or `NADARE_RELEASES_API=""`). The update-check setting also covers the published game
  file list that a direct-mode game page reads to show an update badge;
- **the publisher of the game whose page you open**: its public news feed and page art.
  These are not affected by the update-check setting; they are fetched only for the
  game you open, and cached on disk for 15 minutes.

Everything else happens because you asked for it: an install, an update, a sign-in or
a launch. The config carries `"telemetry": false` as an explicit, permanent record.

Everything Nadare stores stays on your machine, under your XDG directories (plus
any desktop or Steam shortcut you ask it to create).

## What talks to the network

| Destination | When | What is sent |
|---|---|---|
| The project host (Nadare release page and index) | every start, unless turned off; game install/update; opening a direct-mode game page | HTTPS requests for the latest-release metadata, the notices file, the published game file lists and pinned-installer hashes, and the curated cover list (`catalog/art.json`). Nothing about you beyond the IP address and User-Agent every request carries. |
| Game publishers (see the list below) | install, update, sign-in, launch; opening the game's page | The game download requests, and for games that sign in through Nadare the same account data the official launcher sends: STOVE (CZN) gets your sign-in tokens and a device key; Netmarble (Mongil) gets the sign-in and a device key. Opening a page fetches that game's public news and art. |
| GitHub Releases (`github.com`, its asset CDN) | when a game needs a Proton runner, and when Nadare sets up its own umu-run | A normal HTTPS download of a pinned, checksummed Proton build or umu-run release. |
| Valve (`repo.steampowered.com`) | first launch with umu-run | umu-run downloads the Steam Runtime it runs games in. |
| Microsoft (`go.microsoft.com`) | CZN and GFL2 (Haoplay) install and launch | A download of the WebView2 Runtime bootstrapper, installed into the game prefix. |
| Microsoft (`aka.ms`) | CZN and Mongil install and launch (and any game where you turn it on) | A download of the Visual C++ 2015-2022 x64 redistributable, installed into the game prefix. |
| Cover art sources | when a game has no cached cover yet, or `fetch-art` | The project's curated list first, then the publisher's own pages, the Steam store and CDN (`store.steampowered.com`, `steamstatic.com`), SteamGridDB (`steamgriddb.com`, only with your own API key) and AniList (`anilist.co`). The game's title or Steam app id is sent; nothing about you. |
| Flathub (`flathub.org`) | the Flatpak build only, when you ask Nadare to install missing graphics or 32-bit packages | The usual Flatpak install requests for those runtime extensions. |
| Certificate test hosts (`we-have-a-problem.com`) | only `nadare doctor --tls` | Two HTTPS connections that a correct certificate store must refuse; a check for a forged root certificate. |
| `lepton-fgo`'s stack | FGO install/update/launch | Whatever `install.sh` and the lepton CLIs contact (upstream sources, FGO manifest/APKs, the NVIDIA/Venus stack). Nadare does not add any. |

Links Nadare only opens in your browser when you click them (the community Discord,
`discord.gg` / `discord.com`, and publisher web pages) are not requests by Nadare.

### Publishers by game

| Game | Hosts |
|---|---|
| Genshin Impact, Honkai: Star Rail, Honkai Impact 3rd, Zenless Zone Zero | `hoyoverse.com` |
| Wuthering Waves, Punishing: Gray Raven | `kurogame.com`, `kurogame.net`, `kurogames.com`, `kurogames.net` |
| Arknights: Endfield | `gryphline.com` |
| Neverness to Everness | `perfectworld.com` |
| Girls' Frontline 2 | `sunborngame.com` (Darkwinter), `haoplay.com` (Haoplay) |
| Aether Gazer | `yongshigame.com`, `aethergazer.com` |
| Brown Dust 2 | `pmang.cloud`, `browndust2.com` |
| Mongil: Star Dive | `netmarble.com` |
| Chaos Zero Nightmare | `onstove.com` |
| Arknights, Stella Sora | `yo-star.com`, `arknights.global`, `stellasora.global` |
| NIKKE | `nikke-en.com`, `playerinfinite.com`, `jupiterlauncher.com` |
| Fate/Grand Order (NA) | `fate-go.us` |

Nadare does **not** import the official launchers' cookies or saved credentials.
It authenticates with your own account through the front end for that game.

## Tokens and sessions

- Nadare collects **no passwords**. CZN signs in by QR approval; FGO's login is
  handled entirely by the game.
- Session and refresh tokens are **only** written on your machine and only if
  the backend needs them. CZN's account **refresh token is keyring-only**
  (`SecretStore(allow_fallback=False)`); its `czn_auth.json` holds only
  non-secret state, and the short-lived game-token `session.json` is written
  `0600` next to the stub and removed after the game exits. `lepton-fgo` keeps
  its own context files. See [docs/ACCOUNT-SAFETY.md](docs/ACCOUNT-SAFETY.md).
- Tokens never appear in logs. `core/events.py` masks any secret-looking value
  to `first4…last4<len>` before it reaches the log formatter, the CLI or the GUI.
- Tokens are never written to `config.json`.

### Keyring only

If the optional `keyring` package and a working backend are present (Secret
Service / KWallet / macOS Keychain / Windows Credential Manager), Nadare stores
per-profile tokens in the **system keyring**, service name `nadare-launcher`,
account `token:<profile>`.

There is no plaintext fallback. If no keyring is usable, **Remember me** is
unavailable and the token stays in memory for the session only. Set
`NADARE_NO_KEYRING=1` to behave as if no keyring exists ("never remember").
Older betas could write `~/.config/nadare/secrets.json`; current versions never
read or write it, so you can delete a leftover copy.

Account credentials such as the CZN (STOVE) refresh token follow the same rule.
See [docs/ACCOUNT-SAFETY.md](docs/ACCOUNT-SAFETY.md).

The system keyring does not protect against malicious software already running
as your user. Full-disk encryption (LUKS) is the real protection if a machine is
lost or stolen.

## What is stored where

Nadare follows the XDG base directories, all namespaced under `nadare`:

| Path | Contents |
|---|---|
| `~/.config/nadare/config.json` | Profiles (game, backend, prefix, runner, language), settings. Never tokens. |
| `~/.config/nadare/secrets.json` | **Not used by current versions.** A leftover from older betas that can be deleted. |
| `~/.local/share/nadare/runners/` | Downloaded Proton/GE/DW runners. |
| `~/.local/share/nadare/profiles/<name>/` | Per-profile game trees, Wine/Proton prefixes, the CZN stub and session. |
| `~/.local/state/nadare/logs/nadare.log` | Rotating log, secrets masked. |
| `~/.local/state/nadare/` | Crash-safe update journals / receipts. |
| `~/.cache/nadare/` | Downloads (WebView2 bootstrapper, etc.) and scratch data. |

Respect `XDG_DATA_HOME`, `XDG_CONFIG_HOME`, `XDG_STATE_HOME` and
`XDG_CACHE_HOME`; Nadare does. Nothing is hard-coded to `~`.

`lepton-fgo` keeps its own context and data under its own prefix
(`~/.local/share/lepton-nv` by default) and config directory; consult its docs.
Nadare does not copy or mirror that data.

## Deleting your data

```sh
rm -rf ~/.local/share/nadare ~/.config/nadare ~/.local/state/nadare ~/.cache/nadare
```

Stored keyring tokens: clear the `nadare-launcher` service in your credential
manager (e.g. `secret-tool clear service nadare-launcher token:<profile>`). A
leftover `~/.config/nadare/secrets.json` from an older beta can simply be deleted.

Game-side data (FGO APKs/assets, the CZN game tree) is governed by the tool that
installed it. See the Uninstall section of [README.md](README.md).

## Reporting

Never attach tokens, QR codes, `secrets.json`, `session.json`, raw auth traffic
or full logs to an issue. Read [SECURITY.md](SECURITY.md) first.
