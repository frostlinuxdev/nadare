# SPDX-License-Identifier: GPL-3.0-or-later
"""The production-policy lifecycle lane (#1463).

``conftest.py`` turns four production policies off for the unit suite: the Sandbox default,
the background cover migration, the per-game update check and the start-up "found in another
folder" check. Each stub is right for a unit test, but together they meant no test ran the app
the way it starts for a player. This lane runs one controlled lifecycle with those policies
back on:

1. first start: the Sandbox default, the found-copies discovery, the update check (against a
   local fixture server) and the cover migration all run from the window's own start-up wiring;
2. an install that is stopped part-way (Stop, keep the files);
3. a restart: a new ``Config`` read from disk and a new window;
4. recovery: the page offers RESUME, and resuming fetches only what is missing.

Every safety guard of ``conftest.py`` still applies (throw-away HOME/XDG, no real DNS, a
private never-connected session bus, offscreen Qt, modal boxes that fail or are answered).
The game is synthetic and is served from 127.0.0.1; no publisher, account or real library is
reached. ``FAKED`` lists every boundary this file fakes on top of the guards, and
``test_the_lane_fakes_exactly_the_declared_boundaries`` fails when that list and the fixture
drift apart. The lane is left out of the default run (pyproject ``addopts``) and has its own
CI step: ``pytest -p no:xdist -m lifecycle -rA tests/test_lifecycle_production.py``.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

try:
    from PyQt6.QtCore import QCoreApplication
    from PyQt6.QtDBus import QDBusConnection
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001 - PyQt6 may be missing
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.backends import direct
from nadare.backends.winegame import WineGameBackend
from nadare.core import artwork, backends, directkinds, gamesandbox, libraries
from nadare.core import directdl as dd
from nadare.core.backends import BackendContext
from nadare.core.config import Config
from nadare.core.gameversion import INCOMPLETE_MARKER
from nadare.gui import main_window as main_window_module
from nadare.gui.main_window import MainWindow

pytestmark = pytest.mark.lifecycle

#: Wall-clock bounds stretch on a loaded CI runner, as in the rest of the suite (#1024).
_FACTOR = float(os.environ.get("NADARE_TEST_TIME_FACTOR") or 1)

GAME = "lifegame"
GAME_KIND = "direct-lifecycle-fixture"
GAME_TITLE = "Lifecycle Game"
MOVED = "moved"
MOVED_KIND = "direct-lifecycle-moved"
MOVED_TITLE = "Moved Game"
VERSION = "1.0.0"

EXE = b"MZ" + b"\0" * 1022
#: Large enough that the stall below leaves two whole 1 MiB download chunks in the .part file.
BIG = bytes(range(256)) * (4 * 4096)
HALF = len(BIG) // 2

#: Boundaries this lane fakes on top of conftest's guards: name -> why. Kept exact by a test.
FAKED: dict[str, str] = {
    "publisher": (
        "a synthetic direct game whose manifest and files come from a local HTTP server on "
        "127.0.0.1 (plan_factory reads it); no publisher API is asked"
    ),
    "direct-kind admission": (
        "main_window.DIRECT_KINDS also lists the synthetic kind, so the window schedules its "
        "update check as it does for a catalog direct game"
    ),
    "wine runner": "_ensure_default_runner is a no-op: no Proton/umu download",
    "wine prefix": "_ensure_direct_prefix is a no-op: no wineboot runs",
    "host bubblewrap": (
        "gamesandbox.ready()/ready_cached() answer True: the page does not probe this "
        "machine's bwrap (the Sandbox *default* is the real one)"
    ),
}

#: conftest guards this lane relies on (checked in the boundary test, never removed here).
KEPT_GUARDS = (
    "HOME and XDG_* point into the test's tmp tree",
    "socket.getaddrinfo refuses every non-local name (no real DNS)",
    "QDBusConnection.sessionBus is a private never-connected bus",
    "QT_QPA_PLATFORM=offscreen (no window on the user's display)",
    "QMessageBox statics raise; the Stop question answers 'keep'; the failure box 'close'",
    "NADARE_INDEX_BASE and NADARE_RELEASES_API are empty (no Nadare index, no self-update)",
    "keyring off (NADARE_NO_KEYRING=1)",
)

#: Production policies conftest stubs for unit tests and this lane runs for real.
RESTORED = (
    "Sandbox default (gamesandbox.DEFAULT_ON as shipped)",
    "start-up found-copies discovery (MainWindow._start_found_copies_check)",
    "per-game update-check scheduling (MainWindow._maybe_start_check_update)",
    "background cover migration (artwork._schedule_cover_migration)",
)


# ---- the fixture services -------------------------------------------------------------------


class FixtureServer:
    """The synthetic publisher: a manifest and two files, with a stall on demand.

    While ``stall`` is set, the first GET of ``data/big.bin`` sends half the file and then
    waits for ``release`` before closing the connection: an install that stops part-way.
    Ranged GETs are answered 206, so a resumed download can be seen.
    """

    def __init__(self) -> None:
        self.files = {"Game.exe": EXE, "data/big.bin": BIG}
        self.requests: list[tuple[str, str]] = []  # (path, Range header or "")
        self.stall = threading.Event()
        self.half_sent = threading.Event()
        self.release = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802 - http.server naming
                rng = self.headers.get("Range") or ""
                outer.requests.append((self.path, rng))
                if self.path == "/manifest.json":
                    self._send(200, json.dumps(outer.manifest()).encode())
                    return
                name = self.path.removeprefix("/files/")
                body = outer.files.get(name)
                if body is None or not self.path.startswith("/files/"):
                    self._send(404, b"")
                    return
                if rng.startswith("bytes="):
                    start = int(rng[6:].partition("-")[0])
                    part = body[start:]
                    self.send_response(206)
                    self.send_header("Content-Length", str(len(part)))
                    self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
                    self.end_headers()
                    self.wfile.write(part)
                    return
                if name == "data/big.bin" and outer.stall.is_set():
                    outer.stall.clear()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body[:HALF])
                    self.wfile.flush()
                    outer.half_sent.set()
                    outer.release.wait(60)
                    return  # the connection closes with half the file sent
                self._send(200, body)

            def _send(self, status: int, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="lifecycle-fixture-server", daemon=True
        )
        self._thread.start()

    def manifest(self) -> dict[str, Any]:
        return {
            "version": VERSION,
            "exe": "Game.exe",
            "files": [
                {
                    "path": name,
                    "url": f"{self.url}/files/{name}",
                    "size": len(body),
                    "sha256": hashlib.sha256(body).hexdigest(),
                }
                for name, body in self.files.items()
            ],
        }

    def plan(self, fetch: Any = None, **_kwargs: Any) -> dd.Plan:
        """The backend's ``plan_factory``: read the manifest from the fixture server."""
        import urllib.request

        with urllib.request.urlopen(f"{self.url}/manifest.json", timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return dd.Plan(
            LifecycleGame.game_tag,
            str(data["version"]),
            files=tuple(
                dd.RemoteFile(f["path"], f["url"], int(f["size"]), sha256=f["sha256"])
                for f in data["files"]
            ),
            exe=str(data["exe"]),
        )

    def gets(self, path: str) -> list[str]:
        """The Range headers of every GET of ``path`` ("" for a whole-file GET)."""
        return [rng for seen, rng in self.requests if seen == path]

    def close(self) -> None:
        self.release.set()
        self._httpd.shutdown()
        self._httpd.server_close()


def _no_plan(fetch: Any = None, **_kwargs: Any) -> dd.Plan:
    raise dd.DirectError("the lifecycle fixture server is not running")


class LifecycleGame(direct.DirectBackend):
    """A synthetic direct game: the real direct install code, a fixture publisher."""

    kind = GAME_KIND
    display_name = GAME_TITLE
    game_tag = "lifecycle-fixture"
    direct_exe = "Game.exe"
    install_size_hint = 1 << 20  # the download question compares this with free space
    plan_factory = staticmethod(_no_plan)


class MovedGame(direct.DirectBackend):
    """A second synthetic game, whose files were moved: what start-up discovery must find."""

    kind = MOVED_KIND
    display_name = MOVED_TITLE
    game_tag = "lifecycle-moved"
    direct_exe = "Game.exe"
    install_size_hint = 1 << 20
    plan_factory = staticmethod(_no_plan)


# ---- the lane fixture -----------------------------------------------------------------------


@dataclass
class Lane:
    config_path: Path
    server: FixtureServer
    library: Path
    game_dir: Path
    moved_copy: Path
    legacy_cover: Path
    faked: dict[str, str] = field(default_factory=dict)

    @property
    def install_dir(self) -> Path:
        return self.game_dir / "install"

    def backend(self, config: Config) -> LifecycleGame:
        return LifecycleGame(BackendContext(name=GAME, profile=config.profile(GAME), config=config))


def _png_bytes(width: int = 400, height: int = 300) -> bytes:
    from PyQt6.QtCore import QBuffer, QByteArray, QIODevice
    from PyQt6.QtGui import QColor, QImage

    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(QColor(30, 60, 200, 255))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(data)


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def lane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, qapp) -> Lane:
    faked: dict[str, str] = {}

    def fake(name: str, target: Any, attr: str, value: Any) -> None:
        assert name in FAKED, f"undeclared fake {name!r}: add it to FAKED with the reason"
        monkeypatch.setattr(target, attr, value)
        faked[name] = FAKED[name]

    server = FixtureServer()

    # The synthetic games: registered like a custom backend (conftest restores the registry;
    # the storage-class table is restored here).
    monkeypatch.setattr(directkinds, "_STORAGE_CLASSES", dict(directkinds._STORAGE_CLASSES))
    backends.register(GAME_KIND, LifecycleGame)
    backends.register(MOVED_KIND, MovedGame)
    fake("publisher", LifecycleGame, "plan_factory", staticmethod(server.plan))
    fake(
        "direct-kind admission",
        main_window_module,
        "DIRECT_KINDS",
        main_window_module.DIRECT_KINDS | {GAME_KIND},
    )
    fake("wine runner", WineGameBackend, "_ensure_default_runner", lambda self, strict: None)
    fake("wine prefix", WineGameBackend, "_ensure_direct_prefix", lambda self: None)
    fake("host bubblewrap", gamesandbox, "ready", lambda: True)
    monkeypatch.setattr(gamesandbox, "ready_cached", lambda: True)  # same boundary

    # A library on a fake drive inside tmp_path (the #1293 layout).
    library = tmp_path / "drive" / "Games" / libraries.NADARE_DIR
    game_dir = library / GAME_TITLE
    # The moved game: its profile points at the b21 folder, its files sit one level up.
    moved_copy = library / MOVED_TITLE
    (moved_copy / "install").mkdir(parents=True)
    (moved_copy / "install" / "Game.exe").write_bytes(EXE)
    (moved_copy / "prefix" / "drive_c" / "windows").mkdir(parents=True)
    (moved_copy / libraries.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema": libraries.MANIFEST_SCHEMA,
                "backend": MOVED_KIND,
                "title": MOVED_TITLE,
                "install": "install",
                "prefix": "prefix",
                "made_by_nadare": ["install", "prefix"],
            }
        ),
        encoding="utf-8",
    )
    old = library / libraries.NADARE_DIR / MOVED_TITLE

    config = Config.load(tmp_path / "config.json")
    config.data[libraries.CONFIG_KEY] = [{"path": str(library)}]
    config.add_profile(  # first, so it is the active game the window opens
        GAME,
        backend=GAME_KIND,
        title=GAME_TITLE,
        install_dir=str(game_dir / "install"),
        prefix=str(game_dir / "prefix"),
        download_workers=1,  # Game.exe first, then data/big.bin: a deterministic stop point
    )
    config.add_profile(
        MOVED,
        backend=MOVED_KIND,
        title=MOVED_TITLE,
        install_dir=str(old / "install"),
        prefix=str(old / "prefix"),
        owned_dirs=[str(old)],
        owned_trees=["install_dir", "prefix"],
    )
    config.save()

    # A cover cached by an older Nadare as PNG: the start-up cover lookup migrates it.
    covers = artwork.cache_dir()
    covers.mkdir(parents=True, exist_ok=True)
    legacy_cover = covers / "legacy-game-cover.png"
    legacy_cover.write_bytes(_png_bytes())

    yield Lane(
        config_path=config.path,
        server=server,
        library=library,
        game_dir=game_dir,
        moved_copy=moved_copy,
        legacy_cover=legacy_cover,
        faked=faked,
    )
    server.close()


# ---- helpers --------------------------------------------------------------------------------


def _wait(what: str, condition: Callable[[], bool], timeout: float = 20.0) -> None:
    """Run the Qt event loop until ``condition()`` holds; fail naming ``what`` otherwise."""
    deadline = time.monotonic() + timeout * _FACTOR
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if condition():
            return
        time.sleep(0.01)
    QCoreApplication.processEvents()
    assert condition(), f"timed out waiting for: {what}"


def _idle(window: MainWindow, profile: str = GAME) -> bool:
    return not window.controller.is_busy(profile)


def _close(window: MainWindow) -> None:
    """End the window the way quitting does, and let its workers wind down."""
    _wait("the window's operations to end", lambda: not window.controller.busy_profiles())
    window.close()
    window.deleteLater()
    QCoreApplication.processEvents()


def _part_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.part")) if root.is_dir() else []


# ---- the lane -------------------------------------------------------------------------------


def test_the_lane_fakes_exactly_the_declared_boundaries(lane: Lane) -> None:
    """The report the issue asks for: what is faked, what is guarded, what runs for real."""
    lines = ["lifecycle lane (#1463) boundaries", "faked by this lane:"]
    lines += [f"  - {name}: {why}" for name, why in sorted(lane.faked.items())]
    lines += ["kept conftest guards:", *(f"  - {g}" for g in KEPT_GUARDS)]
    lines += ["production policies restored:", *(f"  - {p}" for p in RESTORED)]
    print("\n".join(lines))

    assert lane.faked == FAKED

    # The guards are still in place (the lane must not loosen them).
    home = Path(os.environ["HOME"])
    assert lane.config_path.is_relative_to(home.parent)
    for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        assert Path(os.environ[key]).is_relative_to(home.parent), key
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo("example.com", 443)
    assert not QDBusConnection.sessionBus().isConnected()
    assert os.environ["QT_QPA_PLATFORM"] == "offscreen"
    assert os.environ["NADARE_INDEX_BASE"] == "" and os.environ["NADARE_RELEASES_API"] == ""
    assert os.environ["NADARE_NO_KEYRING"] == "1"

    # The production policies are the shipped code, not conftest's stubs.
    assert MainWindow._start_found_copies_check.__qualname__ == (
        "FoundCopiesMixin._start_found_copies_check"
    )
    assert MainWindow._maybe_start_check_update.__qualname__ == (
        "MainWindow._maybe_start_check_update"
    )
    assert artwork._schedule_cover_migration.__name__ == "_schedule_cover_migration"


def test_start_interrupt_restart_and_recover(lane: Lane, qapp) -> None:
    server = lane.server

    # ---- 1. first start, with the production policies ---------------------------------------
    config = Config.load(lane.config_path)
    window = MainWindow(config)
    assert window._current_profile == GAME

    # Sandbox: the game never chose, so the shipped default decides (on).
    assert not window.game.sandbox.isHidden()
    assert window.game.sandbox.isChecked(), (
        "the production Sandbox default must switch the Sandbox on for a game that never chose"
    )
    assert lane.backend(config).secure_mode_enabled()

    # Discovery: the start-up check finds the moved game and says so.
    _wait("the start-up found-copies discovery", lambda: MOVED in window._found_copies)
    assert set(window._found_copies) == {MOVED}, window._found_copies
    assert [c.game_dir for c in window._found_copies[MOVED]] == [lane.moved_copy.resolve()]
    assert not window.found_banner.isHidden()
    assert MOVED_TITLE in window.found_banner.message_label.text()

    # Update check: the open game's status probe schedules it, against the fixture server.
    _wait("the scheduled update check", lambda: lane.backend(config)._read_latest() is not None)
    assert lane.backend(config)._read_latest()["version"] == VERSION
    assert GAME in window._update_checked
    assert server.gets("/manifest.json"), "the update check never asked the fixture publisher"

    # Cover migration: the start-up cover lookup recoded the old PNG cache.
    webp = lane.legacy_cover.with_suffix(".webp")
    _wait(
        "the background cover migration",
        lambda: webp.is_file() and not lane.legacy_cover.exists(),
    )

    _wait("the first status", lambda: window.game.primary.text() == "INSTALL")

    # ---- 2. install, stopped part-way -------------------------------------------------------
    server.stall.set()
    window._on_game_action("install")
    _wait(
        "half of data/big.bin on disk",
        lambda: (
            server.half_sent.is_set()
            and any(p.stat().st_size >= HALF for p in _part_files(lane.install_dir))
        ),
    )
    window._cancel_with_choice(GAME)  # conftest answers the Stop question with "keep"
    server.release.set()
    _wait("the stopped install to end", lambda: _idle(window))

    assert (lane.install_dir / INCOMPLETE_MARKER).is_file()
    assert (lane.install_dir / "Game.exe").read_bytes() == EXE
    assert not (lane.install_dir / "data" / "big.bin").exists()
    parts = _part_files(lane.install_dir)
    assert [p.name for p in parts] == ["big.bin.part"], parts
    kept = parts[0].stat().st_size
    assert kept >= HALF
    assert server.gets("/files/Game.exe") == [""]
    assert server.gets("/files/data/big.bin") == [""]
    _close(window)

    # ---- 3. restart: a new Config from disk, a new window -----------------------------------
    config = Config.load(lane.config_path)
    window = MainWindow(config)
    assert window._current_profile == GAME
    _wait(
        "RESUME after the restart",
        lambda: window.game.primary.text() == "RESUME",
    )
    status = lane.backend(config).status()
    assert status.state == "not_installed" and status.data.get("incomplete") is True
    assert window.game.sandbox.isChecked()  # still the production default after the restart

    # ---- 4. recovery: resume fetches only what is missing -----------------------------------
    window.game.primary.click()
    _wait(
        "the resumed install to finish",
        lambda: _idle(window) and not (lane.install_dir / INCOMPLETE_MARKER).exists(),
    )
    _wait("LAUNCH after the resumed install", lambda: window.game.primary.text() == "LAUNCH")

    assert server.gets("/files/Game.exe") == [""], "a finished file was downloaded again"
    assert server.gets("/files/data/big.bin") == ["", f"bytes={kept}-"], (
        "the resumed install did not continue the kept partial download"
    )
    assert (lane.install_dir / "data" / "big.bin").read_bytes() == BIG
    assert not _part_files(lane.install_dir)
    final = lane.backend(config).status()
    assert final.state == "installed" and final.version == VERSION
    assert lane.backend(config).secure_mode_enabled()
    _close(window)
