# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for a sweep of CZN / Add-game fixes.

Each test is hermetic: no Wine, no container engine and no network.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from _backend_helpers import make_context
from _parts_patch import patch_everywhere

from nadare.backends import arknights as ak
from nadare.backends import winegame as wg
from nadare.backends.czn import auth as czn_auth
from nadare.backends.czn import update as czn_update
from nadare.backends.czn.backend import CznBackend
from nadare.core.config import Config
from nadare.core.events import Cancelled
from nadare.gui.add_game import AddGameDialog

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
    def test_install_new_hides_install_folder_and_stores_no_install_dir(self, tmp_path, qapp):
        config, dialog = _dialog(tmp_path)
        try:
            dialog.show()
            dialog.set_backend("stove-czn")
            qapp.processEvents()

            assert not dialog.folder.isVisible()
            # The prefix is still created by the backend; its default is kept.
            assert dialog.create_profile() == "czn"
            profile = config.profile("czn")
            assert profile.get("install_dir") == ""
            assert profile["prefix"].endswith("/prefix")
        finally:
            dialog.close()
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

        assert czn_update._default_prefix() == data / "nadare" / "czn" / "prefix"
        assert czn_auth._default_state_dir() == data / "nadare" / "czn"


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
        monkeypatch.setattr(wg.vcredist, "ensure", lambda *a, **k: None)

        def fake_stream(argv, env, on_line=None, cancel=None, before_stop=None):
            calls.append(1)
            (prefix / "drive_c").mkdir(parents=True, exist_ok=True)
            raise Cancelled()

        patch_everywhere(monkeypatch, wg, "stream", fake_stream)
        with pytest.raises(Cancelled):
            backend._ensure_prefix()
        assert not prefix.exists(), "a half-built prefix must not look complete"

        # The retry really runs wineboot again instead of trusting the leftovers.
        def succeeding_stream(argv, env, on_line=None, cancel=None):
            calls.append(1)
            (prefix / "drive_c").mkdir(parents=True, exist_ok=True)
            return 0

        patch_everywhere(monkeypatch, wg, "stream", succeeding_stream)
        backend._ensure_prefix()
        assert calls == [1, 1]
        assert (prefix / "drive_c").is_dir()

    def test_an_existing_prefix_is_never_deleted_on_failure(self, tmp_path, monkeypatch):
        prefix = tmp_path / "prefix"
        prefix.mkdir()
        (prefix / "keep-me").write_text("user data")
        backend = self._backend(tmp_path, prefix)

        def fake_stream(argv, env, on_line=None, cancel=None, before_stop=None):
            (prefix / "drive_c").mkdir(parents=True, exist_ok=True)
            raise Cancelled()

        patch_everywhere(monkeypatch, wg, "stream", fake_stream)
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
