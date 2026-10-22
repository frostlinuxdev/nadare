# Adding a game backend

This is the contributor guide for teaching Nadare a new game or runtime. It
assumes you have read [ARCHITECTURE.md](../ARCHITECTURE.md) and the contract in
`src/nadare/core/backends.py`.

A backend is the **only** thing that knows how one game is installed, updated,
logged into and launched. The CLI and GUI are generic: they resolve a profile,
build the backend, and stream its `Event`s. If your backend blocks, reports
progress and cleans up after itself, it will work in both front ends with no UI
code.

## The contract

```python
class GameBackend(ABC):
    kind: ClassVar[str] = ""                 # registry key, e.g. "mygame"

    def __init__(self, ctx: BackendContext) -> None: ...

    @abstractmethod
    def status(self) -> Status: ...           # cheap, safe, read-only
    @abstractmethod
    def install(self) -> None: ...            # set up prefix/runtime + download
    @abstractmethod
    def update(self) -> None: ...             # bring the game up to date
    @abstractmethod
    def launch(self) -> int: ...              # run the game; return exit code
    @abstractmethod
    def stop(self) -> None: ...               # stop this profile's game

    # Optional; provide only where meaningful:
    def login(self) -> None: ...              # raise BackendUnsupported if N/A
    def verify(self) -> dict[str, Any]: ...   # integrity report
    def uninstall(self, purge: bool = False) -> None: ...
```

`Status` is the dataclass backends return from `status()`:

```python
Status(state="not_installed",   # not_installed | installed | running | error
       version=None,            # user-facing version string, e.g. "2.93.0"
       running=False,
       message="",              # one short human sentence
       data={})                 # backend-specific, JSON-serializable
```

`BackendContext` is everything a backend is allowed to know:

| Member | Meaning |
|---|---|
| `ctx.name` | profile name |
| `ctx.profile` | the profile dict (`config.profiles[name]`) |
| `ctx.config` | the loaded `Config` (`config.save()` persists) |
| `ctx.emit(event)` | the **only** way to report to the UI |
| `ctx.cancel` | cooperative `Cancel`; check `cancelled`, call `raise_if_cancelled()` |
| `ctx.data_dir()` | `~/.local/share/nadare/profiles/<name>/` |
| `ctx.profile_dir(*parts)` | a created subdirectory of the above |
| `ctx.set(key, value)` | persist one profile field atomically |

### The rules

1. **Never touch the UI.** No PyQt6, no printing, no `input()`.
2. **Report through `emit`.** `Event(kind, message, progress, data)` where
   `kind` is one of `status`, `step`, `progress`, `log`, `done`, `error`.
   `progress` is `0.0..1.0` when known.
3. **No secrets in events or logs.** Mask before emitting; the core logger masks
   as a backstop. Tokens must never end up in `Status.data`.
4. **State lives under `core.paths`.** Never hard-code `~` or a user path.
5. **Honor cancellation.** Long loops should call `ctx.cancel.raise_if_cancelled()`
   and catch `Cancelled` at the right boundary.
6. **Raise `BackendError`** with an actionable message. Use `BackendUnsupported`
   for operations that don't apply. The CLI maps these to exit codes.
7. **Never run two mutating operations on one profile.** The GUI/lock layer
   enforces this, but `status()` must still be safe while a game runs.
8. **`launch()` returns the process exit code.** `stop()` must be safe to call
   when nothing is running.

## Worked skeleton

Say we are adding *Example Quest* (`mygame`), a Windows game that already has an
installer CLI called `example-quest-cli`. The backend wraps that CLI. Put it at
`src/nadare/backends/mygame/backend.py` with an `__init__.py` that re-exports the
class:

```python
# src/nadare/backends/mygame/__init__.py
from .backend import MyGameBackend

__all__ = ["MyGameBackend"]
```

```python
# src/nadare/backends/mygame/backend.py
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""``mygame`` backend: drives the ``example-quest-cli`` tool.

Profile keys (all optional unless noted):

``install_dir``
    where the game lives (default: the profile data dir).
``cli``
    path to ``example-quest-cli`` (default: found on PATH).
``region``
    account region; only ``global`` is supported today.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ...core.backends import BackendError, BackendUnsupported, GameBackend, Status
from ...core.events import Cancelled, Event, mask


class MyGameBackend(GameBackend):
    kind = "mygame"

    # ---- small helpers -----------------------------------------------------
    def _cli(self) -> str:
        """Locate the external tool once; fail with a useful message."""
        configured = str(self.ctx.profile.get("cli") or "").strip()
        found = shutil.which(configured) if configured else shutil.which(
            "example-quest-cli")
        if not found:
            raise BackendError(
                "example-quest-cli not found; install it or set 'cli' in the profile")
        return found

    def _install_dir(self) -> Path:
        raw = str(self.ctx.profile.get("install_dir") or "").strip()
        return Path(os.path.expanduser(raw)) if raw else self.ctx.data_dir() / "game"

    def _game_exe(self) -> Path | None:
        exe = self._install_dir() / "ExampleQuest.exe"
        return exe if exe.is_file() else None

    def _run(self, args: list[str], *, stream: bool = True) -> int:
        """Run the CLI, forwarding merged output as masked log lines."""
        self.ctx.emit(Event("log", "$ " + " ".join(args)))
        try:
            proc = subprocess.Popen(
                args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, errors="replace",
                bufsize=1, start_new_session=True,
            )
        except OSError as exc:
            raise BackendError(f"could not start {args[0]}: {exc}") from exc
        assert proc.stdout is not None
        try:
            for raw in proc.stdout:
                if self.ctx.cancel.cancelled:
                    proc.terminate()
                    raise Cancelled()
                if stream:
                    self.ctx.emit(Event("log", mask(raw.rstrip("\n"))))
        finally:
            proc.stdout.close()
        return proc.wait()

    # ---- contract ----------------------------------------------------------
    def status(self) -> Status:
        try:
            cli = self._cli()
        except BackendError as exc:
            return Status(state="not_installed", message=str(exc))
        exe = self._game_exe()
        # A real backend would also scan /proc for the running game here.
        if exe is None:
            return Status(state="not_installed", message="game files not found")
        return Status(state="installed", version=None, running=False,
                      message="installed", data={"exe": str(exe), "cli": cli})

    def install(self) -> None:
        self.ctx.cancel.raise_if_cancelled()
        self.ctx.emit(Event("step", "installing Example Quest"))
        code = self._run([self._cli(), "install", "--dir", str(self._install_dir())])
        if code != 0:
            raise BackendError(f"installer failed (exit {code})", code=code)
        self.ctx.emit(Event("done", "Example Quest is installed"))

    def update(self) -> None:
        self.ctx.cancel.raise_if_cancelled()
        self.ctx.emit(Event("step", "updating Example Quest"))
        code = self._run([self._cli(), "update", str(self._install_dir())])
        if code != 0:
            raise BackendError(f"update failed (exit {code})", code=code)
        self.ctx.emit(Event("done", "game is up to date"))

    def login(self) -> None:
        # If the tool has a QR/login verb, drive it and surface the image path
        # in Event.data (never the token):
        #   self.ctx.emit(Event("step", "scan the QR", data={"png": str(png)}))
        raise BackendUnsupported("mygame has no separate login step")

    def launch(self) -> int:
        self.ctx.cancel.raise_if_cancelled()
        exe = self._game_exe()
        if exe is None:
            raise BackendError("game is not installed")
        self.ctx.emit(Event("step", f"launching {exe.name}"))
        # Reuse the core Proton helpers if this is a Windows game:
        #   from ..wine import _stream
        return self._run([self._cli(), "run", str(exe)])

    def stop(self) -> None:
        # Kill only processes that belong to *this* profile (match the prefix),
        # never every process whose name looks similar.
        try:
            self._run([self._cli(), "stop", str(self._install_dir())], stream=False)
        except BackendError:
            self.ctx.emit(Event("log", "nothing to stop"))

    def verify(self) -> dict[str, Any]:
        exe = self._game_exe()
        return {"verified": exe is not None, "exe": str(exe) if exe else ""}

    def uninstall(self, purge: bool = False) -> None:
        shutil.rmtree(self._install_dir(), ignore_errors=True)
        if purge:
            shutil.rmtree(self.ctx.data_dir(), ignore_errors=True)
        self.ctx.emit(Event("done", "uninstalled"))
```

## Registering the backend

Add the kind and its import path to `_BUILTINS` in
`src/nadare/core/backends.py`:

```python
_BUILTINS = {
    "mygame": "nadare.backends.mygame:MyGameBackend",
    # ...
}
```

Imports are lazy and failures are swallowed, so a backend with a missing
dependency simply is not offered — never crash the registry for everyone else.
If the backend is a third-party plugin, it can call
`nadare.core.backends.register("mygame", MyGameBackend)` at import time instead.

## Profile options

A profile is a free-form dict. Shared fields (`backend`, `title`, `install_dir`,
`prefix`, `runner`, `launch_mode`, `extra`) have defaults in
`core/config.py`; per-game fields live wherever you want, but the convention is
top level for common options and under `extra` for less common ones (the generic
Wine backend reads both — see `_setting()` in `backends/wine.py`).

```json
{
  "profiles": {
    "eq": {
      "backend": "mygame",
      "title": "Example Quest",
      "install_dir": "~/Games/example-quest",
      "cli": "/usr/local/bin/example-quest-cli",
      "extra": { "region": "global" }
    }
  }
}
```

Persist anything you resolve (a downloaded runner, a chosen prefix) with
`self.ctx.set("prefix", str(path))` so the next run is cheap.

## Yostar direct-download backends (`arknights-yostar`, `stellasora-yostar`)

Yostar's launcher API is signed with a per-game salt. That salt cannot ship in
this GPL client, so a scheduled CI job (see `ci/yostar-manifests/`) signs the
two launcher API calls, fetches the public game-file list, and publishes plain
manifests; the backends then download the listed files directly from the public
CDN.

* `arknights-yostar` runs in two modes: **official** (the Yostar installer runs
the launcher, which then downloads the game) and **direct** (the manifest is
synced into `install_dir` and `Arknights.exe` is launched). Direct mode is
selected whenever a manifest is configured.
* `stellasora-yostar` is **direct only**. It uses Nadare's published manifest
index by default, and errors with an actionable message when that index is
disabled and no manifest is configured.

Manifest configuration (environment wins over the profile):

| Source | Meaning |
|---|---|
| `YOSTAR_MANIFEST_INDEX` env | a URL to a manifest index or a per-game manifest |
| profile `manifest_index` | same, stored in the profile |
| profile `manifest_file` | a local manifest path (testing / offline) |

A manifest index maps a game tag to `{"url", "version", "sha256"}`; the client
resolves the entry for its own `game_tag`. The per-game manifest lists files as
`{"path", "size", "crc64"}` (CRC-64/XZ, decimal or hex). Paths are always
treated as relative to `install_dir`; absolute or `..` paths are rejected.
Downloads only come from `*.yo-star.com` or the manifest's own base host, use
the resumable `.part` machinery in `core/download.py`, and are verified against
the manifest CRC before they are kept. `status()` reports `data["mode"]` as
`"direct"` or `"official"`.

Syncs are parallel and keep-alive by default: `yostar.sync(..., workers=N)`
fetches up to 16 files at once (default 6) over per-thread reused HTTP(S)
connections, and the CRC-64/XZ is computed from the bytes as they are written
(liblzma's `lzma_crc64` via ctypes when available, else a pure-Python table), so
no second read is needed. The intended profile knob is `download_workers`
(1-16); the backends do not read it yet, so pass `workers=` to `sync` directly
until that wiring lands. Progress is aggregated and emitted at most every
250 ms with `speed_bps`/`eta_s`.

```json
{
  "profiles": {
    "stella": {
      "backend": "stellasora-yostar",
      "install_dir": "~/Games/StellaSora",
      "manifest_index": "https://example.invalid/yostar/index.json"
    }
  }
}
```

## Testing

Add a `tests/test_mygame.py`. Keep it offline and deterministic:

- Feed the parser fixtures, not live subprocesses.
- Test `status()` for the not-installed, installed and running cases.
- Test command construction (the exact argv your backend builds).
- Test that a non-zero exit becomes the right `BackendError` message and code.

```python
from nadare.backends.mygame.backend import MyGameBackend


def test_status_not_installed(tmp_path, monkeypatch):
    # Build a BackendContext with a fake config/profile, monkeypatch PATH so
    # the CLI is "missing", and assert the Status shape.
    ...
```

Run the suite with `./scripts/dev.sh test` (or `pytest -q`).

## Checklist before opening a PR

- [ ] File has an SPDX header (and upstream attribution if derived).
- [ ] `status()` never mutates and is safe while the game runs.
- [ ] Every long operation checks `ctx.cancel` and can be interrupted.
- [ ] No token, cookie or secret can reach an `Event` or the log.
- [ ] `launch()` returns the exit code; `stop()` is a no-op when idle.
- [ ] Errors are `BackendError`/`BackendUnsupported` with actionable messages.
- [ ] Registered in `_BUILTINS`, or via `register(...)` for a plugin.
- [ ] Tests added; `ruff check`, `ruff format --check` and `pytest` pass.
- [ ] README "Supported games" table and [PLAN.md](../PLAN.md) updated.
