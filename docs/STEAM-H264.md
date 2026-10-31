# Steam H.264 on Linux (design)

Status: design only, no code yet. Companion to the Steam prefix fixer design (PR #716).

## Problem

On Linux, H.264 video does not play in several places until a user takes manual steps.

1. **Steam client.** Chat videos and broadcasts fail until the client is told to enable
   H.264. Valve's own protocol command for this is `steam://unlockh264/`
   (`steam steam://unlockh264/`). Per the Steam Client Beta thread "steam://unlockh264/
   shouldn't be required to play back h264 on linux systems" (Oct 2025) it persists through
   restarts and reboots. The upstream report,
   ValveSoftware/steam-for-linux#6780, has been open since 2019. The Broadcasts page runs
   the same URL the first time it is opened, other parts of the client do not.
2. **Games under Proton.** User-reported (not verified by us): some games show a white
   screen, black video or no intro movie on Valve's Proton, and play correctly on
   Proton-GE or DW-Proton, which ship the media-foundation and ffmpeg pieces. Examples from
   the user: Uma Musume (white screen), Snowbreak. This is the usual reason people use GE.

What we do not know: whether `steam://unlockh264/` changes anything for in-game video. The
pages above only describe the Steam client's own browser. This must be tested before the
feature claims it.

## Proposal

Two separate recommended settings, both opt-in and off by default, shown next to the
prefix fixer's "Use recommended settings" switch:

- **Runner for video-heavy games.** For games on a known list (see below), recommend
  DW-Proton or GE instead of Valve's Proton. Applying it uses the same close-Steam,
  change `CompatToolMapping`, restart-Steam flow as the prefix fixer. This is the fix we
  expect to matter for games.
- **Enable H.264 in the Steam client.** A single global action, not per game. It runs
  `steam steam://unlockh264/` (Steam must be running or starts). It is explained in one
  plain sentence first, and Nadare does not repeat it once done.

Nadare never installs system codec packages (distro-level Mesa/ffmpeg without H.264 needs
root). It may detect a missing system decoder and print the distro's command as a hint.

## Game list

Data, not code, same table style as the prefix fixer. Seed entries come from user reports
and each gets a status of `reported` until someone has tested it:

| Game | Symptom | Recommended runner | Status |
| --- | --- | --- | --- |
| Uma Musume | white screen | DW-Proton / GE | reported |
| Snowbreak | video / startup | DW-Proton / GE | reported |

Steam app ids are filled in only after they are looked up and confirmed.

## Open questions

- Does `steam://unlockh264/` affect any game, or only the Steam client? Test: a game that
  fails on Valve's Proton, with and without it, same runner.
- Is Nadare's pinned DW-Proton enough for these games, or is GE needed for some?
- Where does the global H.264 action live in the UI: next to the per-game switch, or in a
  Steam section of settings?

## Test plan (tests first)

The command construction, "Steam not running" handling, and the list lookup, against fake
directories and a fake `steam` binary. No real Steam, no network.
