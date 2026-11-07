# Sandbox (experimental)

The Sandbox runs a game with your real home folder hidden from it. It was called "Secure mode"
before b21; the name changed because it does not make a game secure, it limits what the game can
read (see "What it does not do"). It is off by default. Every game that runs a publisher's
launcher (NIKKE, NTE, the GFL2 Haoplay launcher) and every game Nadare downloads itself (the
direct games) offers it, as a switch on the game's page and as "Sandbox" under Settings, Game
options; Steam, the generic Wine game and Chaos Zero Nightmare do not yet. It needs `bubblewrap`
(the `bwrap` command); `nadare doctor` says whether it is installed. The setting is stored as
`secure_mode` in the profile (the old key, so nothing has to be migrated).

## Why

A Windows program under Wine can read the whole Linux home through the `Z:` drive or the
`\\?\unix\` path. Taking `Z:` away does not help: Proton puts it back on every launch, and the
unix path ignores drive letters. A mount namespace does work (measured, issue #804): the home is
replaced by an empty folder and only what the game needs is put back.

## What the game still sees

- its own Wine prefix and game folder (read and write), and the Proton runner (read only)
- the umu runtime (`~/.local/share/umu`, writable; the runtime's own `srt-bwrap` is read only so a
  game cannot replace the program Nadare starts it with, and Nadare only starts copies it
  recorded the first time you used the Sandbox, so a newer one a game planted is ignored; if umu
  later replaces its runtime, delete `~/.local/state/nadare/sandbox-runtime.json` to record the
  new one) and a cache folder of its own
  (`~/.cache/nadare/sandbox-cache`, for shader caches)
- the display (X11 or Wayland) and sound (PulseAudio, PipeWire)
- the list of your running programs and their command lines (see below)
- the rest of the system, as today

## What it hides

- your home folder: `~/.ssh`, browser profiles, documents, `~/.config`, `~/.local/share`,
  `~/.local/state`, `~/.cache`, Nadare's own settings and logs, and other games' folders
- other drives: `/mnt`, `/media`, `/run/media`
- the session bus, the keyring and the ssh and gpg agent sockets

## Sign-in buttons that open your browser

A game's "Sign in with Google" asks Wine to open a web link. The Steam Runtime normally passes it
to the Steam client or the desktop portal, both of which the Sandbox hides. Nadare stands in for
the Steam client's end: it reads the request from a private pipe and opens the link with your
system's `xdg-open`. Only plain `http://` and `https://` links are opened (never `file:` links
or `steam://run`), a link is size-limited, and the number and speed of links is capped. Nothing
extra needs installing. A sign-in that sends its result back through a custom link such as
`game://login` is not covered; a result sent to a local address such as `http://localhost:port`
is.

## What it does not do

- It is not a full sandbox. X11 lets a program see and type into other windows, and `/tmp`, the
  network and the GPU are shared. Wayland is safer than X11 here.
- It does not protect against kernel or graphics driver bugs.
- The game can read the command line of every program you run (`/proc/<pid>/cmdline` is public on
  Linux). If another program puts a secret in an argument, the game can read it. Nadare itself
  passes a login token to Mongil that way, and only to Mongil. The game cannot read other
  programs' files or environment through `/proc`: reading `/proc/<pid>/root` and `environ` is
  denied (tested with a planted secret file).
- Installs and updates run without it: only Play is wrapped.
- Publisher anti-cheat may refuse to run, and a launcher may fail to update itself.
- Ubuntu 24.04 and newer restrict unprivileged user namespaces; bubblewrap may need an AppArmor
  allowance there (the same issue as `docs/TESTERS.md`).
- The Flatpak version refuses to start with the Sandbox on rather than run unprotected.
