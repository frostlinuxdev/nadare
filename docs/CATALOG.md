# The game catalog (experimental)

Nadare can add games beyond its hand-built backends in two ways:

| Kind | How the game is installed | Example |
|---|---|---|
| `direct-<game>` | Straight from the publisher's public patch API. No launcher. | Brown Dust 2, Wuthering Waves, GFL2 |
| `launcher-<game>` | The publisher's own installer and launcher run under DW-Proton. | NIKKE, Neverness to Everness |

Every catalog game carries a support tier, shown on its card in the **Store** (the first item in the sidebar; search it and filter by tier) and again on its page:

| Tier | Meaning |
|---|---|
| **Verified** | A maintainer installed it through Nadare and played it past the title screen on a real machine. Currently: Brown Dust 2, Aether Gazer, Punishing: Gray Raven, Girls' Frontline 2: Exilium (Darkwinter build), Girls' Frontline 2 (Haoplay global build), Honkai Impact 3rd, Genshin Impact, Honkai: Star Rail, Zenless Zone Zero, Wuthering Waves, NIKKE: Goddess of Victory, Arknights: Endfield, Neverness to Everness (the direct install) and Mongil: Star Dive. |
| **Experimental** | The install path is covered by tests and fixtures, but nobody has played it end to end yet, or it needs a workaround (an anti-cheat, a publisher launcher, a login). It can break when a publisher changes something. |

The HoYo games (Genshin Impact, Honkai: Star Rail, Honkai Impact 3rd, Zenless Zone Zero) are offered
like the others. All four are Verified and install through HoYo's chunked "Sophon" downloads.
`NADARE_HIDE_HOYO=1` hides all four from the Store and the Add Game dialog.
When a Sophon update changes a file, Nadare stages its verified replacement beside the installed
file before swapping it in; allow free space for the largest changed file during an update.

HoYoverse titles start on Linux because DW-Proton carries the fixes their start-up sequence
expects; it selects them from the game's exe name. Nadare's default runner is DW-Proton; another
runner (GE-Proton, plain Proton-CachyOS) can fail that start-up sequence. Honkai: Star Rail's
`StarRail.exe` is not in DW-Proton's list, so it gets none of these fixes.

A **Community** tier for reviewed third-party entries is planned but has no entries yet. The
tier of a kind is one line in `nadare/core/directkinds.py` (`VERIFIED_KINDS`).

## How a `direct-<game>` works

```
publisher API  ->  nadare.core.sources.<game>.resolve()  ->  directdl.Plan  ->  directdl.sync()
```

* `nadare/core/sources/<game>.py` asks the publisher's **unauthenticated** API what the
  latest build is and returns a `directdl.Plan`: a list of `files` (URL, size, md5/crc32/sha256)
  and/or `archives` (one zip, a zip inside a zip, or a zip split into ordered parts).
* `nadare/core/directdl.py` installs a plan: resumable downloads, hash and size checks, safe
  extraction (no `..`, no symlinks, nothing half-extracted), idempotent re-runs. Split zips
  are read as one virtual file, so they never take twice the disk.
* An archive is unwrapped in one of four ways (`Archive.unwrap`): `("zip",)`, `("zip", "zip")`
  (a zip holding one zip, as Brown Dust 2 ships), `("7z",)` (older HoYo archives, via
  `nadare/core/sevenz.py`, a streaming 7z reader on the standard `lzma` module) and
  `("slices",)` (small files cut out of a downloaded pack by offset and size, as Neverness to
  Everness ships). Any of them can be split into ordered `parts`, which are read as one virtual
  file and never joined on disk.
* `nadare/backends/direct.py` is a thin class per game on top of the Wine/Proton machinery
  that Arknights and Stella Sora already share.

Streamed archives can also be verified against published per-block checksums: see [ARCHIVE-PINS.md](ARCHIVE-PINS.md).

### Adding a direct game

1. **Find the endpoints** (see "Finding the endpoints" below) and record the responses you
   rely on in `tests/fixtures/catalog/`.
2. **Write the source** in `nadare/core/sources/<game>.py`: `resolve(fetch=None) -> Plan`.
   Validate every remote field. Use `directdl.safe_relative_path` on anything that becomes a
   path, accept only `https://` URLs, and raise `directdl.DirectError` (never `KeyError`) for
   a bad response. A manifest format that needs different path rules gets a `RelativePolicy`
   in `core/paths.py` instead of a new validator.
3. **Write tests** in `tests/test_catalog_sources.py` against the recorded fixtures, with a
   fake `fetch`. No test touches the network.
4. **Register the game**: one row in `nadare/core/directkinds.py` and one small class in
   `nadare/backends/direct.py` (exe name, install folder, `plan_factory`).
   `tests/test_direct_backend.py::test_kind_table_matches_the_classes` keeps them in sync.

### Manifests that are wrapped

A few publishers wrap their file list. Neverness to Everness's `ResList.bin` is an AES-CBC
wrapper around zlib-compressed XML; `core/sources/nte.py` unwraps it with the small AES decryptor
in `core/sources/_aes.py`. Nadare ships no crypto library. How a given wrapper is opened is a
per-game project decision; keep research notes out of this repository.

## How a `launcher-<game>` works

`nadare/backends/launcher.py`: a class per game that sets the installer URL, the hosts it may
come from, and the launcher exe. Nadare downloads the installer (only from those hosts, or any
host if you pin `installer_sha256`), runs it in the game's prefix, then runs the installed
launcher. The launcher downloads and updates the game.

## Finding the endpoints

Most launchers talk to an open JSON API. When adding a source, use a public, unauthenticated
endpoint. If you have to capture a launcher's traffic to find one, keep that work and its notes
outside this repository and send the result — the endpoint, the parameters, and whether it needs
a signed header — in the pull request.

Two practical notes that are not about capturing traffic:

* Installers often default to a drive that Wine maps to your home folder (`S:`, `X:`). Pick a
  folder on `C:`.
* To check that a game really starts on the real GPU without opening a window on your desktop:
  `WLR_BACKENDS=headless cage -s -- umu-run Game.exe`, then `grim` on cage's socket.

## Rules

* A source reads public, unauthenticated endpoints. It never logs in, and never needs a
  credential from the user's account.
* A source must never write outside the install folder.
* Anything beyond that (for example a publisher whose manifest is encrypted) is a project
  decision; note it in the pull request rather than working around it quietly.

## Keeping it working (maintainers)

`scripts/refresh_index.sh` runs on the index refresh nodes and turns its heartbeat "down" with a
`CANARY:` message when something needs a person:

* a new Proton runner needs a Steam Runtime the pinned umu lacks (`check_runtime_canary.py`);
* a Kuro launcher token (PGR, Wuthering Waves) was refused or is malformed
  (`check_kuro_tokens.py`; it checks `catalog/kuro-tokens.json` in the index, else the built-in
  tokens). To rotate one without a release, publish the new `50004_<token>` / `50015_<token>` in
  that file; Nadare reads it before its built-in value;
* the pinned NTE installer changed size or vanished, or the Endfield installer now comes from a
  host Nadare does not allow (`check_installer_pins.py`);
* with the catalog on (`NADARE_INDEX_CATALOG=1`), a game's source failed three catalog runs in a
  row (`publish_index.py`).
