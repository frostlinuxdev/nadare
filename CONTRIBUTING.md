# Contributing to Nadare

Thanks for helping. Nadare is an early, unofficial launcher; the most valuable
contributions right now are **new backends**, **packaging/portability fixes**,
and **honest compatibility reports** (distro, GPU, runtime versions, what broke).

By participating you agree your contributions are licensed under
`GPL-3.0-or-later`. One file is the exception: keep `src/nadare/backends/czn/auth.py`
as `GPL-3.0-only` (see [License](#license)).

## Development setup

```sh
git clone https://github.com/frostlinuxdev/nadare.git
cd nadare

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e ".[dev]"        # pytest + ruff
.venv/bin/pip install -e ".[build]"      # PyInstaller, only for packaging
.venv/bin/pip install -e ".[secure]"     # keyring, enables Remember me
```

Or use the helper (it prefers `uv` and the committed `uv.lock` when present):

```sh
./scripts/dev.sh setup      # venv + deps
./scripts/dev.sh test       # pytest
./scripts/dev.sh lint       # ruff check
./scripts/dev.sh fmt        # ruff format
./scripts/dev.sh gui        # start the GUI from source
```

Run the CLI directly while developing:

```sh
.venv/bin/python -m nadare list
.venv/bin/python -m nadare status -p czn
```

`./scripts/dev.sh stub` builds the CZN PCSDK stub with mingw-w64 (see
`scripts/build_stub.sh`).

## Repository layout

```
src/nadare/
  __main__.py         # `python -m nadare` entry
  app.py              # CLI parser + run_cli; run_gui hook
  core/               # paths, config, events, backends+registry, runners, download,
                      # journal, secrets, process, hosting, graphics, yostar, …
  backends/           # czn/, fgo/ packages; arknights.py, stellasora.py;
                      # generic wine/steam/script/waydroid
  gui/                # PyQt6 umbrella UI
packaging/            # PyInstaller spec, .desktop, Flatpak manifest, systemd
scripts/              # dev + build helpers
docs/                 # BACKENDS.md, ARTWORK.md, NEWS-SOURCES.md,
                      # ACCOUNT-SAFETY.md, TESTERS.md
```

## Code style

- **Python 3.12+**, `from __future__ import annotations`, full type hints on
  public functions.
- **Ruff** is the formatter and linter: `ruff check` and `ruff format` must be
  clean. Keep lines around 88–100 columns; match the file you are editing.
- **SPDX header on every source file.** `GPL-3.0-or-later` plus a
  `Copyright (C) <year> Nadare contributors` line. Derived files keep their
  upstream attribution and license.
- **Respect the layering.** `tests/test_layering.py` enforces it: `core/` never
  imports `backends/`, `gui/` or `app`; `backends/` never imports `gui/` or
  `app`; `gui/` never imports a concrete backend. Depend only downward.
- **No secrets in code or logs.** Mask before logging; the core logger does this
  as a safety net, not a substitute.
- **Backends never import PyQt6 or touch the UI.** They report through
  `self.ctx.emit(Event(...))` and block. The GUI runs them on a worker thread.
- **State lives under `core.paths`.** Never hard-code `~` or `/home/<user>`.
- **One place per external format.** If you parse a tool's output, keep the
  parser next to the command builder and unit-test it (see
  `backends/fgo/lepton.py`).
- **Hosting URLs live in `core/hosting.py`.** Don't scatter index/release URLs
  through backends; add an accessor there.
- Keep docs in `docs/` short and accurate. If you skip something, say so.

## Writing a backend

The full guide, with a worked skeleton, is in
[docs/BACKENDS.md](docs/BACKENDS.md). The short version:

1. Subclass `nadare.core.backends.GameBackend`.
2. Set `kind` (a registry key such as `"mygame"`).
3. Implement `status`, `install`, `update`, `launch`, `stop`; override `login`,
   `verify`, `uninstall` when meaningful.
4. Report progress with `self.ctx.emit(Event(...))`; honor `self.ctx.cancel`.
5. Expose editable profile knobs as an `options` tuple of `Option` (the Settings
   page renders them), and set `supports_login` / `supports_remember_me` /
   `account_notice` when they apply.
6. Register it in `_BUILTINS` in `core/backends.py`, or call
   `register("mygame", MyBackend)` from a plugin.
7. Add `tests/` covering the pure logic (parsers, command builders, status
   mapping) — no network needed.

### The contract at a glance

```python
from nadare.core.backends import GameBackend, Status
from nadare.core.events import Event

class MyBackend(GameBackend):
    kind = "mygame"

    def status(self) -> Status:
        """Cheap, read-only. Never mutates. Return state/version/running/message."""
        return Status(state="not_installed")

    def install(self) -> None: ...          # set up prefix/runtime + download
    def update(self) -> None: ...           # bring the game up to date
    def launch(self) -> int: ...            # return the process exit code
    def stop(self) -> None: ...             # stop a running game

    # Optional:
    # def login(self) -> None: ...
    # def verify(self) -> dict: ...
    # def uninstall(self, purge: bool = False) -> None: ...
```

Rules that the reviewer will enforce:

- `status()` must be safe to call at any time, including while the game runs.
- `launch()` and `stop()` must be idempotent enough to detect "already running".
- Raise `BackendError(message)` (and `BackendUnsupported` for operations that
  don't apply). The message is shown to the user; make it actionable.
- Raise `Cancelled` (from `nadare.core.events`) when the user cancels; do not
  swallow it.
- Never emit token values. `Event` data is visible to the GUI and can be logged.
- A profile is a dict under `config.profiles[name]`; unknown keys are preserved,
  so namespacing your options is safe. `self.ctx.set()` persists a field
  atomically.

## Tests

```sh
.venv/bin/pytest -q
```

Use pytest. Prefer pure functions and fake subprocesses; an "oracle" test that
compares parsed output against a recorded fixture is ideal. Tests must not need
a network, a display, a GPU, or a real game install (skip cleanly when a
dependency is missing). If you add a parser, add its fixture and a test. Keep
tests deterministic (freeze time; don't sleep longer than a few milliseconds).

GUI tests need PyQt6 and run headless against the offscreen Qt platform
(`QT_QPA_PLATFORM=offscreen`; the GUI test modules set it themselves and skip
when PyQt6 is absent). FGO is optional: `NADARE_WITHOUT_FGO=1` builds a bundle
with the backend excluded, and tests must not assume it is present — see
`tests/test_optional_fgo.py`.

## Packaging changes

- `packaging/nadare.spec` is the PyInstaller recipe (onedir by default); it
  honours `NADARE_WITHOUT_FGO=1` to drop `nadare.backends.fgo`.
- `scripts/build_appimage.sh` builds the AppImage with `appimagetool`
  (`packaging/AppImageBuilder.yml` is superseded and no longer used), and
  `scripts/build_deb.sh` builds the `.deb`.
- `packaging/flatpak/` and `scripts/build_flatpak.sh` build a Flatpak bundle,
  but Flatpak is deferred and would ship without FGO.
- `scripts/build_stub.sh` builds the CZN PCSDK stub with mingw-w64.

If your change affects what is bundled (a new data file, a new hidden import),
update the spec **and** the `.deb` staging in the same PR.

## Commits and pull requests

- Keep commits focused; one concern per commit.
- Write a short, imperative subject (`backend/fgo: tolerate a stopped container`).
- Describe how you tested it and on what hardware/distro, and note anything you
  did **not** test.
- Do not include tokens, logs, screenshots with QR codes, or downloaded assets.
- Run `ruff check`, `ruff format --check`, and `pytest` before pushing.

## License

Nadare is `GPL-3.0-or-later`. `src/nadare/backends/czn/auth.py` is
`GPL-3.0-only` because it derives from a work with no "or later" grant; keep its
header and do not relicense it. If you port code from another project, preserve
its license header and note the source in the docstring, and make sure the
license is GPL-compatible.
