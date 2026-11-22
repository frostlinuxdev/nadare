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
    # The start-up leftover sweep (#1417) must never sweep the host's real /tmp.
    from nadare.core import leftovers

    systmp = tmp_path_factory.mktemp("systmp")  # outside tmp_path: some tests list it
    monkeypatch.setattr(leftovers, "_system_temp", lambda: systmp)

    # The desktop session the tests run in must not change what they expect (the Steam Set up
    # checklist has an extra step on a Wayland session); a test that needs one sets it itself.
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)

    # The support report redacts $USER: a runner logged in as "runner" (GitHub) must not
    # change what the report tests see.
    monkeypatch.setenv("USER", "nadare-tester")

    # Never talk to a real keyring during tests; use the documented fallback.
    monkeypatch.setenv("NADARE_NO_KEYRING", "1")

    # The one-time "unofficial launcher" notice and the big-download question are
    # modal; tests opt in to them explicitly.
    monkeypatch.setenv("NADARE_ASSUME_NOTICES", "1")

    # No test may reach Nadare's real index or release page by default.
    monkeypatch.setenv("NADARE_INDEX_BASE", "")
    monkeypatch.setenv("NADARE_RELEASES_API", "")
    monkeypatch.delenv("CZN_MANIFEST_INDEX", raising=False)
    # A shell that exported NADARE_CHANNEL=unstable must not move every test's folders.
    monkeypatch.delenv("NADARE_CHANNEL", raising=False)
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
    # tmp tree so the suite never appends to the user's real log. Keep it outside
    # tmp_path: tests may install there and compare every resulting file.
    from nadare.core import logging as nadare_logging

    nadare_logging.reset()
    nadare_logging.setup(filename=tmp_path_factory.mktemp("logs") / "nadare.log")
    yield
    nadare_logging.reset()


def _production_lane(request: pytest.FixtureRequest) -> bool:
    """A ``lifecycle`` test (#1463) keeps the production policies the unit stubs below turn off.

    Only the four policy stubs read this (Sandbox default, cover migration, the per-game update
    check, the start-up found-copies check). Every safety guard (HOME/XDG, DNS, session bus,
    modal boxes, offscreen display) applies to it as to every other test.
    """
    return request.node.get_closest_marker("lifecycle") is not None


@pytest.fixture(autouse=True)
def _host_userns_unrestricted(monkeypatch, request):
    """The host's AppArmor userns sysctl must not decide a test (GitHub runs Ubuntu 24.04).

    Tests of the Ubuntu path patch ``userns.restricted`` (or the probe) themselves;
    ``test_userns`` reads real files through its ``path`` argument.
    """
    if request.module.__name__ == "test_userns":
        return
    from nadare.core import userns

    monkeypatch.setattr(userns, "restricted", lambda *a, **k: False)


@pytest.fixture(autouse=True)
def _sandbox_default_off(monkeypatch, request):
    """Older tests expect no bubblewrap; tests of the default turn it back on.

    The once-per-run ``ready()`` answer is cleared too, so no test sees another's.
    """
    from nadare.core import gamesandbox

    if not _production_lane(request):
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
def _no_real_locale_tool(monkeypatch):
    """No test runs the Steam Runtime's pv-locale-gen from the machine's umu download."""
    from nadare.core import pvlocales

    monkeypatch.setattr(pvlocales, "locale_gen_tool", lambda: None)


@pytest.fixture(autouse=True)
def _no_real_gpus_or_input_devices(monkeypatch, tmp_path_factory):
    """No test reads this machine's GPUs or input devices (FSR4 choice, winebus SDL switch):
    results would depend on the box that runs the suite."""
    from nadare.core import fsr4, nvidiarestart, winebus

    empty = tmp_path_factory.mktemp("no-sysfs") / "missing"
    monkeypatch.setattr(fsr4, "DRM_ROOT", empty)
    monkeypatch.setattr(winebus, "INPUT_ROOT", empty)
    # A launch must not be refused because the test machine's NVIDIA driver waits for a restart.
    monkeypatch.setattr(nvidiarestart, "ROOT", empty)


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch):
    """No test asks a real resolver (#1411).

    A GUI test run resolved the art hosts ~1,000 times a minute and a Pi-hole on the network
    then refused every name from the machine. Numeric addresses and local names still resolve
    (tests run local servers); anything else fails at once, as with no network. A test that
    needs a particular answer patches ``artwork._RESOLVER`` (or ``socket.getaddrinfo``) itself.
    """
    import ipaddress
    import socket

    from nadare.core import artwork

    real = socket.getaddrinfo

    def offline(host, port, *args, **kwargs):
        name = host.decode() if isinstance(host, bytes) else host
        name = (name or "").rstrip(".").lower()
        try:
            ipaddress.ip_address(name.split("%", 1)[0])
            return real(host, port, *args, **kwargs)
        except ValueError:
            pass
        if not name or name == "localhost" or name.endswith(".localhost"):
            return real("localhost" if name else host, port, *args, **kwargs)
        raise socket.gaierror(socket.EAI_NONAME, f"no real DNS in tests: {name}")

    monkeypatch.setattr(socket, "getaddrinfo", offline)
    monkeypatch.setattr(artwork, "_RESOLVER", offline)
    artwork._RESOLVED.clear()


@pytest.fixture(autouse=True)
def _no_live_hoyo_art_feed(monkeypatch):
    """The HoYoverse launcher art feed is never fetched for real in tests."""
    from nadare.backends import hoyo_art
    from nadare.core.news import NewsError

    def offline(url, **kwargs):
        raise NewsError("network is disabled in tests")

    monkeypatch.setattr(hoyo_art, "http_get_json", offline)


@pytest.fixture(autouse=True)
def _no_live_publisher_news(monkeypatch):
    """Background GUI news uses curl, bypassing the Python DNS guard."""
    from urllib.parse import urlsplit

    from nadare.core import news

    request, real_curl = news._request, news._curl

    def offline(method, url, **kwargs):
        if (news._curl is real_curl
                and urlsplit(url).hostname not in ("127.0.0.1", "localhost", "::1")):
            raise news.NewsError("network is disabled in tests")
        return request(method, url, **kwargs)

    monkeypatch.setattr(news, "_request", offline)


@pytest.fixture(autouse=True)
def _fresh_steam_asset_cache():
    """Steam's hashed asset table is process-wide; one test must not answer for the next.

    ``artwork.steam_library_assets`` caches for hours (a 404 answer included), so without this
    a test that faked the request decided what a later test saw.
    """
    from nadare.core import artwork

    artwork._STEAM_ASSETS.clear()
    yield
    artwork._STEAM_ASSETS.clear()


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
    if request.node.get_closest_marker("real_cover_migration") or _production_lane(request):
        return
    from nadare.core import artwork

    monkeypatch.setattr(
        artwork, "_real_schedule_cover_migration", artwork._schedule_cover_migration,
        raising=False,
    )
    monkeypatch.setattr(artwork, "_schedule_cover_migration", lambda root: None)


@pytest.fixture(autouse=True)
def _no_automatic_update_checks(monkeypatch, request):
    """Window tests never start the background version check (#1024).

    Opening a direct game started ``check_update`` on a worker, which asked the publisher's real
    API from the test run and could outlive the test: Qt then aborted the whole xdist worker
    ("Fatal Python error: Aborted", seen on CI 2026-11-08 in ``kuro.resolve_game``). Tests of the
    check call ``backend.check_update()`` themselves.
    """
    if _production_lane(request):
        return
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


@pytest.fixture(autouse=True)
def _no_automatic_found_copies_check(monkeypatch, request):
    """Window tests never start the start-up "game found in another folder" check (#1293).

    It reads the libraries on a thread and can raise a banner; tests of it call
    ``_real_start_found_copies_check`` or ``_show_found_copies`` themselves.
    """
    try:
        from nadare.gui.main_window import MainWindow
    except Exception:  # noqa: BLE001 - PyQt6 may be missing; GUI tests skip themselves
        return
    real = getattr(MainWindow, "_start_found_copies_check", None)
    if real is None:
        return
    # The real chooser is a modal dialog: a test that reaches it unstubbed would hang.
    monkeypatch.setattr(MainWindow, "_choose_found_copy", lambda self, name, copies: None)
    if _production_lane(request):
        return
    monkeypatch.setattr(MainWindow, "_real_start_found_copies_check", real, raising=False)
    monkeypatch.setattr(MainWindow, "_start_found_copies_check", lambda self: None)


@pytest.fixture(autouse=True)
def _no_startup_leftover_sweep(monkeypatch, request):
    """A real window's 5 s start-up sweep (#1417) does nothing in unit tests.

    Its thread deletes files and can outlive the bounded join above: one started by an earlier
    GUI test's timer reached a later test's patched ``installercache.sweep`` (CI 3.13, #1641)
    and deleted another test's umu folder (CI 3.12, #1619/#1639). Tests of the sweep call
    ``CleanupMixin`` directly; the lifecycle lane keeps the real one.
    """
    if _production_lane(request):
        return
    try:
        from nadare.gui.main_window import MainWindow
    except Exception:  # noqa: BLE001 - PyQt6 may be missing; GUI tests skip themselves
        return
    monkeypatch.setattr(MainWindow, "_start_leftover_sweep", lambda self: None)


@pytest.fixture(autouse=True)
def _no_automatic_runner_prefetch(monkeypatch, request):
    """A real window's 20 s runner prefetch (#1425) does nothing in unit tests.

    A leftover window's timer ran the real prefetch during a later test; its thread signalled
    the destroyed window and crashed the worker (CI 1f9ef3c1). Tests of the prefetch call
    ``_real_start_runner_prefetch`` or ``CleanupMixin`` directly; the lifecycle lane keeps it.
    """
    if _production_lane(request):
        return
    try:
        from nadare.gui.main_window import MainWindow
    except Exception:  # noqa: BLE001 - PyQt6 may be missing; GUI tests skip themselves
        return
    real = MainWindow._start_runner_prefetch
    monkeypatch.setattr(MainWindow, "_real_start_runner_prefetch", real, raising=False)
    monkeypatch.setattr(MainWindow, "_start_runner_prefetch", lambda self: None)


@pytest.fixture(autouse=True)
def _no_automatic_startup_checks(monkeypatch, request):
    """A real window's 3 s start-up checks (#1299) do nothing in unit tests.

    A leftover window's timer ran the real Flatpak folder check while ``test_flatpak_reach_1299``
    had patched ``in_flatpak`` to True: it found that window's games out of reach and opened a
    real message box, which aborted the worker (CI run 2403). Tests of the checks call
    ``CleanupMixin`` or ``_real_start_*`` directly; the lifecycle lane keeps them.
    """
    if _production_lane(request):
        return
    try:
        from nadare.gui.main_window import MainWindow
    except Exception:  # noqa: BLE001 - PyQt6 may be missing; GUI tests skip themselves
        return
    for name in (
        "_start_sandbox_broad_check",
        "_start_flatpak_reach_check",
        "_start_sandbox_trust_check",
    ):
        monkeypatch.setattr(MainWindow, "_real" + name, getattr(MainWindow, name), raising=False)
        monkeypatch.setattr(MainWindow, name, lambda self: None)


@pytest.fixture(autouse=True)
def _library_picker_is_a_folder_dialog_in_tests(monkeypatch):
    """Move's library list (#1293) is a modal dialog; tests pick through the folder dialog.

    Tests of Move stub ``QFileDialog.getExistingDirectory``; routing the picker there keeps
    them meaningful and keeps the real modal out of every offscreen run.
    """
    try:
        from PyQt6.QtWidgets import QFileDialog

        from nadare.gui.settings import SettingsPage
    except Exception:  # noqa: BLE001 - PyQt6 may be missing; GUI tests skip themselves
        return
    if not hasattr(SettingsPage, "_choose_library"):
        return

    def pick(self, config, title, start):
        return QFileDialog.getExistingDirectory(self, title, str(start)) or None

    monkeypatch.setattr(
        SettingsPage, "_real_choose_library", SettingsPage._choose_library, raising=False
    )
    monkeypatch.setattr(SettingsPage, "_choose_library", pick)
    # Settings' "use this folder" chooser (#1293) is modal too.
    monkeypatch.setattr(SettingsPage, "_choose_found_copy", lambda self, name, copies: None)


#: Plain daemon threads that do image work in Qt (news images, the cover-cache sweep).
_BACKGROUND_THREAD_NAMES = frozenset(
    {"nadare-news-fetch", "nadare-news-image", "cover-cache-migration", "nadare-found-copies"}
)
#: The window's start-up threads that delete files. One left running reads HOME/XDG when it
#: gets there, which may already be the *next* test's: the leftover sweep deleted the umu
#: folder of test_leftovers_umu before that test took umu's lock (CI 3.12, twice, 2026-11-16).
_DELETING_THREAD_NAMES = frozenset(
    {"leftover-sweep", "nadare-spare-delete", "profile-folders-delete",
     "steam-shortcut-cleanup", "quality-cleanup"}
)


def _join_until(thread: threading.Thread, deadline: float) -> None:
    """Join ``thread`` by ``deadline``, also when it is still starting.

    ``threading.enumerate()`` lists a thread whose ``start()`` was called but which has not
    run yet; joining it raises "cannot join thread before it is started" (the recurring
    test_selfupdate_window teardown error on CI, 3.12/3.13). Wait for it to start instead.
    """
    while True:
        try:
            thread.join(max(0.0, deadline - time.monotonic()))
            return
        except RuntimeError:
            if time.monotonic() >= deadline:
                return
            time.sleep(0.005)


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
        waited = _BACKGROUND_THREAD_NAMES | _DELETING_THREAD_NAMES
        if thread.name in waited and thread is not threading.current_thread():
            _join_until(thread, deadline)
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


@pytest.fixture(autouse=True)
def _no_real_session_bus(monkeypatch):
    """No test may reach the desktop's session bus (a real "hello" notification, 2026-11-15).

    Qt caches the session bus per process, so a test that set DBUS_SESSION_BUS_ADDRESS still
    talked to the real desktop when the suite ran inside one (a worker on flxpc). Every test
    gets a connection that is never connected; a test that wants a bus patches
    ``sessionBus`` itself (its monkeypatch runs after this one), and test_notify_dbus_real
    starts its own private bus in a subprocess.
    """
    try:
        from PyQt6.QtDBus import QDBusConnection
    except Exception:  # noqa: BLE001 - PyQt6 may be missing; GUI tests skip themselves
        return
    monkeypatch.setattr(
        QDBusConnection,
        "sessionBus",
        staticmethod(lambda: QDBusConnection("nadare-tests-no-bus")),
    )


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
def _move_confirmation_never_blocks(monkeypatch):
    """The "Moved" box after a Move (#1352) is modal: tests that care stub it themselves."""
    try:
        from nadare.gui.settings_parts.move import MoveMixin
    except Exception:  # noqa: BLE001 - PyQt6 may be missing; GUI tests skip themselves
        return
    monkeypatch.setattr(MoveMixin, "_real_confirm_moved", MoveMixin._confirm_moved, raising=False)
    monkeypatch.setattr(MoveMixin, "_confirm_moved", lambda self, text: None)


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
def _spare_copies_question_never_blocks(monkeypatch):
    """The other copies of a re-pointed game are offered (#1300): tests that do not care keep
    them."""
    try:
        from nadare.gui.main_window import MainWindow
    except Exception:  # noqa: BLE001 - PyQt6 may be missing; GUI tests skip themselves
        return
    monkeypatch.setattr(MainWindow, "_ask_spare_copies", lambda self, *a, **k: [])


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


@pytest.fixture(autouse=True)
def _no_bundled_umu(monkeypatch, tmp_path_factory):
    """A local AppImage build leaves the pinned umu archive in src/nadare/assets; tests must not
    depend on it (#1338). Tests of the bundle point BUNDLED_ARCHIVE at their own file."""
    from nadare.core import umu as _umu

    monkeypatch.setattr(
        _umu, "BUNDLED_ARCHIVE", tmp_path_factory.getbasetemp() / "no-bundled-umu.tar"
    )


@pytest.fixture(autouse=True)
def _no_live_steam_metadata(monkeypatch):
    """Steam metadata uses curl's resolver, so the Python DNS guard cannot block it."""
    from urllib.parse import urlparse

    from nadare.core import artwork

    get_json, real_curl = artwork._get_json, artwork._curl

    def offline(url, **kwargs):
        steam_host = urlparse(url).hostname in ("api.steampowered.com", "store.steampowered.com")
        if steam_host and artwork._curl is real_curl:
            raise OSError("Steam metadata is offline in tests; supply a fake HTTP client")
        return get_json(url, **kwargs)

    monkeypatch.setattr(artwork, "_get_json", offline)
