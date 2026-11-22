# Sandbox

The Sandbox runs a game with your real home folder hidden from it. It was called "Secure mode"
before b21; the name changed because it does not make a game secure, it limits what the game can
read (see "What it does not do"). It is on by default for every game that offers it, except Chaos
Zero Nightmare, which is off until you switch it on. Every game that runs a publisher's
launcher (NIKKE, NTE, the GFL2 Haoplay launcher) and every game Nadare downloads itself (the
direct games) offers it, as a switch on the game's page and as "Sandbox" under Settings, Game
options; Chaos Zero Nightmare offers it too, off by default. Steam does
not yet. It needs `bubblewrap`
(the `bwrap` command); `nadare doctor` says whether it is installed. The setting is stored as
`secure_mode` in the profile (the old key, so nothing has to be migrated).

## On by default

A game you never set it for (no `secure_mode` in its profile) plays in the Sandbox, unless its
kind is off by default (Chaos Zero Nightmare). Turning the
switch off saves `secure_mode: false` and that game stays off. When the Sandbox is on only by
default and cannot run on this computer (no bubblewrap yet, or bubblewrap blocked), the game
still starts, without the Sandbox, and the game's log says why; the page then shows no "SANDBOX"
pill. When you turned it on yourself, those situations stop the launch with that message instead,
so you are never unprotected without being told.

A game whose folder is too broad to hide your home from (the home folder itself, a folder holding
it, a whole drive) never starts unsandboxed, whether the Sandbox is on by default or switched on:
the launch stops and says to move the game into a library or turn the Sandbox off for that game.
NIKKE's launcher and game live inside its Wine prefix, so the Sandbox opens only that prefix.

## Why

A Windows program under Wine can read the whole Linux home through the `Z:` drive or the
`\\?\unix\` path. Taking `Z:` away does not help: Proton puts it back on every launch, and the
unix path ignores drive letters. A mount namespace does work (measured, issue #804): the home is
replaced by an empty folder and only what the game needs is put back.

## What the game still sees

- its own Wine prefix and game folder (read and write), and the Proton runner (read only)
- for Chaos Zero Nightmare: the launcher stub and the one-time session file the launch hands the
  game (read only). Every process that launch starts (the stub, the game and WebView2) runs in
  the same Sandbox; its launch log is written inside the prefix so Nadare can still read it.
  With Sandbox on, a missing Visual C++ runtime needs Repair files before Play; its installer
  runs during Install or Repair, outside the Sandbox. QR sign-in uses Nadare's Python helper
  outside the Sandbox and does not start Wine
- the umu runtime (`~/.local/share/umu`, writable; the runtime's own `srt-bwrap` is read only so a
  game cannot replace the program Nadare starts it with, and Nadare only starts copies it
  recorded the first time you used the Sandbox, so a newer one a game planted is ignored; if umu
  later replaces its runtime, delete `~/.local/state/nadare/sandbox-runtime.json` to record the
  new one) and a cache folder of its own
  (`~/.cache/nadare/sandbox-cache`), which holds the shader caches: the shared Mesa cache at
  `shader-cache/mesa` and each game's VKD3D cache at `shader-cache/games/<profile>/vkd3d`.
  DXVK's cache stays in the game's prefix (`drive_c/users/<user>/AppData/Local/dxvk`)
- the display (X11 or Wayland) and sound (PulseAudio, PipeWire)
- its own temporary folder (`/tmp`) and its own process list. Of the host's `/tmp` it gets
  only the X11 socket folder (read only, so the display still connects) and the X authority
  file when that lives there
- the rest of the system, as today

## What it hides

- your home folder: `~/.ssh`, browser profiles, documents, `~/.config`, `~/.local/share`,
  `~/.local/state`, `~/.cache`, Nadare's own settings and logs, and other games' folders
- other drives: `/mnt`, `/media`, `/run/media`
- the host's `/tmp` and the list of your other running programs and their command lines
- the session bus, the keyring and the ssh and gpg agent sockets

The game still runs umu, pressure-vessel and Wine inside its own namespace, so their
temporary trees (`/tmp/pressure-vessel-*`) and Wine's `.wine-UID` server directory are in the
same private `/tmp` and process list as the game: the server and the game always meet.

If one game cannot start with the private `/tmp` or process list, its profile can give back
that one thing without turning the Sandbox off for every game: `sandbox_host_tmp` or
`sandbox_share_pid` (see `CONFIG-ONLY-OPTIONS.md`).

## Sign-in buttons that open your browser

A game's "Sign in with Google" asks Wine to open a web link. The Steam Runtime normally passes it
to the Steam client or the desktop portal, both of which the Sandbox hides. Nadare stands in for
the Steam client's end: it reads the request from a private pipe and opens the link with your
system's `xdg-open`. Only plain `http://` and `https://` links are opened (never `file:` links
or `steam://run`), a link is size-limited, and the number and speed of links is capped. Nothing
extra needs installing. A sign-in that sends its result back through a custom link such as
`game://login` is not covered; a result sent to a local address such as `http://localhost:port`
is.

## In the Flatpak

Flatpak has its own sandbox, so the Flatpak version does not need bubblewrap. Nadare starts the
game with `flatpak-spawn --sandbox`: the game gets an empty home folder and no `/mnt`, and only
the folders it needs are exposed (the prefix, game folder and cache writable; the Proton runner
and files Nadare downloaded read only). The display, sound, GPU and a filtered session bus are
shared. The filtered bus reaches the desktop portals, so a "Sign in with Google" link opens in
your browser directly; Nadare's own link relay is not used there. The game stays in Nadare's
process namespace, so Stop still works and it exits when Nadare exits; its `/tmp` is the
Flatpak app's own, not the host's. `flatpak-spawn` cannot add the bubblewrap flags below to
that sub-sandbox, so the private `/tmp` and process list described here are the bubblewrap
Sandbox's. If `flatpak-spawn` is missing, `nadare doctor` says so.

## What it does not do

- It is not a full sandbox. X11 lets a program see and type into other windows, and the network
  and the GPU are shared. Wayland is safer than X11 here.
- It does not protect against kernel or graphics driver bugs.
- The game's own process list keeps other programs' command lines out of its `/proc`. In the
  Flatpak, which shares Nadare's process list so Stop can find the game, the game can read the
  command line of every program you run (`/proc/<pid>/cmdline` is public on Linux); if another
  program puts a secret in an argument, the game can read it there. Nadare itself passes a login
  token to Mongil that way, and only to Mongil. The game cannot read other programs' files or
  environment through `/proc`: reading `/proc/<pid>/root` and `environ` is denied (tested with a
  planted secret file).
- A publisher's installer (NIKKE, NTE, the Haoplay launcher) runs in the Sandbox too. Files Nadare downloads itself (the direct games) are written by Nadare, not by publisher code, so that step needs no Sandbox. A launcher that updates the game does so while it runs under Play, inside the Sandbox.
- Publisher anti-cheat may refuse to run, and a launcher may fail to update itself.
- Ubuntu 24.04 and newer restrict unprivileged user namespaces; bubblewrap may need an AppArmor
  allowance there (the same issue as `docs/TESTERS.md`).
