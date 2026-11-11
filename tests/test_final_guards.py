# SPDX-License-Identifier: GPL-3.0-or-later
"""Final round (b23): HIGH-risk guards that no test executed or checked.

Found by the rebuilt sweep (/mnt/kikyou/work/flash-audit/2026-11-11-diff-final): each guard was
broken on purpose and every test that runs its line stayed green (or no test ran it at all).
Each test here kills that mutant (18 of the 20 HIGH ones; mutation run in
/mnt/kikyou/work/mutation-final-c). Not yet: g057 (config's disk-profile names on a damaged file)
and g024 (a linked steamuser folder): these tests reach them by another path, see #1207.
Where a guard has a legitimate side, it is tested too.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover
    from PyQt6.QtWidgets import QApplication, QDialog
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import libraries, process, removal, sophon, vcredist
from nadare.core.backends import BackendError
from nadare.core.config import Config


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    return tmp_path


# ---- removal: never delete through a link, never into a mounted drive ---------------------------
def test_removing_a_tree_never_follows_a_link_inside_it(tmp_path):
    root = tmp_path / "game"
    (root / "keep").mkdir(parents=True)
    (root / "junk.bin").write_text("x")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("mine")
    (root / "link").symlink_to(outside, target_is_directory=True)
    removal._rmtree_safely(root, keep=[root / "keep"])  # the walk that honours keep trees
    assert (outside / "precious.txt").read_text() == "mine"
    assert not (root / "link").exists() and not (root / "junk.bin").exists()


def test_a_tree_that_is_itself_a_mount_is_left_alone(tmp_path, monkeypatch):
    root = tmp_path / "drive"
    root.mkdir()
    (root / "game.bin").write_text("x")
    monkeypatch.setattr(removal, "_is_mount", lambda path, mounts: path == root)
    outcome = removal._rmtree_safely(root)
    assert outcome.mounts == [root]
    assert (root / "game.bin").exists()


# ---- CZN: a plain uninstall keeps the login; purge removes it -------------------------------
def _czn(xdg, name="stove-czn"):
    from nadare.backends.czn.backend import CznBackend
    from nadare.core.backends import BackendContext

    cfg = Config.load(xdg / "config.json")
    cfg.add_profile(name, backend="stove-czn")
    backend = CznBackend(BackendContext(name=name, profile=cfg.profile(name), config=cfg))
    data = backend._data_dir()
    data.mkdir(parents=True, exist_ok=True)
    (data / "session.json").write_text("{}")
    (backend.prefix() / "drive_c").mkdir(parents=True, exist_ok=True)
    backend._running = lambda: False
    return backend, data


def test_czn_plain_uninstall_keeps_the_login_and_purge_removes_it(xdg):
    backend, data = _czn(xdg)
    with contextlib.suppress(BackendError):  # what it says about the prefix does not matter here
        backend.uninstall(purge=False)
    assert (data / "session.json").exists()
    with contextlib.suppress(BackendError):
        backend.uninstall(purge=True)
    assert not (data / "session.json").exists()


# ---- config: a damaged profiles value on disk is not "every profile removed" ------------------
def test_a_damaged_profiles_value_on_disk_drops_no_profile(tmp_path):
    path = tmp_path / "config.json"
    cfg = Config.load(path)
    cfg.add_profile("g", backend="wine", title="G")
    cfg.save()
    raw = json.loads(path.read_text())
    raw["profiles"] = ["not", "a", "dict"]
    path.write_text(json.dumps(raw))
    cfg.data["language"] = "fr"
    cfg.save()
    assert "g" in cfg.profiles
    assert "g" in json.loads(path.read_text()).get("profiles", {})


# ---- libraries: a manifest path that is not text is a skipped manifest, not a crash -----------
def test_a_manifest_path_that_is_not_text_is_skipped(xdg, monkeypatch):
    home = xdg / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    cfg = Config.load(xdg / "config.json")
    game = home / "Games" / libraries.NADARE_DIR / "Brown Dust 2"
    game.mkdir(parents=True)
    (game / libraries.MANIFEST_NAME).write_text(
        json.dumps({"schema": libraries.MANIFEST_SCHEMA, "backend": "direct-bd2",
                    "title": "BD2", "install": 5, "made_by_nadare": []})
    )
    found, notes = libraries.scan(cfg, home / "Games")
    assert found == []
    assert any("skipped" in n for n in notes)


# ---- process: an empty group is never signalled (its number may belong to someone else) -------
def test_an_empty_process_group_is_not_signalled(monkeypatch):
    sent = []
    monkeypatch.setattr(process, "_group_alive", lambda pgid: False)
    monkeypatch.setattr(process.os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))
    process._stop_orphaned_group(424242)
    assert sent == []
    monkeypatch.setattr(process, "_group_alive", lambda pgid: bool(sent) is False)
    process._stop_orphaned_group(424242)
    assert sent and sent[0] == (424242, signal.SIGTERM)  # a live group is still stopped


# ---- Sophon: an update never treats its own ledger as an earlier version -----------------------
def test_a_package_does_not_supersede_its_own_ledger(tmp_path):
    state = tmp_path / sophon.STATE_DIR
    state.mkdir()
    for name in ("game-3.3.0", "game-3.2.0"):
        (state / f"sophon-{name}.files").write_text("")
    pkg = sophon.SophonPackage(name="game-3.3.0", manifest_url="", chunk_url="", manifest_id="",
                               size=0, supersedes=r"game-\d+\.\d+\.\d+")
    found = [p.name for p in sophon._superseded_ledgers(pkg, tmp_path)]
    assert found == ["sophon-game-3.2.0.files"]


# ---- NIKKE CDN: only the publisher's CDN, and only the bytes the manifest names ----------------
def test_a_manifest_url_off_the_cdn_is_refused(monkeypatch):
    from nadare.core import railrepo
    from nadare.core.sources import nikke as source

    good = "https://" + source.CDN_HOST + "/root/"
    repo = SimpleNamespace(cdn_root=good, manifest_url="https://evil.example/m")
    monkeypatch.setattr(railrepo, "repositories", lambda info: [repo])
    with pytest.raises(railrepo.RailError, match="manifest URL"):
        source.game_repositories({})
    repo.manifest_url = good + "manifest"
    assert source.game_repositories({}) == [repo]


class _Response:
    def __init__(self, data: bytes, url: str):
        self._data, self.url = data, url

    def read(self, n=-1):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fetch(monkeypatch, data: bytes, url: str, md5_hex: str):
    import hashlib  # noqa: F401 - md5 below

    from nadare.core.sources import nikke as source

    monkeypatch.setattr(source, "RETRY_DELAYS", ())
    monkeypatch.setattr(source.download, "http_open", lambda u, **k: _Response(data, url))
    target = "https://" + source.CDN_HOST + "/obj"
    return source.fetch_object(target, len(data), md5_hex)


def test_an_object_redirected_off_the_cdn_is_refused(monkeypatch):
    import hashlib

    from nadare.core import railrepo
    from nadare.core.sources import nikke as source

    data = b"payload"
    md5 = hashlib.md5(data).hexdigest()  # noqa: S324
    with pytest.raises(railrepo.RailError, match="redirected"):
        _fetch(monkeypatch, data, "https://evil.example/obj", md5)
    assert _fetch(monkeypatch, data, "https://" + source.CDN_HOST + "/obj", md5) == data


def test_an_object_with_the_wrong_md5_is_refused(monkeypatch):
    from nadare.core import railrepo
    from nadare.core.sources import nikke as source

    with pytest.raises(railrepo.RailError, match="MD5"):
        _fetch(monkeypatch, b"payload", "https://" + source.CDN_HOST + "/obj", "0" * 32)


# ---- VC++: the stray-installer sweep and the pin rules -------------------------------------------
def _proc(root: Path, pid: str, cmd: str, group: int, ppid: int) -> None:
    d = root / pid
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(cmd.replace(" ", "\0").encode())
    (d / "stat").write_text(f"{pid} (x) S {ppid} {group} {group} 0 0 0")


def test_the_installer_sweep_skips_odd_entries_and_never_signals_group_zero(tmp_path, monkeypatch):
    root = tmp_path / "proc"
    root.mkdir()
    (root / "self").mkdir()
    (root / "self" / "cmdline").write_bytes(b"x")
    own = os.getpid()
    _proc(root, "4242", f"{vcredist._INSTALLER_NAME} /quiet", 0, own)
    sent = []
    monkeypatch.setattr(vcredist, "_descends_from", lambda proc_root, pid, r: True)
    monkeypatch.setattr(vcredist.os, "killpg", lambda g, s: sent.append(("group", g)))
    monkeypatch.setattr(vcredist.os, "kill", lambda p, s: sent.append(("pid", p)))
    monkeypatch.setattr(vcredist.time, "sleep", lambda s: None)
    vcredist._stop_installer(str(root), root_pid=own)
    assert ("group", 0) not in sent


def test_a_user_pin_override_does_not_also_accept_index_hashes(tmp_path):
    path = tmp_path / "vc_redist.x64.exe"
    path.write_bytes(b"not the pinned build")
    digest = vcredist._sha256_file(path)
    with pytest.raises(BackendError):
        vcredist.verify(
            path, profile={}, emit=lambda e: None, cancel=SimpleNamespace(cancelled=False),
            constant="a" * 64, index_pins_fn=lambda: {digest}, overridden_fn=lambda: True,
        )
    path.write_bytes(b"not the pinned build")
    vcredist.verify(  # without the override, the index's hash is accepted
        path, profile={}, emit=lambda e: None, cancel=SimpleNamespace(cancelled=False),
        constant="a" * 64, index_pins_fn=lambda: {digest}, overridden_fn=lambda: False,
    )


# ---- GUI ---------------------------------------------------------------------------------------
def test_a_folder_that_cannot_be_read_is_not_called_empty(tmp_path, monkeypatch):
    from nadare.gui import add_game

    folder = tmp_path / "f"
    folder.mkdir()

    def denied(self):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "iterdir", denied)
    assert add_game._new_or_empty(folder) is False


def test_cancelling_the_games_found_question_adds_nothing(xdg, qapp, monkeypatch):
    from nadare.gui.libraries_panel import LibrariesPanel

    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    panel = LibrariesPanel()
    assert panel._choose_box([("Brown Dust 2", True)]) is None
    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    assert panel._choose_box([("Brown Dust 2", True)]) == [True]
    panel.deleteLater()


def test_the_install_dialog_shows_the_unofficial_notice(qapp):
    from PyQt6.QtWidgets import QLabel

    from nadare.gui.quick_install import QuickInstallDialog

    dialog = QuickInstallDialog("G", notice="Read this first.", describe=lambda c: ("/f", "", ""))
    texts = [w.text() for w in dialog.findChildren(QLabel)]
    assert any("unofficial launcher" in t for t in texts)
    assert any("Read this first." in t for t in texts)
    dialog.deleteLater()


def _window(tmp_path):
    from nadare.gui.main_window import MainWindow

    config = Config.load(tmp_path / "config.json")
    config.data["check_notices"] = False
    return MainWindow(config)


def test_package_managed_builds_never_check_the_release_page(tmp_path, qapp, monkeypatch):
    from nadare.core import updatecheck
    from nadare.gui import main_window

    window = _window(tmp_path)
    started = []
    def thread(*a, **k):
        return SimpleNamespace(start=lambda: started.append(k.get("name")))

    monkeypatch.setattr(main_window.threading, "Thread", thread)
    try:
        window.config.data["check_updates"] = True
        monkeypatch.setattr(updatecheck, "managed_updates", lambda: True)
        window._start_update_check()
        assert started == []
        monkeypatch.setattr(updatecheck, "managed_updates", lambda: False)
        window._start_update_check()
        assert started == ["nadare-update-check"]
    finally:
        window.deleteLater()
        qapp.processEvents()


def test_an_update_restart_stops_when_the_window_did_not_close(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path)
    try:
        window.show()
        qapp.processEvents()
        monkeypatch.setattr(window, "close", lambda: False)  # a prompt the user cancelled
        assert window._close_window_for_update() is False
    finally:
        window.hide()
        window.deleteLater()
        qapp.processEvents()


def test_a_pending_operation_waits_for_the_worker_that_holds_the_profile(qapp, monkeypatch):
    from nadare.gui.workers import BackendController

    controller = BackendController(None)
    started = []
    monkeypatch.setattr(controller, "_start_or_wait", lambda *a: started.append(a) or True)
    current = SimpleNamespace(isRunning=lambda: True, operation="install")
    old = SimpleNamespace(isRunning=lambda: False, operation="status")
    controller._workers["g"] = current
    controller._pending["g"] = ("update", None)
    controller._on_worker_finished("g", old)
    assert started == []  # the running install still owns the slot
    controller._workers["g"] = old
    controller._on_worker_finished("g", old)
    assert started == [("g", "update", None)]
