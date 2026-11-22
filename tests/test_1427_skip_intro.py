# SPDX-License-Identifier: GPL-3.0-or-later
"""#1427: the optional per-game "Skip intro videos" switch."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from _backend_helpers import make_context

from nadare.backends import direct
from nadare.core.backends import get

HINT = (
    "Starts the game without its logo videos. Turn it off if the game misbehaves after an update."
)

CASES = (
    ("direct-wuwa", direct.WutheringWavesBackend, "-SkipSplash"),
    ("direct-mongil", direct.MongilBackend, "-nosplash"),
)
OTHER_KINDS = ("direct-genshin", "direct-snowbreak", "direct-bd2", "wine")


def _backend(cls, kind, tmp_path, profile=None):
    values = {"prefix": str(tmp_path / "pfx")}
    values.update(profile or {})
    return cls(make_context(tmp_path, kind, values))


def _wuwa(tmp_path, profile=None):
    return _backend(direct.WutheringWavesBackend, "direct-wuwa", tmp_path, profile)


def _mongil(tmp_path, profile=None):
    return _backend(direct.MongilBackend, "direct-mongil", tmp_path, profile)


def _options(kind):
    return {option.key: option for option in get(kind).options}


# ---------------------------------------------------------------- the option
@pytest.mark.parametrize(("kind", "_cls", "_arg"), CASES)
def test_the_option_is_a_bool_off_by_default_with_the_hint(kind, _cls, _arg):
    option = _options(kind)["skip_intro"]
    assert option.kind == "bool"
    assert option.default is False
    assert option.label == "Skip intro videos"
    assert option.help == HINT


def test_the_table_names_only_the_two_games():
    assert direct.SKIP_INTRO_ARGS == {"wuwa": ("-SkipSplash",), "mongil": ("-nosplash",)}


@pytest.mark.parametrize("kind", OTHER_KINDS)
def test_no_other_game_offers_the_option(kind):
    assert "skip_intro" not in _options(kind)


def test_another_game_gets_nothing_even_when_the_profile_says_so(tmp_path):
    backend = _backend(direct.GenshinBackend, "direct-genshin", tmp_path, {"skip_intro": True})
    argv = backend._direct_argv(Path("/game.exe"))
    assert argv == ["/game.exe"]


# ------------------------------------------------------------------ the args
def test_off_by_default_adds_nothing(tmp_path):
    assert _wuwa(tmp_path)._direct_argv(Path("/game.exe")) == [
        "/game.exe",
        "-dx12",
        "-krqlv=hd",
    ]
    assert _mongil(tmp_path)._direct_argv(Path("/game.exe")) == ["/game.exe"]


@pytest.mark.parametrize(("kind", "_cls", "arg"), CASES)
def test_on_adds_the_right_arg_for_each_game(kind, _cls, arg, tmp_path):
    profile = {"prefix": str(tmp_path / "pfx"), "skip_intro": True}
    backend = _cls(make_context(tmp_path, kind, profile))
    argv = backend._direct_argv(Path("/game.exe"))
    assert arg in argv
    assert argv.count(arg) == 1


def test_wuwa_order_is_defaults_then_the_flag_then_the_user_args(tmp_path):
    # No user args: the game's own launcher-default args come first.
    assert _wuwa(tmp_path, {"skip_intro": True})._direct_argv(Path("/game.exe")) == [
        "/game.exe",
        "-dx12",
        "-krqlv=hd",
        "-SkipSplash",
    ]
    # A profile launch_args keeps control of the defaults but not of the splash flag.
    assert _wuwa(tmp_path / "b", {"skip_intro": True, "launch_args": "-dx11"})._direct_argv(
        Path("/game.exe")
    ) == ["/game.exe", "-SkipSplash", "-dx11"]


def test_mongil_order_is_the_flag_then_the_user_args(tmp_path):
    backend = _mongil(tmp_path, {"skip_intro": True, "launch_args": "-windowed"})
    assert backend._direct_argv(Path("/game.exe")) == [
        "/game.exe",
        "-nosplash",
        "-windowed",
    ]


@pytest.mark.parametrize(
    ("kind", "cls", "arg"),
    (
        ("direct-wuwa", direct.WutheringWavesBackend, "-SkipSplash"),
        ("direct-mongil", direct.MongilBackend, "-nosplash"),
    ),
)
def test_an_arg_the_user_already_typed_is_not_repeated(kind, cls, arg, tmp_path):
    backend = _backend(cls, kind, tmp_path, {"skip_intro": True, "launch_args": f"{arg} -dx11"})
    argv = backend._direct_argv(Path("/game.exe"))
    assert argv.count(arg) == 1
    assert argv == ["/game.exe", arg, "-dx11"]


# --------------------------------------------------------------------- GUI
@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PyQt6.QtWidgets")
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _page(tmp_path, kind):
    from nadare.core.config import Config
    from nadare.gui.settings import SettingsPage

    config = Config.load(tmp_path / f"{kind}.json")
    config.add_profile("g", backend=kind, title="G")
    page = SettingsPage()
    page.load(config, "g")
    page.show()
    return page


def test_settings_show_the_checkbox_only_for_those_games(tmp_path, qapp):
    try:
        for kind, _cls, _arg in CASES:
            page = _page(tmp_path / kind, kind)
            try:
                box = page.option_widget("skip_intro")
                assert box is not None, kind
                assert not box.isChecked(), kind
            finally:
                page.deleteLater()
                qapp.processEvents()
        for kind in OTHER_KINDS:
            page = _page(tmp_path / f"other-{kind}", kind)
            try:
                assert page.option_widget("skip_intro") is None, kind
            finally:
                page.deleteLater()
                qapp.processEvents()
    finally:
        qapp.processEvents()


def test_screenshot_of_the_settings_dialog(tmp_path, qapp):
    shots = Path(os.environ.get("NADARE_SHOT_DIR", tmp_path))
    shots.mkdir(parents=True, exist_ok=True)
    for kind in ("direct-wuwa", "direct-mongil", "direct-genshin"):
        page = _page(tmp_path / f"shot-{kind}", kind)
        try:
            page.resize(900, 760)
            page.tabs.setCurrentIndex(1)  # the "Game" tab holds Game options
            qapp.processEvents()
            box = page.option_widget("skip_intro") or page.option_widget("prefix")
            if box is not None:
                page.tabs.widget(1).ensureWidgetVisible(box, 40, 80)
            qapp.processEvents()
            shot = shots / f"settings-{kind}.png"
            page.grab().save(str(shot))
            assert shot.stat().st_size > 0
            if box is not None and kind in ("direct-wuwa", "direct-mongil"):
                box.setChecked(True)
                qapp.processEvents()
                on_shot = shots / f"settings-{kind}-on.png"
                page.grab().save(str(on_shot))
                assert on_shot.stat().st_size > 0
        finally:
            page.deleteLater()
            qapp.processEvents()


@pytest.mark.parametrize("value", [False, None, "false", "off", "invalid", 0])
def test_disabled_profile_values_do_not_enable_skip(tmp_path, value):
    assert "-SkipSplash" not in _wuwa(tmp_path, {"skip_intro": value})._direct_argv(
        Path("/game.exe")
    )


def test_legacy_extra_is_read_but_top_level_false_wins(tmp_path):
    backend = _wuwa(tmp_path, {"extra": {"skip_intro": True}})
    assert "-SkipSplash" in backend._direct_argv(Path("/game.exe"))
    backend.ctx.profile["skip_intro"] = False
    assert "-SkipSplash" not in backend._direct_argv(Path("/game.exe"))


def test_existing_wuwa_launch_hook_still_returns_defaults(tmp_path):
    assert _wuwa(tmp_path).launch_extra_args() == ["-dx12", "-krqlv=hd"]


@pytest.mark.parametrize("as_list", [False, True])
def test_quoted_or_list_args_keep_paths_and_exact_duplicate_detection(tmp_path, as_list):
    args = ["-nosplash", "-output", "folder with spaces", "-nosplash-extra"]
    backend = _mongil(
        tmp_path,
        {
            "skip_intro": True,
            "launch_args": args
            if as_list
            else '-nosplash -output "folder with spaces" -nosplash-extra',
        },
    )
    assert backend._direct_argv(Path("/game folder/game.exe")) == [
        "/game folder/game.exe",
        *args,
    ]
    backend.ctx.profile["launch_args"] = "-nosplash-extra"
    assert backend._direct_argv(Path("/game.exe")) == [
        "/game.exe",
        "-nosplash",
        "-nosplash-extra",
    ]


def test_mongil_keeps_extra_args_after_user_args(tmp_path, monkeypatch):
    # Stand-in for the existing login hook; no live session or keyring required.
    monkeypatch.setattr(direct.MongilBackend, "launch_extra_args", lambda self: ["tail"])
    backend = _mongil(tmp_path, {"skip_intro": True, "launch_args": "-windowed"})
    assert backend._direct_argv(Path("/game.exe")) == [
        "/game.exe",
        "-nosplash",
        "-windowed",
        "tail",
    ]
