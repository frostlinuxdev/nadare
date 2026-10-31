# Steam prefix fixer (design)

Status: superseded by `STEAM-SETUP.md`, which is built. Kept as the original design; where it
differs (below), the code follows `STEAM-SETUP.md`.

## Problem

Some Steam games fail on Linux for reasons that live in the game's Proton prefix, not in
Proton itself. Example: Epic Seven (Steam app 5019180) shows
`STOVE ERROR Cmd:STOVE_INITIALIZE Code:-80` on the server-select screen because the prefix
has no WebView2 runtime (the game ships `WebView2Loader.dll`). Installing the Microsoft
WebView2 runtime into the game's own prefix fixed it.

Nadare's generic `steam` backend only hands Steam a `steam://rungameid/<id>` URI and never
touches the prefix, so users hit this and have no way to repair it.

## Goals

- Epic Seven is a game in Nadare's catalog. The user adds it like any other game.
- If it is not installed in their Steam, Nadare asks Steam to install it. Nadare then takes
  care of the rest: prefix, WebView2, runner, settings.
- Work however the game is started afterwards (Steam, Nadare, desktop shortcut).
- The same machinery is reusable for other Steam games later (GFL2 is the next one).

## Non-goals

- No login handling: the identity comes from the Steam client (the game signs in with the
  Steam account and STOVE creates the account from it). Nadare cannot buy or own the game
  for the user.
- Steam stays the downloader for this game. Nadare asks Steam to install it
  (`steam://install/<id>`) and does not fetch the files itself. Nadare's purpose is to get
  games running from one place, not to download every game itself: where a game only runs
  with Steam (its ownership check needs it), Nadare uses Steam and takes care of the rest.
  The game's own content (about 2.6 GB) is downloaded by the game on first launch from its
  public CDN, not by Nadare.
- No edits to Steam's `localconfig.vdf`.
- No custom Proton build. WebView2 is per-prefix state, which Proton cannot carry.

## Flow

1. **Add.** The user adds Epic Seven from Nadare's catalog (a `steam` backend profile with
   `steam_app_id = 5019180`).
2. **Installed?** Look for `appmanifest_<id>.acf` in every Steam library (the existing
   `backends/steam.py` helpers). If it is missing, Nadare asks Steam to install it with
   `steam://install/<id>`, after saying so plainly. Steam shows its own install dialog and
   owns the download; Nadare waits for the manifest to appear. If the user does not own the
   game, Steam says so and Nadare reports it, nothing more.
3. **Prefix.** `compatdata/<id>/pfx` in the library that holds the game. It only exists
   after the game has run once under Steam. If it does not exist yet, Nadare asks the user
   to start the game once from Steam and close it (it will show the STOVE error, which is
   expected), then continues. Refuse with a clear message for Flatpak Steam if the path
   cannot be resolved.
4. **Check.** Per prerequisite: WebView2 (`Program Files (x86)/Microsoft/EdgeWebView`),
   VC++ runtime (reuse `core/vcredist.py`). Show found / missing.
5. **Gates.** Refuse when the game or a wineserver for that prefix is running, or when the
   runner Steam uses for the game cannot be determined.
6. **Back up.** Copy the prefix to `compatdata/<id>.bak-pre-<fix>`, show the size, ask
   first. Never delete the backup automatically.
7. **Fix.** Run the missing installer with the runner Steam uses for that game, on a
   private virtual display (never the user's display; without any display the WebView2
   installer hangs, see #437). Downloads reuse the CZN rules: Microsoft host only,
   SHA-256 pinned.
8. **Environment.** Set `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS` in the prefix registry
   (`HKCU\Environment`) only if testing shows the game needs it. Epic Seven is believed
   not to need `--no-sandbox`; to be verified before any code depends on it.
9. **Report.** Plain result line. If the game still fails, offer a log capture
   (`PROTON_LOG=1`, `WINEDEBUG=+winhttp`) summarized with tokens redacted.

## Runner policy

Proton Experimental is unreliable for many of these games, so the fixer should not depend
on whatever Steam picked. Proposal:

- Nadare's tested runner (DW-Proton, the same pin the CZN backend uses) is the preferred
  runner for Steam games in the fix table.
- (Design note: as built, the installer runs with the Proton the game currently uses, so the
  prefix is changed by the same Wine the game runs under. The runner is a separate, optional
  step.)
- To make Steam launch the game with it, Nadare can set the game's entry in Steam's
  `CompatToolMapping` (`config.vdf`). That file is only safe to edit while Steam is closed,
  so Nadare either does it with Steam shut down (asked first, with a backup of the file) or
  shows the one setting the user must pick under the game's Properties, Compatibility.
- If DW-Proton is not installed, Nadare offers to install it into `compatibilitytools.d`
  (it already manages runners in `core/runners.py`).

### Getting and updating the runner

Users may start the game from Steam and never from Nadare, so nothing here assumes they
launch through Nadare.

- Install: the tested, hash-pinned runner is downloaded with the existing
  `core/runners.py` machinery into Steam's `compatibilitytools.d`, where Steam can list it.
  Steam rescans that folder on start, which the apply flow below already does.
- Pinned, not "latest": only a tested build is installed by default. A newer release is
  offered as an update, never installed silently.
- Update check: runs when Nadare is opened (`runner_info` already compares the installed
  tag with the latest release). If the user never opens Nadare they stay on the pinned
  build, which is predictable; the UI says so.
- After an update, the game's `CompatToolMapping` entry moves to the new build only when
  the switch below is on, using the same close-Steam, change, restart flow.

### "Use recommended settings" (opt-in, decided)

Each fixable game gets a switch, off by default: **Use recommended settings**. Nothing
changes until the user turns it on.

- On: Nadare applies the game's recommended set. Today that is the runner (DW-Proton) and,
  if testing shows it is needed, the prefix environment variable from the Flow section.
- Applying the runner edits Steam's `CompatToolMapping` in `config.vdf`, so Nadare asks the
  user to close Steam first (it refuses while Steam is running), copies `config.vdf` to a
  dated backup, changes only that game's entry, and reports exactly what changed.
- Steam keeps `config.vdf` in memory and rewrites it on exit, so the edit only sticks if
  Steam is closed while it happens. The button is "Apply: close Steam, change, restart
  Steam", using the existing `steam -shutdown` support. It asks first (closing Steam stops
  downloads and any running game), refuses while a Steam game is running, waits for the
  client to exit before editing, and starts Steam again afterwards. No PC reboot.
- Off again: Nadare restores the previous entry for that game (it records the old value
  first), so the switch is reversible and never touches other games.
- The switch is per game, and the fix table row lists what "recommended" means for it.

## Fix table (data, not code)

| Steam app | Game | Needs |
| --- | --- | --- |
| 5019180 | Epic Seven | WebView2 |
| 3347400 | Girls' Frontline 2: Exilium | WebView2 (to verify on the Steam build) |

Adding a game is one row.

GFL2 already gets WebView2 from Nadare's Haoplay install path for the non-Steam client
(`direct-gfl2-haoplay`), which signs in through WebView2. The Steam build is expected to
have the same sign-in dependency, so the fixer would reuse that installer logic for the
Steam prefix. This needs a check on the real Steam build before the row is trusted.
Note that Girls' Frontline (the first game, Steam app 3887700) is a different title and is
not in the table; whether it needs anything is unknown.

## Open questions

- Check automatically at launch, or only when the user presses Fix? Proposal: only on Fix.
- Is `--no-sandbox` needed at all for Epic Seven? Test with an empty launch option.
- Does `HKCU\Environment` in the prefix reach the game process under Steam's Proton?

## Test plan (tests first)

Prefix discovery, WebView2 detection, the running-game gate, the backup step and the fix
orchestration, all against fake directories and a fake runner. No real Steam or Wine.

## Notes from the Epic Seven investigation

- All STOVE calls returned HTTP 200 once WebView2 was present.
- Wine's `WinHttpCrackUrl` keeps the query in the host for `http://host?query`
  (upstream `dlls/winhttp/url.c`). It makes a lookup of `ifconfig.me?&ts=...` fail, but the
  game works anyway, so it was not the cause of -80.
- The Steam build signs in through the Steam account (`/gc/v2.0/steam/STOVE_EPIC7`), so
  Nadare cannot reuse a login there.
