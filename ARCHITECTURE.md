# Nadare — architecture

Nadare is a Linux launcher for anime gacha games that runs without the official
Windows vendor launcher. It is designed "for the people": one install, real
packages, safe updates, profile-per-game, and support for very different runtime
stacks (Wine/Proton, native Steam, Android-in-a-container, shell scripts).

## Games and backends

| Game | Backend kind | Runtime |
|---|---|---|
| Chaos Zero Nightmare | `stove-czn` | umu/Proton + native STOVE login + a PCSDK stub |
| Arknights (EN) | `arknights-yostar` | Proton/Wine + Yostar's own launcher/installer |
| Stella Sora (EN) | `stellasora-yostar` | Proton/Wine + a CI-signed Yostar manifest (direct download) |
| Fate/Grand Order (NA) | `fgo-lepton` | lepton-x86 Android context + Venus/virgl, via the `lepton-fgo` CLI |
| Wine / Steam / script | `wine`, `steam`, `script` | scaffolding; you supply the prefix, exe or app |
| Catalog: 13 direct-download games | `direct-<game>` | Proton/Wine prefix + the publisher's patch API |
| Catalog: 3 publisher-launcher games | `launcher-<game>` | Proton/Wine prefix + the publisher's installer/launcher |

The `direct-*` kinds are `direct-bd2`, `direct-aethergazer`, `direct-mongil`,
`direct-wuwa`, `direct-gfl2`, `direct-endfield`, `direct-pgr`, `direct-nte`,
`direct-gfl2-haoplay`, `direct-genshin`, `direct-hsr`, `direct-hi3` and
`direct-zzz`. The `launcher-*` kinds are `launcher-nikke`, `launcher-nte` and
`launcher-gfl2-haoplay`. Their catalogue rows live in
`core/directkinds.py`; see [docs/CATALOG.md](docs/CATALOG.md) for how they work
and how to add one.

`stellasora-yostar` subclasses `arknights-yostar` (`backends/arknights.py`) so the
Yostar/Wine machinery lives in one place; it adds the manifest requirement and
game-specific paths. FGO is experimental and **optional** in builds (see
[Optional FGO](#optional-fgo)).

## Knows

The design builds on projects that already work:

- **czn-linux** — native STOVE auth, dpms updater, pinned Proton runner downloads,
  the PCSDK stub, reproducible builds.
- **lepton-fgo** — the FGO stack; Nadare never reimplements it, it drives its CLI.
- **Yostar's official PC clients** — Arknights and Stella Sora are installed and
  launched through Yostar's own launcher or a signed download manifest.

## Layout

```
src/nadare/
  __main__.py           # `python -m nadare` entry
  app.py                # CLI parser, run_cli, run_gui hook
  core/
    paths.py            # XDG dirs for data/config/state/cache/logs
    events.py           # Event + Emitter; the only way backends talk to the UI
    config.py           # profiles, active profile, atomic save
    backends.py         # GameBackend ABC, BackendContext, Status, Option, registry
    runners.py          # Proton runner manager (ported from czn_runners.py)
    download.py         # resumable, verified downloads
    journal.py          # crash-safe update journal (phase machine)
    secrets.py          # OS keyring token store
    process.py          # subprocess helpers, env, running-process detection
    hosting.py          # the only place Nadare's own index/release URLs live
    graphics.py hostlibs.py sandbox.py diskspace.py vcredist.py
    news.py artwork.py i18n.py logging.py shortcuts.py updatecheck.py yostar.py
  backends/
    czn/                # stove-czn: auth.py, backend.py, runners.py, update.py,
                        # news.py, stub_src/, build_stub.sh
    fgo/                # fgo-lepton: backend.py, installer.py, lepton.py,
                        # news.py, readiness.py
    arknights.py  arknights_news.py  stellasora.py   # Yostar backends
    wine.py  steam.py  script.py         # generic backends
  gui/                  # PyQt6: main_window, library, game_view, settings,
                        # sidebar, add_game, notice, theme, workers, single_instance
packaging/              # PyInstaller spec, .desktop, Flatpak manifest, systemd
scripts/                # build + release helpers
```

## The backend contract

Every game is a `GameBackend` (`core/backends.py`). A *profile* selects one with
its `backend` key. Backends are **non-interactive**: they report through
`self.ctx.emit(Event(...))` and block; the GUI runs them on a worker thread and
passes a `Cancel` (`self.ctx.cancel`).

```python
class GameBackend(ABC):
    kind: ClassVar[str]                      # registry key, e.g. "stove-czn"

    def status(self) -> Status: ...          # cheap, safe, read-only
    def install(self) -> None: ...
    def update(self) -> None: ...
    def login(self) -> None: ...             # optional; raise BackendUnsupported
    def launch(self) -> int: ...             # process exit code
    def stop(self) -> None: ...
    def verify(self) -> dict: ...            # integrity report
    def uninstall(self, purge: bool = False) -> None: ...
```

`Status` is a dataclass: `state` (`not_installed|installed|running|error`),
`version`, `running`, `message`, and a free-form `data` dict. Class-level knobs
include `supports_login`, `supports_remember_me`, `account_notice`,
`news_source`, and `options` — `Option` tuples the Settings page renders as the
"Game options" card.

### Rules

1. **Backends never touch the UI.** They call `self.ctx.emit(Event(...))`.
2. **No secrets in logs.** Tokens are masked to first/last 4 chars.
3. **State lives under `core.paths`** — never a hard-coded `~`.
4. **Derived code keeps its licence header.** `backends/czn/auth.py` is GPL-3.0-only
   (from yporoc via czn-linux); every other file is offered as GPL-3.0-or-later, but
   the combined work is GPL-3.0-only (see [docs/AI.md](docs/AI.md)).
5. **Nadare drives external installers, it does not fork them.** For FGO that is
   `lepton-fgo`; for STOVE games it is `czn_auth.py`; for the Yostar games it is
   Yostar's own launcher or a signed manifest. If the tool grows a `--json` mode,
   use it; otherwise parse its documented text output in one place.

## Layering

`tests/test_layering.py` enforces the import direction:

- `core/` never imports `nadare.backends`, `nadare.gui` or `nadare.app`.
- `backends/` never import `nadare.gui` or `nadare.app`.
- `gui/` never imports a concrete backend (`nadare.backends`).

Everything flows `core ← backends ← gui/app`. The registry resolves backends
lazily, so a missing optional dependency (PyQt6, podman) never breaks unrelated
modules.

## Backend registry

`core/backends.py` maps each kind to a `"module:Class"` target in `_BUILTINS`
and imports it lazily. Hand-written backends are listed there directly; the
catalog's `direct-*` and `launcher-*` kinds come from `CATALOG_BACKENDS` in
`core/directkinds.py`, which is merged into `_BUILTINS`. Add a hand-written
backend to `_BUILTINS`; add a catalog game to `core/directkinds.py` (see
[docs/CATALOG.md](docs/CATALOG.md)). `load_builtins()` resolves them,
`is_available(kind)` reports whether one loaded, and `unavailable()` lists
built-ins whose import failed — for example a backend left out of a bundle.
Unknown or unavailable kinds degrade gracefully rather than crashing the app.

## Hosting

Every URL that points at Nadare's own published data lives only in
`core/hosting.py`: the index base and the latest-release endpoint, plus the
derived CZN/WebView2/VC-redist/Yostar index URLs. Each is overridable through
the environment (`NADARE_INDEX_BASE`, `NADARE_RELEASES_API`), so moving hosts is
a one-file change. Asset hashes travel inside the fetched manifests and manifest
signing is not implemented yet, so integrity currently rests on TLS to that
host.

## Optional FGO

The FGO backend is optional. When `NADARE_WITHOUT_FGO=1`, `packaging/nadare.spec`
drops `nadare.backends.fgo` and its hidden imports so a bundle without the
podman/lepton stack never ships it. The registry then reports it through
`unavailable()`, and `nadare doctor` prints "FGO support is not included in this
build." when the backend cannot be imported. Flatpak (below) is the intended
FGO-free target.

## Packaging

- `packaging/nadare.spec` is the PyInstaller onedir recipe (onedir by default).
- `scripts/build_appimage.sh` builds the AppImage with `appimagetool`.
- `scripts/build_deb.sh` builds the `.deb`.
- `packaging/flatpak/` holds a Flatpak manifest and `scripts/build_flatpak.sh`
  builds it, but **Flatpak is deferred** and a Flatpak/Flathub build would ship
  **without FGO**.

## Data layout

```
~/.local/share/nadare/          # data: runners/, profiles/<name>/ (trees, prefixes, stubs)
~/.config/nadare/config.json    # profiles + settings
~/.local/state/nadare/          # logs/, journals, update receipts
~/.cache/nadare/                # downloads, WebView2, backgrounds
```

## Threading model

The GUI owns the main thread. `gui/workers.py` runs one backend operation at a
time per profile on a `QThread`, forwarding `Event`s to the Qt signal bus.
Nadare never runs two mutating operations on the same profile concurrently; it
takes a per-profile lock (see `core/process.py`).

## Why Python

`curl_cffi`'s Chrome TLS impersonation is required for STOVE's CDN, and the CZN
auth path is already proven in it. Rust would risk the one thing that works, and
packaging is a solved Python problem. A future
engine/UI split can move hot paths to Rust without a rewrite.
