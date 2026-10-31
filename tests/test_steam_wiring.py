# SPDX-License-Identifier: GPL-3.0-or-later
"""The window-side wiring of Steam Set up: the profile's app id, the dialog, the main window."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6 import sip

try:
    from PyQt6.QtWidgets import QApplication
except Exception:  # pragma: no cover - optional GUI dependencies
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import artwork, steamglue, steamrun
from nadare.core.config import Config
from nadare.gui import steam_wiring
from nadare.gui.main_window import MainWindow
from nadare.gui.workers import BackendController

UMA = "3224770"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_real_art(monkeypatch):
    monkeypatch.setattr(artwork, "fetch", lambda *args, **kwargs: None)


# ---- the profile's Steam app id -----------------------------------------------------------
@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        ({"backend": "steam", "steam_app_id": UMA}, UMA),
        ({"backend": "steam", "app_id": UMA}, UMA),
        ({"backend": "steam", "extra": {"steam_app_id": UMA}}, UMA),
        ({"backend": "steam", "extra": {"app_id": f" {UMA} "}}, UMA),
        ({"backend": "steam"}, ""),
        ({"backend": "wine", "steam_app_id": UMA}, ""),
        ({"backend": "steam", "steam_app_id": "12 34"}, ""),
    ],
)
def test_the_steam_app_id_of_a_profile(profile, expected):
    assert steam_wiring.steam_app_id(profile) == expected


# ---- the dialog it builds -----------------------------------------------------------------
def _root(tmp_path: Path) -> Path:
    root = tmp_path / "Steam"
    (root / "steamapps").mkdir(parents=True)
    (root / "config").mkdir()
    return root


def test_the_dialog_plans_from_the_real_steam_files_and_uses_the_given_vulkan(qapp, tmp_path):
    root = _root(tmp_path)
    (root / "steamapps" / f"appmanifest_{UMA}.acf").write_text('"AppState"\n{\n}\n')
    dialog = steam_wiring.build_dialog(
        None, UMA, root=root, vulkan_version_fn=lambda: "1.2.170", async_plan=False,
        confirm_fn=lambda title, text: True, run=lambda argv: 0,
    )
    try:
        states = {key: row for key, row in dialog.rows.items()}
        assert set(states) == {"installed", "h264", "runner"}
        assert dialog.windowTitle().startswith("Set up")
        marker = dialog.rows["runner"].marker_label.text()
        assert marker == "▲"  # old Vulkan: recommended
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_without_steam_the_dialog_is_not_built_but_the_reason_is_plain(qapp):
    with pytest.raises(steam_wiring.SteamUnavailable) as caught:
        steam_wiring.build_dialog(None, UMA, root=None, vulkan_version_fn=lambda: None)
    assert "Steam was not found" in str(caught.value)


def test_the_undo_button_restores_through_the_closed_steam_flow(qapp, tmp_path):
    root = _root(tmp_path)
    calls: list[tuple[str, object]] = []

    def fake_restore(app_id, previous, **kwargs):
        calls.append((app_id, previous))
        return steamrun.ActionResult(True, "restored")

    dialog = steam_wiring.build_dialog(
        None, UMA, root=root, vulkan_version_fn=lambda: None, async_plan=False,
        confirm_fn=lambda title, text: True, run=lambda argv: 0, restore_fn=fake_restore,
    )
    try:
        result = dialog._undo_fn("PREVIOUS")
        assert result.ok and calls == [(UMA, "PREVIOUS")]
    finally:
        dialog.deleteLater()
        qapp.processEvents()


# ---- the main window ----------------------------------------------------------------------
_UMA_PROFILE = {"backend": "steam", "title": "Uma", "steam_app_id": UMA}


def _window(tmp_path: Path, monkeypatch, profile: dict) -> MainWindow:
    def fake_spawn(self, name, operation, payload=None):
        return True

    monkeypatch.setattr(BackendController, "_spawn", fake_spawn)
    config = Config.load(tmp_path / "config.json")
    config.add_profile("uma", **profile)
    config.save()
    return MainWindow(config)


def test_the_set_up_button_opens_the_dialog_for_that_game(tmp_path, qapp, monkeypatch):
    seen: list[tuple[str, dict]] = []

    class FakeDialog:
        def __init__(self) -> None:
            self.shown = False

        def show(self) -> None:
            self.shown = True

        def setAttribute(self, *args) -> None:  # noqa: N802 - Qt naming
            pass

        def finished(self):  # pragma: no cover - not used
            pass

    built: list[FakeDialog] = []

    def fake_build(parent, app_id, **kwargs):
        seen.append((app_id, kwargs))
        dialog = FakeDialog()
        built.append(dialog)
        return dialog

    monkeypatch.setattr(steam_wiring, "build_dialog", fake_build)
    window = _window(tmp_path, monkeypatch, _UMA_PROFILE)
    try:
        window.open_game("uma")
        window.game.steam_setup_requested.emit()
        assert [app for app, _ in seen] == [UMA]
        assert built[0].shown
    finally:
        window.controller._workers.clear()
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_a_missing_steam_is_told_to_the_user_not_raised(tmp_path, qapp, monkeypatch):
    told: list[str] = []

    def fake_build(parent, app_id, **kwargs):
        raise steam_wiring.SteamUnavailable("Steam was not found. Install it.")

    monkeypatch.setattr(steam_wiring, "build_dialog", fake_build)
    window = _window(tmp_path, monkeypatch, _UMA_PROFILE)
    monkeypatch.setattr(window, "_tell_steam_problem", told.append)
    try:
        window.open_game("uma")
        window.game.steam_setup_requested.emit()
        assert told == ["Steam was not found. Install it."]
    finally:
        window.controller._workers.clear()
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_the_installer_constants_are_shared_with_the_czn_backend():
    """Steam Set up reads them from core; CZN re-exports them (its tests patch them there)."""
    from nadare.backends.czn import backend as czn_backend
    from nadare.core import webview2

    assert czn_backend.WEBVIEW2_URL == webview2.DEFAULT_URL
    assert czn_backend.WEBVIEW2_SHA256 == webview2.PINNED_SHA256
    assert webview2.DEFAULT_URL.startswith("https://go.microsoft.com/")
    assert len(webview2.PINNED_SHA256 or "") == 64


# ---- the Steam command, and which Vulkan version to trust ---------------------------------
def test_a_profiles_steam_command_is_read_from_the_top_level_or_extra():
    top = {"steam_binary": " /opt/steam "}
    assert steam_wiring.profile_value(top, "steam_binary") == "/opt/steam"
    assert steam_wiring.profile_value({"extra": {"steam_binary": "x"}}, "steam_binary") == "x"
    assert steam_wiring.profile_value({}, "steam_binary") == ""


def test_the_steam_command_reaches_every_command_the_dialog_runs(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(steamglue, "wait_until", lambda *a, **k: False)  # no 20 s poll
    root = _root(tmp_path)
    (root / "steamapps" / f"appmanifest_{UMA}.acf").write_text('"AppState"\n{\n}\n')
    seen: list[list[str]] = []
    dialog = steam_wiring.build_dialog(
        None, UMA, root=root, vulkan_version_fn=lambda: None, binary="/opt/mysteam --quiet",
        async_plan=False,
        confirm_fn=lambda title, text: True, run=lambda argv: seen.append(argv) or 0,
    )
    try:
        dialog._act_fn(dialog._plan.items[0])  # installed: nothing to do -> unknown, but safe
        item = next(i for i in dialog._plan.items if i.key == "h264")
        dialog._act_fn(item)
        assert seen and seen[0][:2] == ["/opt/mysteam", "--quiet"]
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_a_flatpak_steam_gets_the_flatpak_command_by_default(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(steamglue, "wait_until", lambda *a, **k: False)  # no 20 s poll
    flat = tmp_path / ".var/app/com.valvesoftware.Steam/.local/share/Steam"
    (flat / "steamapps").mkdir(parents=True)
    (flat / "steamapps" / f"appmanifest_{UMA}.acf").write_text('"AppState"\n{\n}\n')
    monkeypatch.setenv("HOME", str(tmp_path))
    seen: list[list[str]] = []
    dialog = steam_wiring.build_dialog(
        None, UMA, root=flat, vulkan_version_fn=lambda: None, async_plan=False,
        confirm_fn=lambda title, text: True, run=lambda argv: seen.append(argv) or 0,
    )
    try:
        item = next(i for i in dialog._plan.items if i.key == "h264")
        dialog._act_fn(item)
        assert seen[0][:3] == ["flatpak", "run", "com.valvesoftware.Steam"]
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_a_loader_only_vulkan_probe_is_not_trusted_as_the_gpus_version(monkeypatch):
    from nadare.core import graphics

    class Info:
        def __init__(self, version, loader_only):
            self.api_version = version
            self.loader_only = loader_only

    monkeypatch.setattr(graphics, "vulkan_info", lambda: Info("1.2.170", True))
    assert steam_wiring._vulkan_version() is None
    monkeypatch.setattr(graphics, "vulkan_info", lambda: Info("1.2.170", False))
    assert steam_wiring._vulkan_version() == "1.2.170"
    monkeypatch.setattr(graphics, "vulkan_info", lambda: Info("1.4.305", True))
    assert steam_wiring._vulkan_version() is None


def test_steam_and_vulkan_are_read_off_the_gui_thread_by_default(qapp, tmp_path):
    import threading
    import time

    root = _root(tmp_path)
    (root / "steamapps" / f"appmanifest_{UMA}.acf").write_text('"AppState"\n{\n}\n')
    main = threading.get_ident()
    probes: list[int] = []

    def slow_vulkan() -> str:
        probes.append(threading.get_ident())
        return "1.4.305"

    dialog = steam_wiring.build_dialog(None, UMA, root=root, vulkan_version_fn=slow_vulkan)
    try:
        assert dialog.rows == {}  # built at once, filled in when the worker has read Steam
        deadline = time.monotonic() + 5
        while not dialog.rows and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.005)
        assert set(dialog.rows) == {"installed", "h264", "runner"}
        assert probes and all(tid != main for tid in probes)
        assert len(probes) == 1  # asked once, not after every step
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_a_second_request_while_a_set_up_window_is_open_shows_that_window(
    tmp_path, qapp, monkeypatch
):
    from nadare.gui.steam_setup import SteamSetupDialog

    built: list[SteamSetupDialog] = []

    def fake_build(parent, app_id, **kwargs):
        dialog = SteamSetupDialog(
            parent, plan_fn=lambda: _one_item_plan(), act_fn=lambda item: None, app_id=app_id
        )
        built.append(dialog)
        return dialog

    monkeypatch.setattr(steam_wiring, "build_dialog", fake_build)
    window = _window(tmp_path, monkeypatch, _UMA_PROFILE)
    try:
        window.open_game("uma")
        window.game.steam_setup_requested.emit()
        window.game.steam_setup_requested.emit()  # a double click
        assert len(built) == 1
        built[0].close()
        qapp.processEvents()
        window.game.steam_setup_requested.emit()  # the first one is gone: a new one opens
        qapp.processEvents()
        assert len(built) == 2
    finally:
        for dialog in built:
            if not sip.isdeleted(dialog):
                dialog.close()
        window.controller._workers.clear()
        window.close()
        window.deleteLater()
        qapp.processEvents()


def _one_item_plan():
    from nadare.core import steamsetup

    item = steamsetup.SetupItem("h264", "H.264 video unlocked", steamsetup.STATE_TODO, action="x")
    return steamsetup.SetupPlan(UMA, "Uma", True, "", (item,))


def test_quitting_during_a_set_up_step_asks_first(tmp_path, qapp, monkeypatch):
    from nadare.gui import steam_setup

    window = _window(tmp_path, monkeypatch, _UMA_PROFILE)
    asked: list[int] = []
    try:
        monkeypatch.setattr(steam_setup, "steps_running", lambda: 0)
        window._ask_steam_step_close = lambda: asked.append(1) or False
        assert window._confirm_close() is True and asked == []
        monkeypatch.setattr(steam_setup, "steps_running", lambda: 1)
        assert window._confirm_close() is False  # the user said no
        assert asked == [1]
        window._ask_steam_step_close = lambda: True
        assert window._confirm_close() is True  # the user chose to close anyway
    finally:
        window.controller._workers.clear()
        window.close()
        window.deleteLater()
        qapp.processEvents()


# ---- install_webview2, end to end with fakes at the process and installer edges -----------
E7 = "5019180"


def _e7_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = _root(tmp_path)
    prefix = root / "steamapps" / "compatdata" / E7 / "pfx"
    (prefix / "drive_c").mkdir(parents=True)
    (root / "steamapps" / f"appmanifest_{E7}.acf").write_text('"AppState"\n{\n}\n')
    proton = root / "steamapps" / "common" / "Proton - Experimental" / "proton"
    proton.parent.mkdir(parents=True)
    proton.write_text("#!/bin/sh\n")
    return root, prefix, proton


class _Edges:
    """What the real installer and process would have been asked to do."""

    def __init__(self, monkeypatch) -> None:
        self.ensure_kwargs: dict = {}
        self.spawned: list[tuple[list[str], dict]] = []
        monkeypatch.setattr(steam_wiring.graphics, "has_display", lambda: True)
        monkeypatch.setattr(steam_wiring.steamfix, "running_blockers", lambda *a: [])

        def fake_ensure(prefix, **kwargs):
            self.ensure_kwargs = {"prefix": prefix, **kwargs}
            kwargs["run_in_prefix"](["C:\\installer.exe", "/silent"])

        class Done:
            returncode = 0

        def fake_run(argv, **kwargs):
            self.spawned.append((list(argv), kwargs))
            return Done()

        monkeypatch.setattr(steam_wiring.webview2, "ensure", fake_ensure)
        monkeypatch.setattr(steam_wiring.subprocess, "run", fake_run)


def test_the_webview2_install_runs_in_the_games_own_prefix_with_the_pinned_hash(
    tmp_path, monkeypatch
):
    root, prefix, proton = _e7_tree(tmp_path)
    edges = _Edges(monkeypatch)
    result = steam_wiring.install_webview2(E7, root=root, run_env={"DISPLAY": ":0"})
    assert result.ok, result.line
    assert edges.ensure_kwargs["prefix"] == prefix
    assert edges.ensure_kwargs["constant"] == steam_wiring.webview2.PINNED_SHA256
    assert edges.ensure_kwargs["url"] == steam_wiring.webview2.DEFAULT_URL
    argv, kwargs = edges.spawned[0]
    assert argv[0] == str(proton) and argv[-2:] == ["C:\\installer.exe", "/silent"]
    assert kwargs["env"]["STEAM_COMPAT_DATA_PATH"] == str(prefix.parent)
    assert kwargs["env"]["STEAM_COMPAT_CLIENT_INSTALL_PATH"] == str(root)
    assert kwargs["env"]["PROTON_ENABLE_WAYLAND"] == "0"
    assert result.backup is not None and result.backup.is_dir()  # the backup was made first
    assert (result.backup / "drive_c").is_dir()


def test_the_webview2_install_does_nothing_while_the_game_runs(tmp_path, monkeypatch):
    root, prefix, _proton = _e7_tree(tmp_path)
    edges = _Edges(monkeypatch)
    monkeypatch.setattr(steam_wiring.steamfix, "running_blockers", lambda *a: ["wineserver"])
    result = steam_wiring.install_webview2(E7, root=root)
    assert not result.ok and "still running" in result.line
    assert edges.spawned == [] and not edges.ensure_kwargs
    assert not list(prefix.parent.glob("*.bak*"))


def test_with_no_display_nothing_is_copied_and_the_reason_is_plain(tmp_path, monkeypatch):
    root, prefix, _proton = _e7_tree(tmp_path)
    edges = _Edges(monkeypatch)
    monkeypatch.setattr(steam_wiring.graphics, "has_display", lambda: False)
    result = steam_wiring.install_webview2(E7, root=root)
    assert not result.ok and "No display" in result.line
    assert not list(prefix.parent.glob("*.bak*")) and not edges.spawned


def test_without_the_games_proton_nothing_is_copied(tmp_path, monkeypatch):
    root, prefix, proton = _e7_tree(tmp_path)
    proton.unlink()
    edges = _Edges(monkeypatch)
    result = steam_wiring.install_webview2(E7, root=root)
    assert not result.ok and "Proton" in result.line
    assert not list(prefix.parent.glob("*.bak*")) and not edges.spawned


# ---- one Set up window, for the right game; quitting waits for a step ---------------------
class _Window:
    """A Set up window as far as the main window can tell."""

    def __init__(self, app_id: str) -> None:
        self.app_id = app_id
        self.events: list[str] = []
        self.visible = True

    def isVisible(self) -> bool:  # noqa: N802 - Qt naming
        return self.visible

    def raise_(self) -> None:
        self.events.append("raise")

    def activateWindow(self) -> None:  # noqa: N802 - Qt naming
        self.events.append("activate")

    def refresh(self) -> None:
        self.events.append("refresh")

    def close(self) -> bool:
        self.events.append("close")
        self.visible = False
        return True

    def show(self) -> None:
        self.events.append("show")

    def setAttribute(self, *args) -> None:  # noqa: N802 - Qt naming
        pass


_TWO_STEAM_GAMES = {
    "uma": {"backend": "steam", "title": "Uma", "steam_app_id": UMA},
    "e7": {"backend": "steam", "title": "E7", "steam_app_id": "5019180"},
}


def _two_game_window(tmp_path, monkeypatch):
    window = _window(tmp_path, monkeypatch, _UMA_PROFILE)
    window.config.add_profile("e7", **_TWO_STEAM_GAMES["e7"])
    built: list[_Window] = []
    monkeypatch.setattr(
        steam_wiring, "build_dialog", lambda parent, app_id, **kw: built.append(_Window(app_id))
        or built[-1]
    )
    return window, built


def test_a_request_for_another_game_replaces_an_idle_set_up_window(tmp_path, qapp, monkeypatch):
    from nadare.gui import steam_setup

    window, built = _two_game_window(tmp_path, monkeypatch)
    monkeypatch.setattr(steam_setup, "steps_running", lambda: 0)
    try:
        window.open_game("uma")
        window._open_steam_setup()
        window.open_game("e7")
        window._open_steam_setup()
        assert [w.app_id for w in built] == [UMA, "5019180"]
        assert "close" in built[0].events  # the old one made way
        assert window._steam_setup_dialog is built[1]
    finally:
        window.controller._workers.clear()
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_a_request_for_another_game_waits_while_a_step_runs(tmp_path, qapp, monkeypatch):
    from nadare.gui import steam_setup

    window, built = _two_game_window(tmp_path, monkeypatch)
    told: list[str] = []
    monkeypatch.setattr(window, "_tell_steam_problem", told.append)
    try:
        window.open_game("uma")
        window._open_steam_setup()
        monkeypatch.setattr(steam_setup, "steps_running", lambda: 1)
        window.open_game("e7")
        window._open_steam_setup()
        assert len(built) == 1 and "close" not in built[0].events
        assert told and "running" in told[0].lower()
    finally:
        monkeypatch.setattr(steam_setup, "steps_running", lambda: 0)
        window.controller._workers.clear()
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_a_second_request_for_the_same_game_refreshes_that_window(tmp_path, qapp, monkeypatch):
    window, built = _two_game_window(tmp_path, monkeypatch)
    try:
        window.open_game("uma")
        window._open_steam_setup()
        window._open_steam_setup()
        assert len(built) == 1
        assert built[0].events[-3:] == ["raise", "activate", "refresh"]
    finally:
        window.controller._workers.clear()
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_a_hidden_close_does_not_quit_under_a_running_set_up_step(tmp_path, qapp, monkeypatch):
    from nadare.gui import steam_setup

    window = _window(tmp_path, monkeypatch, _UMA_PROFILE)
    finished: list[int] = []
    monkeypatch.setattr(window, "_finish_background_close", lambda: finished.append(1))
    try:
        window._quit_after_games = True
        monkeypatch.setattr(steam_setup, "steps_running", lambda: 1)
        window._maybe_finish_background_close()
        qapp.processEvents()
        assert window._quit_after_games and finished == []  # still waiting
        monkeypatch.setattr(steam_setup, "steps_running", lambda: 0)
        window._maybe_finish_background_close()
        qapp.processEvents()
        assert not window._quit_after_games and finished == [1]
    finally:
        monkeypatch.setattr(steam_setup, "steps_running", lambda: 0)
        window.controller._workers.clear()
        window.close()
        window.deleteLater()
        qapp.processEvents()


# ---- wave 2: what the earlier tests let through -------------------------------------------
def test_the_installer_the_wiring_hands_to_webview2_is_checked_against_the_pin(
    tmp_path, monkeypatch
):
    import hashlib

    from nadare.core.backends import BackendError

    root, _prefix, _proton = _e7_tree(tmp_path)
    edges = _Edges(monkeypatch)
    monkeypatch.setattr(steam_wiring.webview2, "index_pins", lambda: set())
    assert steam_wiring.install_webview2(E7, root=root).ok
    verify = edges.ensure_kwargs["verify_fn"]
    evil = tmp_path / "evil.exe"
    evil.write_bytes(b"not microsoft")
    with pytest.raises(BackendError):
        verify(evil)  # bytes that match no pin are never run
    # ...and a file the published index vouches for is accepted (a refused file is removed)
    evil.write_bytes(b"not microsoft")
    monkeypatch.setattr(
        steam_wiring.webview2, "index_pins", lambda: {hashlib.sha256(b"not microsoft").hexdigest()}
    )
    verify(evil)

    seen: dict = {}
    monkeypatch.setattr(
        steam_wiring.webview2, "download", lambda *a, **kw: seen.update(kw)
    )
    edges.ensure_kwargs["download_fn"]("https://x.example/i.exe", tmp_path / "t", tmp_path / "i")
    assert callable(seen["verify_fn"])  # a fresh download is verified before it is kept


def test_the_undo_button_passes_every_seam_the_restore_needs(qapp, tmp_path):
    root = _root(tmp_path)
    seen: dict = {}

    def fake_restore(app_id, previous, **kwargs):
        seen.update(kwargs)
        return steamrun.ActionResult(True, "restored")

    def run(argv):
        return 0

    dialog = steam_wiring.build_dialog(
        None, UMA, root=root, vulkan_version_fn=lambda: None, async_plan=False,
        confirm_fn=lambda title, text: True, run=run, restore_fn=fake_restore,
        binary="/opt/mysteam --quiet",
    )
    try:
        dialog._undo_fn("PREVIOUS")
        assert seen["config_path"] == root / "config" / "config.vdf"
        assert seen["run"] is run
        assert seen["binary"] == ["/opt/mysteam", "--quiet"] or seen["binary"] == (
            "/opt/mysteam --quiet"
        )
        assert callable(seen["steam_running"]) and callable(seen["wait_gone"])
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_every_action_uses_the_configured_steam_command(qapp, tmp_path, monkeypatch):
    from nadare.core import steamglue

    seen: list[tuple[str, object]] = []
    ok = steamrun.ActionResult(True, "x")
    monkeypatch.setattr(
        steamglue.steamrun, "request_steam_install",
        lambda key, **kw: seen.append(("install", kw.get("binary"))) or ok,
    )
    monkeypatch.setattr(
        steamglue.steamrun, "set_runner",
        lambda key, tool, **kw: seen.append(("runner", kw.get("binary"))) or ok,
    )
    monkeypatch.setattr(steamglue, "ensure_steam_tool", lambda root, fetch: root / "tool")
    act = steamglue.make_actions(
        UMA, root=tmp_path, run=lambda argv: 0, binary="/opt/mysteam --quiet",
        ensure_runner_fn=lambda: tmp_path / "tool",
    )
    from nadare.core.steamsetup import SetupItem

    act(SetupItem("installed", "Installed", "todo", action="steam_install"))
    act(SetupItem("runner", "Runner", "todo", action="set_runner"))
    assert seen == [("install", "/opt/mysteam --quiet"), ("runner", "/opt/mysteam --quiet")]


def test_the_profiles_steam_command_is_given_to_the_set_up_dialog(tmp_path, qapp, monkeypatch):
    seen: dict = {}

    class FakeDialog:
        def show(self) -> None:
            pass

        def setAttribute(self, *args) -> None:  # noqa: N802 - Qt naming
            pass

    def fake_build(parent, app_id, **kwargs):
        seen.update(kwargs)
        return FakeDialog()

    monkeypatch.setattr(steam_wiring, "build_dialog", fake_build)
    profile = {**_UMA_PROFILE, "steam_binary": "flatpak run com.valvesoftware.Steam"}
    window = _window(tmp_path, monkeypatch, profile)
    try:
        window.open_game("uma")
        window._open_steam_setup()
        assert seen["binary"] == "flatpak run com.valvesoftware.Steam"
    finally:
        window.controller._workers.clear()
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_the_vulkan_probe_is_not_repeated_by_a_later_plan(qapp, tmp_path):
    root = _root(tmp_path)
    (root / "steamapps" / f"appmanifest_{UMA}.acf").write_text('"AppState"\n{\n}\n')
    probes: list[int] = []
    dialog = steam_wiring.build_dialog(
        None, UMA, root=root, vulkan_version_fn=lambda: probes.append(1) or "1.3.0",
        async_plan=False, confirm_fn=lambda title, text: True, run=lambda argv: 0,
    )
    try:
        dialog._plan_fn()
        dialog._plan_fn()  # what every refresh after a step does
        assert len(probes) == 1
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_the_undo_button_restores_whichever_step_it_belongs_to(qapp, tmp_path):
    from nadare.core import steamwayland

    root = _root(tmp_path)
    cfg = root / "userdata" / "1" / "config"
    cfg.mkdir(parents=True)
    local = cfg / "localconfig.vdf"
    local.write_text(
        '"UserLocalConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n\t\t\t{\n'
        '\t\t\t\t"apps"\n\t\t\t\t{\n\t\t\t\t\t"3224770"\n\t\t\t\t\t{\n'
        '\t\t\t\t\t\t"LaunchOptions"\t\t"PROTON_ENABLE_WAYLAND=1 %command%"\n'
        "\t\t\t\t\t}\n\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n"
    )
    runner_calls: list[object] = []
    dialog = steam_wiring.build_dialog(
        None, UMA, root=root, vulkan_version_fn=lambda: None, async_plan=False,
        confirm_fn=lambda title, text: True, run=lambda argv: 0,
        steam_running=lambda: False, wait_gone=lambda t: True,  # not the host's real Steam
        restore_fn=lambda app_id, previous, **kw: runner_calls.append(previous)
        or steamrun.ActionResult(True, "runner restored"),
    )
    try:
        result = dialog._undo_fn(steamwayland.LaunchUndo(None))  # a Wayland undo
        assert result.ok and runner_calls == []  # it did not go to the runner restore
        assert "PROTON_ENABLE_WAYLAND" not in local.read_text()
        dialog._undo_fn("PREVIOUS")  # a runner undo
        assert runner_calls == ["PREVIOUS"]
    finally:
        dialog.deleteLater()
        qapp.processEvents()


# ---- the undo is kept across windows and restarts -----------------------------------------
_WAYLAND_LOCAL = (
    '"UserLocalConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n\t\t\t{\n'
    '\t\t\t\t"apps"\n\t\t\t\t{\n\t\t\t\t\t"3224770"\n\t\t\t\t\t{\n'
    '\t\t\t\t\t\t"LastPlayed"\t\t"1"\n\t\t\t\t\t}\n\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n'
)


def _wayland_root(tmp_path: Path) -> tuple[Path, Path]:
    root = _root(tmp_path)
    (root / "steamapps" / f"appmanifest_{UMA}.acf").write_text('"AppState"\n{\n}\n')
    cfg = root / "userdata" / "1" / "config"
    cfg.mkdir(parents=True)
    local = cfg / "localconfig.vdf"
    local.write_text(_WAYLAND_LOCAL)
    return root, local


def _wayland_dialog(root: Path, **kwargs):
    return steam_wiring.build_dialog(
        None, UMA, root=root, vulkan_version_fn=lambda: "1.4", async_plan=False,
        confirm_fn=lambda title, text: True, run=lambda argv: 0,
        steam_running=lambda: False, wait_gone=lambda t: True, **kwargs,
    )


def test_a_change_saves_its_undo_and_the_next_window_offers_it(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    root, local = _wayland_root(tmp_path)
    first = _wayland_dialog(root)
    try:
        item = steam_wiring.steamsetup.SetupItem(
            "wayland", "Native Wayland", "optional", action="set_wayland"
        )
        result = first._act_fn(item)
        assert result.ok and result.changed
        assert "PROTON_ENABLE_WAYLAND=1" in local.read_text()
    finally:
        first.deleteLater()
        qapp.processEvents()
    second = _wayland_dialog(root)  # "after a restart": a new window, nothing in memory
    try:
        assert not second.undo_button.isHidden()
        assert second.undo_button.text() == "Undo native Wayland"
        result = second._undo_fn(second._undo_previous)
        assert result.ok
        assert local.read_text() == _WAYLAND_LOCAL  # exactly as it was
        from nadare.core import steamundo

        assert steamundo.load(UMA) is None  # and the kept undo is gone with it
    finally:
        second.deleteLater()
        qapp.processEvents()
    third = _wayland_dialog(root)
    try:
        assert third.undo_button.isHidden()  # a successful undo is not offered again
    finally:
        third.deleteLater()
        qapp.processEvents()


def test_a_change_that_changed_nothing_saves_no_undo(qapp, tmp_path, monkeypatch):
    from nadare.core import steamundo

    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    root, local = _wayland_root(tmp_path)
    local.write_text(
        _WAYLAND_LOCAL.replace(
            '"LastPlayed"\t\t"1"', '"LaunchOptions"\t\t"PROTON_ENABLE_WAYLAND=1 %command%"'
        )
    )
    dialog = _wayland_dialog(root)
    try:
        item = steam_wiring.steamsetup.SetupItem(
            "wayland", "Native Wayland", "optional", action="set_wayland"
        )
        assert not dialog._act_fn(item).changed
        assert steamundo.load(UMA) is None
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_a_failed_undo_keeps_the_saved_undo(qapp, tmp_path, monkeypatch):
    from nadare.core import steamundo, steamwayland

    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    root, local = _wayland_root(tmp_path)
    steamundo.save(UMA, "set_wayland", steamwayland.LaunchUndo(None))
    local.write_text(
        _WAYLAND_LOCAL.replace(
            '"LastPlayed"\t\t"1"', '"LaunchOptions"\t\t"PROTON_ENABLE_WAYLAND=1 %command%"'
        )
    )
    dialog = steam_wiring.build_dialog(
        None, UMA, root=root, vulkan_version_fn=lambda: "1.4", async_plan=False,
        confirm_fn=lambda title, text: True, run=lambda argv: 0,
        steam_running=lambda: True, wait_gone=lambda t: False,  # Steam will not close
    )
    try:
        result = dialog._undo_fn(steamwayland.LaunchUndo(None))
        assert not result.ok
        assert steamundo.load(UMA) is not None
    finally:
        dialog.deleteLater()
        qapp.processEvents()


# ---- Cancel reaches the token the running step watches ------------------------------------
def test_the_webview2_install_hands_its_cancel_token_to_the_installer(tmp_path, monkeypatch):
    from nadare.core.events import Cancel

    root, _prefix, _proton = _e7_tree(tmp_path)
    edges = _Edges(monkeypatch)
    token = Cancel()
    assert steam_wiring.install_webview2(E7, root=root, cancel=token).ok
    assert edges.ensure_kwargs["cancel"] is token


def test_a_cancel_before_the_installer_runs_leaves_the_prefix_backed_up_and_untouched(
    tmp_path, monkeypatch
):
    from nadare.core.events import Cancel

    root, prefix, _proton = _e7_tree(tmp_path)
    edges = _Edges(monkeypatch)
    token = Cancel()
    token.cancel()
    result = steam_wiring.install_webview2(E7, root=root, cancel=token)
    assert not result.ok and "Cancelled" in result.line
    assert not edges.spawned and not edges.ensure_kwargs  # the installer never started


def test_the_dialogs_cancel_stops_the_step_that_is_running(qapp, tmp_path, monkeypatch):
    seen: list = []
    real = steam_wiring.steamglue.make_actions

    def spy(*args, **kwargs):
        seen.append(kwargs.get("cancel"))
        return real(*args, **kwargs)

    monkeypatch.setattr(steam_wiring.steamglue, "make_actions", spy)
    root = _root(tmp_path)
    dialog = steam_wiring.build_dialog(
        None, UMA, root=root, vulkan_version_fn=lambda: None, async_plan=False,
        confirm_fn=lambda title, text: True, run=lambda argv: 0,
        steam_running=lambda: False, wait_gone=lambda t: True,
    )
    try:
        item = steam_wiring.steamsetup.SetupItem("x", "x", "todo", action="steam_install")
        dialog._act_fn(item)
        first = seen[-1]
        assert first is not None and not first.cancelled
        dialog._cancel_fn()
        assert first.cancelled
        dialog._act_fn(item)  # the next step starts with a fresh token
        assert seen[-1] is not first and not seen[-1].cancelled
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_the_prefix_step_the_dialog_runs_watches_the_dialogs_cancel(qapp, tmp_path, monkeypatch):
    seen: list = []

    def fake_install(game, *, root, cancel=None, **kw):
        seen.append(cancel)
        return steamrun.ActionResult(True, "x")

    monkeypatch.setattr(steam_wiring, "install_webview2", fake_install)
    root = _root(tmp_path)
    (root / "steamapps" / f"appmanifest_{E7}.acf").write_text('"AppState"\n{\n}\n')
    dialog = steam_wiring.build_dialog(
        None, E7, root=root, vulkan_version_fn=lambda: None, async_plan=False,
        confirm_fn=lambda title, text: True, run=lambda argv: 0,
        steam_running=lambda: False, wait_gone=lambda t: True,
    )
    try:
        item = steam_wiring.steamsetup.SetupItem(
            "prefix:webview2", "WebView2", "todo", action="install_prefix:webview2"
        )
        dialog._act_fn(item)
        assert seen and seen[0] is not None and not seen[0].cancelled
        dialog._cancel_fn()
        assert seen[0].cancelled  # the same token the installer is watching
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_a_half_installed_webview2_is_installed_again_not_skipped(tmp_path, monkeypatch):
    root, prefix, _proton = _e7_tree(tmp_path)
    edges = _Edges(monkeypatch)
    app = prefix / "drive_c/Program Files (x86)/Microsoft/EdgeWebView/Application/150.0.1"
    app.mkdir(parents=True)
    (app / "msedgewebview2.exe").write_bytes(b"MZ")  # files, but no registration
    assert steam_wiring.install_webview2(E7, root=root).ok
    installed = edges.ensure_kwargs["installed_fn"]
    assert installed() is False  # so the installer really runs
    (prefix / "system.reg").write_text(
        "WINE REGISTRY Version 2\n\n"
        "[Software\\\\Wow6432Node\\\\Microsoft\\\\EdgeUpdate\\\\Clients\\\\"
        '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}] 1\n"pv"="150.0.1"\n'
    )
    assert installed() is True
