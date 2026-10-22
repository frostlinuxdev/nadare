# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for a sweep of CZN / Add-game fixes.

Each test is hermetic: no Wine, no container engine and no network.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from _backend_helpers import make_context

from nadare.backends import arknights as ak
from nadare.backends.czn import auth as czn_auth
from nadare.backends.czn import runners as czn_runners
from nadare.backends.czn import update as czn_update
from nadare.backends.czn.backend import CznBackend
from nadare.core.config import Config
from nadare.core.events import Cancelled
from nadare.gui.add_game import AddGameDialog, detect_import

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _dialog(tmp_path: Path) -> tuple[Config, AddGameDialog]:
    config = Config.load(tmp_path / "config.json")
    return config, AddGameDialog(config, data_root=tmp_path / "data")


# ---------------------------------------------------------------------------
# 1. Prefix-only kinds do not ask for / store a separate install folder
# ---------------------------------------------------------------------------
class TestCznLocationRows:
    def test_import_hides_install_folder_and_labels_prefix_as_game_folder(self, tmp_path, qapp):
        config, dialog = _dialog(tmp_path)
        try:
            dialog.show()
            dialog.set_backend("stove-czn")
            dialog.set_mode(True)
            qapp.processEvents()

            assert not dialog.folder.isVisible(), "a prefix-only kind has no install folder"
            assert dialog.prefix.isVisible(), "an import must still ask for the prefix"
            label = dialog._form.labelForField(dialog.prefix_row)
            assert label is not None
            assert label.text() == "Game folder"
            assert "Wine prefix" in (label.toolTip() + dialog.prefix.toolTip())

            prefix = tmp_path / "czn-prefix"
            (prefix / "drive_c").mkdir(parents=True)
            dialog.set_prefix(prefix)
            assert dialog.create_profile() == "chaos-zero-nightmare"
            profile = config.profile("chaos-zero-nightmare")
            assert profile["prefix"] == str(prefix)
            assert profile.get("install_dir") == ""
        finally:
            dialog.close()
            qapp.processEvents()

    def test_install_new_hides_install_folder_and_stores_no_install_dir(self, tmp_path, qapp):
        config, dialog = _dialog(tmp_path)
        try:
            dialog.show()
            dialog.set_backend("stove-czn")
            assert dialog.install_new.isChecked()
            qapp.processEvents()

            assert not dialog.folder.isVisible()
            # The prefix is still created by the backend; its default is kept.
            assert dialog.create_profile() == "chaos-zero-nightmare"
            profile = config.profile("chaos-zero-nightmare")
            assert profile.get("install_dir") == ""
            assert profile["prefix"].endswith("/prefix")
        finally:
            dialog.close()
            qapp.processEvents()


# ---------------------------------------------------------------------------
# 2. Picking a folder under drive_c adopts the prefix that contains it
# ---------------------------------------------------------------------------
class TestDriveCFolderAdoption:
    def test_detect_import_walks_up_to_the_prefix_from_a_game_folder(self, tmp_path):
        prefix = tmp_path / "prefix"
        install = prefix / "drive_c" / "YostarGames" / "Arknights_EN"
        install.mkdir(parents=True)
        (install / "Arknights.exe").write_text("MZ")

        info = detect_import("arknights-yostar", install, None)
        assert info["prefix"] == str(prefix)
        assert info["install_dir"] == str(install)
        assert any("prefix" in note.lower() for note in info["notes"])

    def test_detect_import_adopts_prefix_for_a_prefix_only_kind(self, tmp_path):
        prefix = tmp_path / "prefix"
        game = prefix / "drive_c" / "ProgramData" / "Smilegate" / "Games" / "ChaosZeroNightmare"
        game.mkdir(parents=True)

        info = detect_import("stove-czn", game, None)
        assert info["prefix"] == str(prefix)
        assert any("prefix" in note.lower() for note in info["notes"])

    def test_import_from_the_game_folder_keeps_the_prefix_and_shows_the_hint(
        self, tmp_path, qapp
    ):
        prefix = tmp_path / "prefix"
        install = prefix / "drive_c" / "YostarGames" / "Arknights_EN"
        install.mkdir(parents=True)
        (install / "Arknights.exe").write_text("MZ")

        config, dialog = _dialog(tmp_path)
        try:
            dialog.set_backend("arknights-yostar")
            dialog.set_mode(True)
            dialog.set_install_dir(install)
            dialog.detect()
            assert str(prefix) in dialog.detect_label.text()
            assert dialog.create_profile() == "arknights"
            profile = config.profile("arknights")
            assert profile["install_dir"] == str(install)
            assert profile["prefix"] == str(prefix)
        finally:
            dialog.deleteLater()
            qapp.processEvents()


# ---------------------------------------------------------------------------
# 3. CZN helper defaults honour XDG when the CZN_* overrides are absent
# ---------------------------------------------------------------------------
def _force_paths_import_failure(monkeypatch) -> None:
    """Make ``from nadare.core import paths`` fail, exercising the fallbacks."""
    import nadare.core as core

    monkeypatch.delattr(core, "paths", raising=False)
    monkeypatch.setitem(sys.modules, "nadare.core.paths", None)


class TestXdgFallbacks:
    def test_helpers_resolve_under_xdg_without_czn_overrides(self, tmp_path, monkeypatch):
        data = tmp_path / "xdg-data"
        cache = tmp_path / "xdg-cache"
        monkeypatch.setenv("XDG_DATA_HOME", str(data))
        monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
        for var in (
            "CZN_RUNNERS_DIR",
            "CZN_CACHE_DIR",
            "CZN_PREFIX",
            "CZN_STATE_DIR",
        ):
            monkeypatch.delenv(var, raising=False)
        _force_paths_import_failure(monkeypatch)

        assert czn_runners._default_runners_dir() == data / "nadare" / "runners"
        assert czn_runners._default_cache_dir() == cache / "nadare" / "czn"
        assert czn_update._default_prefix() == data / "nadare" / "czn" / "prefix"
        assert czn_auth._default_state_dir() == data / "nadare" / "czn"


# ---------------------------------------------------------------------------
# 4. build_stub.sh creates a missing output directory before changing into it
# ---------------------------------------------------------------------------
def test_build_stub_creates_the_output_dir_before_the_container_cd(tmp_path):
    script = REPO_ROOT / "src" / "nadare" / "backends" / "czn" / "build_stub.sh"
    text = script.read_text()
    branch = text.index('if [ "${1:-}" = "--container" ]')
    mkdir_at = text.index('mkdir -p "$(dirname "$out")"', branch)
    cd_at = text.index('outdir="$(cd', branch)
    assert mkdir_at < cd_at, "the container branch must create the out dir before cd"

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "args.log"
    podman = fake_bin / "podman"
    podman.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$FAKE_PODMAN_LOG"\nexit 0\n')
    podman.chmod(0o755)

    out = tmp_path / "missing" / "nested" / "czn_pcsdk_stub.exe"
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:/usr/bin:/bin"
    env["FAKE_PODMAN_LOG"] = str(log)
    result = subprocess.run(
        ["bash", str(script), "--container", str(out)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert out.parent.is_dir(), "the script must create the missing output directory"
    args = log.read_text().splitlines()
    assert f"{out.parent.resolve()}:/out:Z" in args


# ---------------------------------------------------------------------------
# 5. Arknights: a failed/cancelled wineboot never leaves a half-built prefix
# ---------------------------------------------------------------------------
class TestArknightsPrefixCleanup:
    def _backend(self, tmp_path: Path, prefix: Path) -> ak.ArknightsBackend:
        return ak.ArknightsBackend(
            make_context(
                tmp_path,
                ak.KIND,
                {"prefix": str(prefix), "wine_binary": "/usr/bin/wine"},
            )
        )

    def test_cancelled_wineboot_removes_a_prefix_it_created_and_retry_runs_again(
        self, tmp_path, monkeypatch
    ):
        prefix = tmp_path / "prefix"
        backend = self._backend(tmp_path, prefix)
        calls: list[int] = []
        monkeypatch.setattr(ak.vcredist, "ensure", lambda *a, **k: None)

        def fake_stream(argv, env, on_line=None, cancel=None):
            calls.append(1)
            (prefix / "drive_c").mkdir(parents=True, exist_ok=True)
            raise Cancelled()

        monkeypatch.setattr(ak, "stream", fake_stream)
        with pytest.raises(Cancelled):
            backend._ensure_prefix()
        assert not prefix.exists(), "a half-built prefix must not look complete"

        # The retry really runs wineboot again instead of trusting the leftovers.
        def succeeding_stream(argv, env, on_line=None, cancel=None):
            calls.append(1)
            (prefix / "drive_c").mkdir(parents=True, exist_ok=True)
            return 0

        monkeypatch.setattr(ak, "stream", succeeding_stream)
        backend._ensure_prefix()
        assert calls == [1, 1]
        assert (prefix / "drive_c").is_dir()

    def test_an_existing_prefix_is_never_deleted_on_failure(self, tmp_path, monkeypatch):
        prefix = tmp_path / "prefix"
        prefix.mkdir()
        (prefix / "keep-me").write_text("user data")
        backend = self._backend(tmp_path, prefix)

        def fake_stream(argv, env, on_line=None, cancel=None):
            (prefix / "drive_c").mkdir(parents=True, exist_ok=True)
            raise Cancelled()

        monkeypatch.setattr(ak, "stream", fake_stream)
        with pytest.raises(Cancelled):
            backend._ensure_prefix()
        assert (prefix / "keep-me").read_text() == "user data"
        assert prefix.is_dir()


# ---------------------------------------------------------------------------
# 6. The X11/XWayland display option warns about the blank window
# ---------------------------------------------------------------------------
def test_czn_display_help_warns_about_xwayland():
    display = next(option for option in CznBackend.options if option.key == "display")
    assert "XWayland" in display.help
    assert "blank window" in display.help
    assert "Auto" in display.help


def test_czn_import_adopts_the_prefix_when_the_game_folder_is_picked_in_the_prefix_row(
    tmp_path,
) -> None:
    from nadare.gui.add_game import detect_import

    prefix = tmp_path / "pfx"
    game = prefix / "drive_c" / "Program Files" / "Chaos"
    game.mkdir(parents=True)
    info = detect_import("stove-czn", None, game)
    assert info["prefix"] == str(prefix)
