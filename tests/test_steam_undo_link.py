# SPDX-License-Identifier: GPL-3.0-or-later
"""#1031: Undo of the runner step also removes the DW-Proton link Nadare made, when unused.

The runner step links Nadare's runner into Steam's ``compatibilitytools.d``; Undo put the old
runner back in ``config.vdf`` but left the link, so Steam kept listing a tool nothing used. The
link goes only when it is Nadare's own (a symlink into Nadare's runners folder) and no game in
the ``CompatToolMapping`` still uses it.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest

from nadare.core import paths, steamglue, steamrun, steamtools

TOOL = "dwproton-11.0-15-x86_64"


def _config(*mappings: tuple[str, str]) -> str:
    entries = "".join(
        f'\t\t\t\t\t"{app}"\n\t\t\t\t\t{{\n\t\t\t\t\t\t"name"\t\t"{tool}"\n'
        f'\t\t\t\t\t\t"config"\t\t""\n\t\t\t\t\t\t"Priority"\t\t"250"\n\t\t\t\t\t}}\n'
        for app, tool in mappings
    )
    return (
        '"InstallConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n\t\t\t{\n'
        '\t\t\t\t"CompatToolMapping"\n\t\t\t\t{\n' + entries + "\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n"
    )


@pytest.fixture
def steam(tmp_path):
    root = tmp_path / "Steam"
    (root / "config").mkdir(parents=True)
    runner = paths.runners_dir() / TOOL
    runner.mkdir(parents=True)
    (runner / "proton").write_text("#!/bin/sh\n")
    assert steamglue.ensure_steam_tool(root, runner) == TOOL
    return root, runner


def test_tools_in_use_lists_every_mapped_tool():
    text = _config(("1", TOOL), ("2", "proton_9"), ("3", TOOL))
    assert steamtools.tools_in_use(text) == {TOOL, "proton_9"}
    assert steamtools.tools_in_use("garbage") == set()


def test_an_unused_nadare_link_is_removed(steam):
    root, runner = steam
    link = steamglue.tool_dir(root) / TOOL
    assert link.is_symlink()
    assert steamglue.remove_unused_tool(root, TOOL, _config(("5019180", "proton_9"))) is True
    assert not link.exists() and not link.is_symlink()
    assert runner.is_dir()  # the runner itself is Nadare's and stays


def test_a_link_another_game_still_uses_stays(steam):
    root, _runner = steam
    assert steamglue.remove_unused_tool(root, TOOL, _config(("42", TOOL))) is False
    assert (steamglue.tool_dir(root) / TOOL).is_symlink()


def test_a_tool_that_is_not_nadares_link_is_never_touched(tmp_path):
    root = tmp_path / "Steam"
    real = steamglue.tool_dir(root) / TOOL
    real.mkdir(parents=True)  # a real folder the user installed
    assert steamglue.remove_unused_tool(root, TOOL, _config()) is False
    assert real.is_dir()
    other = tmp_path / "elsewhere" / "GE-Proton9-1"
    other.mkdir(parents=True)
    (steamglue.tool_dir(root) / "GE-Proton9-1").symlink_to(other)
    assert steamglue.remove_unused_tool(root, "GE-Proton9-1", _config()) is False
    assert (steamglue.tool_dir(root) / "GE-Proton9-1").is_symlink()


def test_an_unreadable_mapping_keeps_the_link(steam):
    root, _runner = steam
    assert steamglue.remove_unused_tool(root, TOOL, None) is False
    assert (steamglue.tool_dir(root) / TOOL).is_symlink()


def test_undo_through_the_window_removes_the_unused_link(steam, tmp_path):
    pytest.importorskip("PyQt6.QtWidgets")
    from PyQt6.QtWidgets import QApplication

    from nadare.gui import steam_wiring

    app = QApplication.instance() or QApplication([])
    root, _runner = steam
    (root / "steamapps").mkdir()
    config = steamglue.config_vdf(root)
    config.write_text(_config(("5019180", TOOL)))

    def fake_restore(app_id, previous, *, config_path, **kwargs):
        Path(config_path).write_text(_config(("5019180", "proton_9")))
        return steamrun.ActionResult(True, "restored")

    dialog = steam_wiring.build_dialog(
        None, "5019180", root=root, vulkan_version_fn=lambda: None, async_plan=False,
        confirm_fn=lambda title, text: True, run=lambda argv: 0, restore_fn=fake_restore,
    )
    try:
        assert dialog._undo_fn("PREVIOUS").ok
        assert not (steamglue.tool_dir(root) / TOOL).is_symlink()
    finally:
        dialog.deleteLater()
        app.processEvents()
