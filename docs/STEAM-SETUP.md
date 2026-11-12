# Steam Set up (what Nadare does for a game that Steam owns)

Status: built for b21 (`core/steamsetup.py` planner, `steamrun`/`steamglue` actions, the Set up
window, and Store tiles for the verified games). Not published yet.
Supersedes the open questions in `STEAM-PREFIX-FIX.md` and `STEAM-H264.md`, which stay as the
original designs.

## What it is

Steam stays the downloader and owner of the game. Nadare adds **one "Set up" button** per Steam
game. It shows a checklist and does the first step that is still to do, least invasive first:

1. **Installed in Steam.** If not, Nadare asks Steam to install it (`steam://install/<id>`);
   Steam shows its own dialog and does the download. Nadare does not install Steam itself.
2. **H.264 video unlocked** (only for games whose problem is their opening video).
   `steam steam://unlockh264/`. Silent, global, kept by Steam, no restart.
3. **What the game's Proton prefix is missing** (WebView2 and/or Visual C++). Backup of the
   prefix first, asked beforehand; refused while the game or its wineserver runs.
4. **A better runner (DW-Proton; for Haoplay's GFL2, GE-Proton)**. *Recommended* on a Vulkan older than 1.3, otherwise
   *optional*. DW-Proton ships DXVK-Sarek next to DXVK, and its `proton` script switches to
   Sarek by itself when the primary GPU lacks Vulkan 1.3 (read from the 11.0-15 script); Steam's
   default Proton has no such switch. The Vulkan version is only trusted when `vulkaninfo`
   reported it, never the loader's. The only step that edits Steam's `config.vdf`
   (`CompatToolMapping`). It shows a warning first, needs Steam closed, backs the file up,
   changes only that game's entry and can be undone. A merely optional runner never blocks
   "ready".

5. **Native Wayland** (only on a Wayland session). Valve's own Proton ships only Wine's X11
   driver, so its games run through XWayland, which flickers in some windows. DW-Proton also
   ships `winewayland.so` and switches to it when the game's Steam launch option has
   `PROTON_ENABLE_WAYLAND=1`. The step adds `PROTON_ENABLE_WAYLAND=1` to that game's launch
   options in the signed-in account's `localconfig.vdf` (what the player already wrote stays),
   with Steam closed, a dated backup, and an Undo. It is offered only when the game's runner has
   the driver (otherwise it says to use the DW-Proton step first). Known side effects, from the
   ProtonPlus documentation: the Steam overlay may not appear and Steam Input (controllers) can
   misbehave under native Wayland. By hand: Properties, General, Launch options,
   `PROTON_ENABLE_WAYLAND=1 %command%`.

Everything it changes lives in Steam's own files, so it keeps working if Nadare is not
running, and each fix can also be done by hand (see below).

## Evidence (reproduced on a real Steam, 2026-10-31)

Test machine: a Wayland desktop VM with software Vulkan (llvmpipe), Steam native `.deb`,
Proton Experimental, no launch options, an ordinary account that owns the games.

| Game | App id | Result |
| --- | --- | --- |
| Epic Seven | 5019180 | Fresh install shows `STOVE ERROR Cmd:STOVE_INITIALIZE Code:-80`. The prefix has no `Program Files (x86)/Microsoft/EdgeWebView`. Installing the WebView2 Runtime into that prefix (Proton's `run`, `/silent /install`, with a display) fixes it: the server list loads with the account's existing characters. No `--no-sandbox` and no registry variable needed. |
| Umamusume: Pretty Derby | 3224770 | Opens a plain white window that stays white for 3+ minutes (the opening video; the Proton log shows the H.264 decoder loading and then silence). After `steam steam://unlockh264/` (Steam writes `"UnlockedH264Feature" "1"` into the account's `localconfig.vdf`), same runner, no restart: the title screen "TAP TO START" appears. The unlock alone is the fix; no DW-Proton needed. |
| Blue Archive | 3557620 | 25.8 GB install; first launch on Proton Experimental starts normally, runs "Data Preprocessing" and reaches the game's own birthday prompt (no crash, no white screen). The H.264 unlock was already on in the test, so a launch without it was not tested. DW-Proton is only an option here. |
| Snowbreak: Containment Zone | 2668080 | Not tested (50 GB download). Reported white screen or video trouble; expected to follow Uma Musume. |
| Girls' Frontline 2: Exilium (Darkwinter) | 3347400 | `steam://install` failed with `No licenses` on a region-locked build. A player reports its sign-in does not use WebView2. Not verified. |
| Girls' Frontline 2: Exilium (Haoplay) | 3308670 | Installs and starts, then asks for an 87.5 GB in-game download (done through the game's own updater). Its login SDK `PnSDKCPP.dll` (`haoplay-pcsdk-cpp`) imports `WebView2Loader.dll` and creates a WebView2 browser, so Set up installs WebView2 into the prefix. **Sign-in verified on 2026-10-31:** with **GE-Proton11-7**, native Wayland and the launch options `PROTON_ENABLE_WAYLAND=1 UMU_ID=0`, the HAOPLAY sign-in panel (Facebook, Google, Apple, Email) renders inside the game window and takes clicks (WebView2 156.0.4314.58, real AMD GPU). With **DW-Proton 11.0-15** the game froze at the account screen; under XWayland the page flickers. The difference is most likely the Wine build: GE-Proton 11-6 and 11-7 carry a Wine-Wayland series for cross-process child-window rendering (their 11-7 notes name a WebView2 login UI), which DW-Proton's CachyOS Wine lacks; DW-Proton 11.0-15 also ships DXVK 3.1.1 (known to hang on its first present on Wayland), GE-Proton11-7 ships DXVK 3.1. The two causes were not separated. Set up therefore offers **GE-Proton** for this game. Nothing past the sign-in is tested. |

### WebView2 installer pin

The pinned checksum in `backends/czn/backend.py` (`2a6add76…`) is stale: Microsoft replaced the
installer behind `https://go.microsoft.com/fwlink/?linkid=2124701`. The current file
(215,017,168 bytes, sha256 `62863f0f…951249b`) is the first entry of the Nadare index list
`webview2/latest.json`, which is what the fixer must verify against. Never skip verification.

## Doing it by hand (no Nadare)

- **Video games (white window at the start):** in a terminal run `steam steam://unlockh264/`
  (Steam must be running). It persists across restarts.
- **Epic Seven "STOVE ERROR -80":** install Microsoft's *WebView2 Runtime* (Evergreen
  installer, x64) into the game's prefix `…/steamapps/compatdata/5019180/pfx` with the
  same Proton the game uses, for example
  `STEAM_COMPAT_DATA_PATH=…/compatdata/5019180 STEAM_COMPAT_CLIENT_INSTALL_PATH=<Steam folder> "…/Proton - Experimental/proton" run installer.exe /silent /install`
  (needs a display). Close the game first and back up `compatdata/5019180` before you start.
- **A different runner:** in Steam, the game's Properties, Compatibility, tick "Force the use
  of a specific Steam Play compatibility tool" and pick DW-Proton (it must be in
  `~/.local/share/Steam/compatibilitytools.d`).

## Rules the code keeps

- Nothing changes before the user confirms; the confirm text names what changes and how to undo it.
- No credentials, no Steam login handling, no `localconfig.vdf` or `config.vdf` writes except the
  one `CompatToolMapping` entry described above.
- Steam not installed: Set up says so and points to Steam's own download; Nadare does not install Steam.
- Fix definitions are data in the app (`GAMES` in `core/steamsetup.py`): a fixed set of
  actions, never free-form commands.

## Blue Archive

Reached the in-game birthday prompt on Proton Experimental without any change (software Vulkan,
`BlueArchive.exe`). The prompt asks for a date of birth; the test did not enter one. Whether it
needs the H.264 unlock was not isolated because the unlock was already on.
