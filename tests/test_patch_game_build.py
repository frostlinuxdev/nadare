# SPDX-License-Identifier: GPL-3.0-or-later
"""A patch knows the game build it is made for (#1895), and says so when the game moved on (#1894).

Seen 2026-11-27: Azur Promilia hot-updated its own resources while running (3219598 -> 3273853).
The English patch was still recorded as applied, status said "partial", and only an update with a
newer index pin said the real reason. The build number is in the manifest file names, so it can be
read without hashing.
"""

from __future__ import annotations

import dataclasses
import os
import time
from pathlib import Path

import pytest

# Reuse the update tests' fake game, specs and fixture.
from test_patch_update import KIND, NEW_GAME_BUILD, PATCH_ID, env  # noqa: F401

from nadare import app
from nadare.core import backends, patches, patchops, patchspecs
from nadare.core.config import Config

OLD, NEW = "3219598", "3273853"


def _manifest(root: Path, pkg: str, build: str, *, base: str = "AzurPromiliaJP_Data/.res") -> None:
    folder = root / base / pkg / "ManifestFiles"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"0release_{pkg}_{build}.bytes").write_bytes(b"m")
    (folder / f"0release_{pkg}_{build}.hash").write_bytes(b"h")


# --------------------------------------------------------------------------- the probe


def test_azur_build_is_the_highest_manifest_number(tmp_path):
    # The real 11-27 install: raw_base keeps its old manifest next to the new one.
    for pkg in ("default_package", "raw_streaming_package", "raw_nostreaming_package"):
        _manifest(tmp_path, pkg, NEW)
    _manifest(tmp_path, "raw_base_package", OLD)
    _manifest(tmp_path, "raw_base_package", NEW)
    assert patchspecs.installed_build("azurpromilia-jp-yostar", tmp_path) == NEW


def test_azur_build_reads_the_shipped_manifests_before_any_hot_update(tmp_path):
    _manifest(tmp_path, "default_package", OLD, base="AzurPromiliaJP_Data/StreamingAssets/.res")
    assert patchspecs.installed_build("azurpromilia-jp-yostar", tmp_path) == OLD


@pytest.mark.parametrize(
    "name",
    [
        "0release_default_package_.bytes",
        "0release_default_package_12a.bytes",
        "readme.bytes",
        "0release_default_package_3273853.hash",
    ],
)
def test_azur_build_ignores_other_names(tmp_path, name):
    folder = tmp_path / "AzurPromiliaJP_Data/.res/default_package/ManifestFiles"
    folder.mkdir(parents=True)
    (folder / name).write_bytes(b"x")
    assert patchspecs.installed_build("azurpromilia-jp-yostar", tmp_path) is None


def test_build_is_unknown_for_a_kind_without_a_probe_or_an_empty_install(tmp_path):
    assert patchspecs.installed_build("azurpromilia-jp-yostar", tmp_path) is None
    assert patchspecs.installed_build("no-such-kind", tmp_path) is None
    assert patchspecs.installed_build("azurpromilia-jp-yostar", tmp_path / "missing") is None


def test_the_probe_only_lists_folders(tmp_path, monkeypatch):
    _manifest(tmp_path, "default_package", NEW)
    monkeypatch.setattr(
        patches, "_hash_file", lambda *a, **k: pytest.fail("the build probe must not hash")
    )
    assert patchspecs.installed_build("azurpromilia-jp-yostar", tmp_path) == NEW


# --------------------------------------------------------------------------- the pin


def test_kibotl_pins_the_build_its_version_names():
    # The upstream tag still says 3219598 (KiboTL re-uploads under it); the version names
    # the build the pinned upload fits.
    spec = patchspecs.KIBOTL_EN
    assert spec.game_build == NEW
    assert spec.version.endswith("-" + spec.game_build)


@pytest.mark.parametrize("bad", ["", "abc", "12 3", "-1", "1" * 21, 3219598])
def test_a_spec_refuses_a_bad_game_build(bad):
    with pytest.raises(ValueError):
        dataclasses.replace(patchspecs.KIBOTL_EN, game_build=bad)


def _index_doc(**extra):
    spec = patchspecs.KIBOTL_EN
    doc = {
        "schema": 1,
        "id": spec.id,
        "kind": "azurpromilia-jp-yostar",
        "version": spec.version,
        "url": spec.url,
        "archive_sha256": spec.archive_sha256,
        "archive_size": 123456,
        "files": [
            {
                "member": f.member,
                "dest": f.dest,
                "sha256": f.sha256,
                "size": f.size,
                "before_sha256": f.before_sha256,
                "before_size": f.before_size,
            }
            for f in spec.files
        ],
    }
    doc.update(extra)
    return doc


def test_an_index_pin_carries_its_game_build():
    spec = patchspecs.spec_from_index(patchspecs.KIBOTL_EN, _index_doc(game_build=NEW))
    assert spec is not None and spec.game_build == NEW


def test_an_index_pin_without_a_game_build_does_not_inherit_the_builtin_one():
    # A newer pin may be for a newer build: guessing the old number would refuse a good patch.
    spec = patchspecs.spec_from_index(patchspecs.KIBOTL_EN, _index_doc())
    assert spec is not None and spec.game_build is None


@pytest.mark.parametrize("bad", ["abc", 3273853, "", "../1"])
def test_an_index_pin_with_a_bad_game_build_is_rejected(bad):
    assert patchspecs.spec_from_index(patchspecs.KIBOTL_EN, _index_doc(game_build=bad)) is None


# --------------------------------------------------------------------------- apply / update / state


@pytest.fixture
def built(env, monkeypatch):  # noqa: F811 - the fixture imported above
    """``env`` with a game build the test can move, and specs pinned to OLD."""
    build = {"now": OLD}
    monkeypatch.setitem(patchspecs.BUILD_PROBES, KIND, lambda root: build["now"])
    for key in ("spec_v1", "spec_v2"):
        env[key] = dataclasses.replace(env[key], game_build=OLD)
    env["current"]["spec"] = env["spec_v1"]
    env["build"] = build
    return env


def test_apply_refuses_another_build_before_downloading(built):
    backend, root = built["backend"], built["root"]
    built["build"]["now"] = NEW
    with pytest.raises(backends.BackendError) as caught:
        patchops.apply(backend, PATCH_ID)
    msg = str(caught.value)
    assert OLD in msg and NEW in msg
    assert built["fetched"] == []
    assert patches.applied_patches(root) == ()


def test_apply_goes_ahead_on_the_build_it_is_made_for(built):
    patchops.apply(built["backend"], PATCH_ID)
    assert patchops.state(built["backend"], PATCH_ID) == "applied"


def test_apply_goes_ahead_when_the_build_cannot_be_read(built):
    built["build"]["now"] = None
    patchops.apply(built["backend"], PATCH_ID)
    assert patchops.state(built["backend"], PATCH_ID) == "applied"


def test_apply_goes_ahead_when_the_pin_names_no_build(built):
    built["current"]["spec"] = dataclasses.replace(built["spec_v1"], game_build=None)
    built["build"]["now"] = NEW
    patchops.apply(built["backend"], PATCH_ID)
    assert patchops.state(built["backend"], PATCH_ID) == "applied"


def test_update_after_the_game_updated_itself_names_both_builds(built):
    backend, root = built["backend"], built["root"]
    patchops.apply(backend, PATCH_ID)
    fetched = len(built["fetched"])
    backend.update()  # the game rewrites the file itself
    built["build"]["now"] = NEW
    built["current"]["spec"] = built["spec_v2"]
    with pytest.raises(backends.BackendError) as caught:
        patchops.update(backend, PATCH_ID)
    msg = str(caught.value)
    assert OLD in msg and NEW in msg
    assert len(built["fetched"]) == fetched
    assert (root / "Data/table.r").read_bytes() == NEW_GAME_BUILD


def test_state_is_undone_when_the_game_moved_to_another_build(built):
    backend = built["backend"]
    patchops.apply(backend, PATCH_ID)
    built["build"]["now"] = NEW
    assert patchops.state(backend, PATCH_ID) == "undone"
    # A newer pin for the old build does not make it "outdated": updating cannot help.
    built["current"]["spec"] = built["spec_v2"]
    assert patchops.state(backend, PATCH_ID, spec=built["spec_v2"]) == "undone"


def test_state_is_undone_when_the_game_rewrote_the_files_even_without_a_build_number(built):
    backend = built["backend"]
    patchops.apply(backend, PATCH_ID)
    built["build"]["now"] = None
    backend.update()
    assert patchops.state(backend, PATCH_ID) == "undone"


def test_state_is_wrong_build_when_nothing_is_applied(built):
    built["build"]["now"] = NEW
    assert patchops.state(built["backend"], PATCH_ID) == "wrong-build"


def test_build_mismatch_is_cheap_and_names_both(built, monkeypatch):
    monkeypatch.setattr(
        patches, "_hash_file", lambda *a, **k: pytest.fail("the page check must not hash")
    )
    assert patchops.build_mismatch(built["backend"], built["spec_v1"]) is None
    built["build"]["now"] = NEW
    assert patchops.build_mismatch(built["backend"], built["spec_v1"]) == (OLD, NEW)


def test_page_note_says_the_game_updated_and_the_patch_waits(built):
    backend = built["backend"]
    assert patchops.page_note(backend, built["spec_v1"]) == ""
    built["build"]["now"] = NEW
    note = patchops.page_note(backend, built["spec_v1"])
    assert OLD in note and NEW in note
    assert "game" in note.lower()


def test_remove_after_the_game_moved_on_keeps_the_new_files(built):
    # The 11-27 question: turning the switch off must not put old-build files back.
    backend, root = built["backend"], built["root"]
    patchops.apply(backend, PATCH_ID)
    backend.update()
    built["build"]["now"] = NEW
    patchops.remove(backend, PATCH_ID)
    assert (root / "Data/table.r").read_bytes() == NEW_GAME_BUILD
    assert patches.applied_patches(root) == ()


def test_cli_status_names_both_builds(built, tmp_path, monkeypatch, capsys):
    backend = built["backend"]
    patchops.apply(backend, PATCH_ID)
    built["build"]["now"] = NEW
    cfg_path = tmp_path / "config.json"
    config = Config.load(cfg_path)
    config.add_profile("azur", backend="wine", title="Azur")
    config.save()
    monkeypatch.setattr(app, "create_backend", lambda *a, **k: backend)
    assert app.run_cli(["-c", str(cfg_path), "patch", "status", "-p", "azur"]) == 0
    out = capsys.readouterr().out
    assert "undone" in out
    assert OLD in out and NEW in out


# --------------------------------------------------------------------------- the game page

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def view():
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    from nadare.gui.game_view import GameView

    QApplication.instance() or QApplication([])
    page = GameView()
    yield page
    page.close()
    page.deleteLater()


def test_the_page_shows_the_note_and_hides_update(view):
    from nadare.core.backends import Status

    view.set_profile("azur", {"backend": "azurpromilia-jp-yostar"})
    view.set_status(Status(state="installed"))
    view.set_patch(True, outdated=True, note="The game updated itself.")
    assert not view.patch_note.isHidden()
    assert view.patch_note.text() == "The game updated itself."
    assert view.patch_update_btn.isHidden()
    view.set_patch(True, outdated=True)
    assert view.patch_note.isHidden()
    assert not view.patch_update_btn.isHidden()


def test_the_window_passes_the_build_note(tmp_path, monkeypatch):
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    from nadare.core import holds
    from nadare.core.backends import Status
    from nadare.gui.main_window import MainWindow

    app_ = QApplication.instance() or QApplication([])
    monkeypatch.setenv("NADARE_INDEX_BASE", "")
    monkeypatch.setattr(holds, "load", lambda *a, **k: [])
    install_dir = tmp_path / "game"
    install_dir.mkdir()
    (install_dir / "AzurPromiliaJP.exe").write_bytes(b"stub")
    _manifest(install_dir, "default_package", OLD)  # the pin is for NEW
    record = patches.PatchRecord(
        id="kibotl-en",
        version=patchspecs.KIBOTL_EN.version,
        archive_sha256=patchspecs.KIBOTL_EN.archive_sha256,
        applied_at=time.time(),
        files=(),
    )
    patches._save_state(install_dir, {"kibotl-en": record})
    config = Config.load(tmp_path / "config.json")
    config.profiles["azur"] = {"backend": "azurpromilia-jp-yostar", "install_dir": str(install_dir)}
    window = MainWindow(config)
    try:
        window._current_profile = "azur"
        window.game.set_profile("azur", config.profiles["azur"])
        window.game.set_status(Status(state="installed"))
        window._sync_patch("azur")
        text = window.game.patch_note.text()
        assert not window.game.patch_note.isHidden()
        assert OLD in text and NEW in text
    finally:
        window.close()
        window.deleteLater()
        app_.processEvents()
