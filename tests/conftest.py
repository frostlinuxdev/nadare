# SPDX-License-Identifier: GPL-3.0-or-later
"""Hermetic test configuration for Nadare.

Every test gets a throw-away HOME and XDG tree so nothing touches the real
user's config, state, cache or downloads. ``src`` is put on ``sys.path`` so the
suite also runs without ``PYTHONPATH=src``.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

# Force, not setdefault: a desktop session exports QT_QPA_PLATFORM and the GUI
# tests would otherwise open real windows (and behave differently).
os.environ["QT_QPA_PLATFORM"] = "offscreen"

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.fixture(autouse=True)
def _isolated_environment(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """Redirect HOME/XDG and force the plaintext secret fallback.

    The redirect is applied for *every* test, so ``nadare.core.paths`` (which
    reads the environment lazily) always resolves under ``tmp_path``.
    """
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))

    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg / "cache"))
    # Outside tmp_path (some tests list it). Without a real folder the single-instance socket
    # falls back to one name shared by every parallel worker.
    runtime = tmp_path_factory.mktemp("runtime")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))

    # The desktop session the tests run in must not change what they expect (the Steam Set up
    # checklist has an extra step on a Wayland session); a test that needs one sets it itself.
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)

    # Never talk to a real keyring during tests; use the documented fallback.
    monkeypatch.setenv("NADARE_NO_KEYRING", "1")

    # The one-time "unofficial launcher" notice and the big-download question are
    # modal; tests opt in to them explicitly.
    monkeypatch.setenv("NADARE_ASSUME_NOTICES", "1")

    # No test may reach Nadare's real index or release page by default.
    monkeypatch.setenv("NADARE_INDEX_BASE", "")
    monkeypatch.setenv("NADARE_RELEASES_API", "")
    monkeypatch.delenv("CZN_MANIFEST_INDEX", raising=False)
    monkeypatch.delenv("YOSTAR_MANIFEST_INDEX", raising=False)
    monkeypatch.delenv("YOSTAR_MEDIA_FILE", raising=False)

    # The CZN 32-bit-libs preflight reads the real ldconfig cache; tests must
    # not depend on the host (tests that exercise it patch ``missing_32bit``).
    from nadare.core import hostlibs

    if request.module.__name__ != "test_hostlibs":
        monkeypatch.setattr(hostlibs, "missing_32bit", lambda: [])
        monkeypatch.setattr(hostlibs, "missing_gl", lambda: [])

    # Installers need a display (#437); CI has none. Tests that exercise the
    # headless refusal remove it again.
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        monkeypatch.setenv("DISPLAY", ":99")

    # Logging may already have been configured at import time (with the real
    # state dir). Tear it down and re-point the file handler at this test's
    # tmp tree so the suite never appends to the user's real log.
    from nadare.core import logging as nadare_logging

    nadare_logging.reset()
    nadare_logging.setup(filename=tmp_path / "nadare.log")
    yield
    nadare_logging.reset()


@pytest.fixture(autouse=True)
def _sandbox_default_off(monkeypatch):
    """Older tests expect no bubblewrap; tests of the default turn it back on.

    The once-per-run ``ready()`` answer is cleared too, so no test sees another's.
    """
    from nadare.core import gamesandbox

    monkeypatch.setattr(gamesandbox, "DEFAULT_ON", False)
    gamesandbox._READY_CACHE.clear()
    yield
    gamesandbox._READY_CACHE.clear()


@pytest.fixture(autouse=True)
def _http_open_follows_patched_urlopen(monkeypatch):
    """Tests that fake ``urllib.request.urlopen`` also fake ``download.http_open``.

    The WebView2 / VC++ downloads go through ``download.http_open`` (so a
    per-hop redirect guard applies); older tests stub ``urlopen`` instead.
    """
    import urllib.error
    import urllib.request

    from nadare.core import download

    real_http_open = download.http_open
    real_urlopen = urllib.request.urlopen

    def http_open(
        url, *, headers=None, timeout=download.DEFAULT_TIMEOUT, redirect_allowed=None, data=None
    ):
        if urllib.request.urlopen is real_urlopen:
            return real_http_open(
                url,
                headers=headers,
                timeout=timeout,
                redirect_allowed=redirect_allowed,
                data=data,
            )
        try:
            return urllib.request.urlopen(url, timeout=timeout)
        except download.DownloadError:
            raise
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise download.DownloadError(f"cannot reach {url}: {exc}") from exc

    monkeypatch.setattr(download, "http_open", http_open)


@pytest.fixture(autouse=True)
def _no_live_hoyo_art_feed(monkeypatch):
    """The HoYoverse launcher art feed is never fetched for real in tests."""
    from nadare.backends import hoyo_art
    from nadare.core.news import NewsError

    def offline(url, **kwargs):
        raise NewsError("network is disabled in tests")

    monkeypatch.setattr(hoyo_art, "http_get_json", offline)


@pytest.fixture(autouse=True)
def _nikke_launcher_api_offline(monkeypatch):
    """Install tests that do not fake the launcher CDN take the old path, as without a network."""
    from nadare.core import railrepo
    from nadare.core.sources import nikke

    real_launcher_info = nikke.launcher_info
    real_client_info = nikke.client_info

    def offline(post=None, **kwargs):
        # A test that supplies its own POST is already hermetic and may run the real parser;
        # only the default network path is refused.
        if post is not None:
            return real_launcher_info(post=post, **kwargs)
        raise railrepo.RailError("offline in tests")

    def client_offline(post=None, **kwargs):
        # The game-client record too: a launcherless Play checks it before starting, and a test
        # that did not fake it once downloaded the whole 1.1 GB client (2026-11-10).
        if post is not None:
            return real_client_info(post=post, **kwargs)
        raise railrepo.RailError("offline in tests")

    monkeypatch.setattr(nikke, "launcher_info", offline)
    monkeypatch.setattr(nikke, "client_info", client_offline)


@pytest.fixture(autouse=True)
def _no_background_cover_migration(request, monkeypatch):
    """A cover lookup never starts the background WebP migration in a test (#1024).

    The daemon thread converted a test's own cached PNG and deleted it and its ``.src`` sidecar
    while the test still read them. Tests of the migration call ``migrate_cached_covers()``
    themselves, restore ``artwork._real_schedule_cover_migration``, or carry the
    ``real_cover_migration`` marker.
    """
    if request.node.get_closest_marker("real_cover_migration"):
        return
    from nadare.core import artwork

    monkeypatch.setattr(
        artwork, "_real_schedule_cover_migration", artwork._schedule_cover_migration,
        raising=False,
    )
    monkeypatch.setattr(artwork, "_schedule_cover_migration", lambda root: None)


@pytest.fixture(autouse=True)
def _no_automatic_update_checks(monkeypatch):
    """Window tests never start the background version check (#1024).

    Opening a direct game started ``check_update`` on a worker, which asked the publisher's real
    API from the test run and could outlive the test: Qt then aborted the whole xdist worker
    ("Fatal Python error: Aborted", seen on CI 2026-11-08 in ``kuro.resolve_game``). Tests of the
    check call ``backend.check_update()`` themselves.
    """
    try:
        from nadare.gui.main_window import MainWindow
    except Exception:  # noqa: BLE001 - PyQt6 may be missing; GUI tests skip themselves
        return
    # A test of the throttling itself (with a recording _start) restores the real method.
    monkeypatch.setattr(
        MainWindow, "_real_maybe_start_check_update", MainWindow._maybe_start_check_update,
        raising=False,
    )
    monkeypatch.setattr(MainWindow, "_maybe_start_check_update", lambda self, profile: False)


#: Plain daemon threads that do image work in Qt (news images, the cover-cache sweep).
_BACKGROUND_THREAD_NAMES = frozenset(
    {"nadare-news-fetch", "nadare-news-image", "cover-cache-migration"}
)


@pytest.fixture(autouse=True)
def _background_work_ends_with_its_test():
    """Wait (bounded) for Qt's shared thread pool after each test (#1024).

    A cover fetch or image decode started by one GUI test kept running into the next test,
    which had patched ``artwork.cache_dir`` or ``os.replace`` process-wide: it wrote into that
    test's folder or was counted by its fakes. Waiting here keeps each test's work its own.
    """
    yield
    # News images and the cover-cache sweep run on plain daemon threads that outlive the
    # loader's deadline; one was still recoding WebP inside Qt when the next test's event loop
    # ran, and the xdist worker aborted (CI 2026-11-10, test_single_instance).
    deadline = time.monotonic() + 3.0
    for thread in threading.enumerate():
        if thread.name in _BACKGROUND_THREAD_NAMES and thread is not threading.current_thread():
            thread.join(max(0.0, deadline - time.monotonic()))
    try:
        from PyQt6.QtCore import QCoreApplication, QThreadPool
    except Exception:  # noqa: BLE001 - PyQt6 may be missing; nothing to wait for
        return
    if QCoreApplication.instance() is not None:
        QThreadPool.globalInstance().waitForDone(3000)
        # Status/news workers are QThreads, not pool tasks: a closed window detaches them, and
        # one still fetching hero art wrote it into the next test's XDG folder (CI 2026-11-08,
        # test_sevenz_parallel saw a stray ``covers/a-official-hero.webp``).
        try:
            from nadare.gui import workers

            workers.wait_detached(3000)
        except Exception:  # noqa: BLE001 - best effort, like the pool wait
            pass


class RealModalBox(AssertionError):
    """A test reached a real, unstubbed message box: offscreen it would wait forever."""


@pytest.fixture(autouse=True)
def _message_boxes_fail_instead_of_hanging(monkeypatch):
    """An unstubbed QMessageBox fails the test at once instead of hanging the CI run.

    Offscreen, nobody can click a modal box: one new box in a path an old test runs kept the
    single runner at 99 % until the run was cancelled (2026-11-08). A test that expects a box
    stubs it itself (its monkeypatch runs after this one).
    """
    try:
        from PyQt6.QtWidgets import QMessageBox
    except Exception:  # noqa: BLE001 - PyQt6 may be missing; GUI tests skip themselves
        return

    def refuse(*args, **kwargs):
        text = next((a for a in args if isinstance(a, str) and len(a) > 20), "")
        raise RealModalBox(f"a real message box would block this test: {text[:200]!r}")

    for name in ("warning", "critical", "information", "question", "about"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(refuse))
    monkeypatch.setattr(QMessageBox, "exec", lambda self: refuse(self.text()))


@pytest.fixture(autouse=True)
def _stop_download_question_never_blocks(monkeypatch):
    """Stopping a download asks first (#991); tests that do not care stop and keep the files."""
    try:
        from nadare.gui.main_window import MainWindow
    except Exception:  # noqa: BLE001 - PyQt6 may be missing; GUI tests skip themselves
        return
    monkeypatch.setattr(MainWindow, "_ask_cancel_choice", lambda self, *a, **k: "keep")


@pytest.fixture(autouse=True)
def _failure_dialog_never_blocks(monkeypatch):
    """The Retry / Open logs / Close box is modal: tests must never wait on it."""
    try:
        from nadare.gui.game_view import GameView
    except Exception:  # noqa: BLE001 - PyQt6 may be missing; GUI tests skip themselves
        return
    # A test that drives the real box (with its own exec) reaches it here.
    monkeypatch.setattr(GameView, "_real_failure_choice", GameView._failure_choice, raising=False)
    monkeypatch.setattr(GameView, "_failure_choice", lambda self, *a, **k: "close")


@pytest.fixture(autouse=True)
def _quality_cleanup_question_never_blocks(monkeypatch):
    """Removing old resource qualities asks first: tests that do not care keep the files."""
    try:
        from nadare.gui.main_window import MainWindow
    except Exception:  # noqa: BLE001 - PyQt6 may be missing; GUI tests skip themselves
        return
    monkeypatch.setattr(MainWindow, "_ask_remove_qualities", lambda self, *a, **k: False)


@pytest.fixture(autouse=True)
def _restore_backend_registry():
    """Tests that ``register()`` a fake backend must not leak it to other tests."""
    from nadare.core import backends

    with backends._REGISTRY_LOCK:
        saved = dict(backends._REGISTRY)
    yield
    with backends._REGISTRY_LOCK:
        backends._REGISTRY.clear()
        backends._REGISTRY.update(saved)


@pytest.fixture(autouse=True)
def _fresh_wineserver_cooldown() -> None:
    """The kill cooldown is per process; tests must not inherit another test's kill."""
    from nadare.backends import wine

    wine._WINESERVER_KILLED.clear()


@pytest.fixture(scope="session", autouse=True)
def _one_qapplication():
    """Keep one QApplication alive for the whole run.

    PyQt destroys the C++ application when the last Python reference goes, and the
    module-scoped ``qapp`` fixtures drop theirs at module teardown while widgets may
    still exist. One session-wide reference makes that teardown deterministic.
    """
    try:
        from PyQt6.QtWidgets import QApplication
    except ImportError:  # PyQt-less interpreters skip the GUI tests anyway
        yield None
        return
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def restyle_safe():
    """For tests that restyle the whole app: end earlier tests' windows first (#1024).

    ``QApplication.setStyleSheet`` and an app font change repolish every live widget; after the
    GUI files a worker holds about 30,000 of them, and that restyle segfaulted twice on CI 3.12.
    Not autouse: ending windows after every test would cut off workers still winding down.
    """
    from _qt_leftovers import end_leftover_windows
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    end_leftover_windows()
    yield app


@pytest.fixture(autouse=True)
def _no_leaked_message_boxes():
    """Sweep QMessageBoxes a test built but never ran.

    Tests replace ``QMessageBox.exec`` with a stub far more often than they show a box. A
    never-shown box left alive makes the next app-wide ``setStyleSheet`` (the runtime font
    refresh in ``theme.apply_theme``) segfault in Qt's ``QMessageBox::changeEvent`` ->
    ``setIcon`` -> ``QGridLayout::addWidget`` path. Only boxes are swept here: flushing every
    deferred delete would also tear down windows whose worker threads are still winding down.
    """
    yield
    try:
        from PyQt6.QtCore import QEvent
        from PyQt6.QtWidgets import QApplication, QMessageBox
    except ImportError:
        return
    app = QApplication.instance()
    if app is None:
        return
    for box in [w for w in QApplication.allWidgets() if isinstance(w, QMessageBox)]:
        box.deleteLater()
        QApplication.sendPostedEvents(box, QEvent.Type.DeferredDelete)
    app.processEvents()


@pytest.fixture(autouse=True)
def _no_leaked_detached_workers():
    """A test double left in the process-wide detached set must not reach the next test."""
    yield
    try:
        from PyQt6.QtCore import QThread

        from nadare.gui import workers
    except ImportError:
        return
    for worker in list(workers._detached):
        if isinstance(worker, QThread) and worker.isRunning():
            continue  # never drop a running thread
        workers._detached.discard(worker)
