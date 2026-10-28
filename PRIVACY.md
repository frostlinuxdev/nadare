# Privacy

Nadare is a local launcher. It has **no telemetry, no analytics, no crash
reporting and sends nothing about you to anyone**. The one automatic request it
makes to the project's own servers is an optional update check against the
release page (on by default; turn it off with *Settings → Launcher → Check for
launcher and game updates*). The same setting also controls a small request for
the published game file list of direct-mode games when you open their page (to
tell you when an update is available). The config even carries `"telemetry":
false` as an explicit, permanent record.

Everything Nadare stores stays on your machine, under your XDG directories (plus
any desktop or Steam shortcut you ask it to create). The
network is only used to do the job you asked for.

## What talks to the network

| Destination | When | What is sent |
|---|---|---|
| STOVE auth / CDN (CZN) | install, login, update, launch | Your STOVE login (QR approval, then rotating tokens) and the game download requests. Nadare's `auth.py`/`update.py` stand in for the official launcher. |
| GitHub Releases | CZN runner download | A normal HTTPS request for a pinned Proton build; nothing about you. |
| Microsoft | CZN install | A download of the WebView2 Runtime bootstrapper (`go.microsoft.com`), installed into the game prefix. |
| Microsoft | CZN, Arknights and Stella Sora install and launch | A download of the Visual C++ 2015-2022 x64 redistributable (`aka.ms`), installed into the game prefix. |
| `lepton-fgo`'s stack | FGO install/update/launch | Whatever `install.sh` and the lepton CLIs contact (upstream sources, FGO manifest/APKs, the NVIDIA/Venus stack). Nadare does not add any. |
| Nadare release page and index (project host) | every start, unless disabled; game install/update; opening a direct-mode game page | A normal HTTPS request for the latest-release metadata (`hosting.releases_api()`) and the project's Yostar manifest index (the published game file list, used to show an update badge) and pinned-installer hashes (`hosting.index_base()`); nothing about you and no identifier. Disable both with the *Check for launcher and game updates* setting or `NADARE_RELEASES_API=""`. |
| Official game news and artwork (publisher feeds) | when a game page or the library is shown | A request to the publisher's public news API and image CDN for that game's announcements and artwork (e.g. STOVE for CZN). Cached on disk for 15 minutes. |

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
