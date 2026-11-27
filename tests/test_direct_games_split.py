# SPDX-License-Identifier: GPL-3.0-or-later
"""The per-game module preserves registry identity and owns its runtime globals."""

from pathlib import Path

from _backend_helpers import make_context

from nadare.backends import direct, webview2_signin
from nadare.core import directkinds, webview2
from nadare.core.backends import get

#: The modules that hold the game backends split out of direct.py (#1601).
GAME_MODULES = (
    "direct_bpsr", "direct_games", "direct_r1999", "direct_strinova", "direct_tof", "direct_wuwa",
)


def test_direct_game_registry_reexports_are_identical():
    import importlib

    for kind, (name, *_rest) in directkinds._DIRECT.items():
        home = importlib.import_module(f"nadare.backends.{direct._GAME_EXPORTS[name]}")
        cls = getattr(home, name)
        assert getattr(direct, name) is cls
        assert get(kind) is cls
        assert directkinds.BUILTIN_BACKENDS[kind] == f"nadare.backends.direct:{name}"
        assert cls.__module__ == home.__name__
    assert direct._GAME_EXPORTS["WutheringWavesBackend"] == "direct_wuwa"


def test_direct_base_module_is_under_1200_lines():
    assert len(Path(direct.__file__).read_text().splitlines()) < 1200


def test_the_new_game_modules_add_no_size_debt():
    """#1601: a new file must not need a baseline entry, so each stays at or under the limit."""
    import importlib

    for name in GAME_MODULES:
        mod = importlib.import_module(f"nadare.backends.{name}")
        assert len(Path(mod.__file__).read_text().splitlines()) <= 800, name


def test_the_non_class_exports_still_come_from_direct():
    from nadare.backends import direct_games

    assert direct.DNA_ACCOUNT_NOTICE is direct_games.DNA_ACCOUNT_NOTICE
    assert direct.game_subdir("X") == Path("drive_c/Games") / "X"


def test_haoplay_reads_stream_from_direct_games(tmp_path, monkeypatch):
    from nadare.backends import wine

    backend = direct.Gfl2HaoplayBackend(make_context(tmp_path, "direct-gfl2-haoplay", {}))
    captured = {}
    monkeypatch.setattr(webview2, "ensure", lambda prefix, **kwargs: captured.update(kwargs))
    monkeypatch.setattr(backend, "_prefix_command", lambda args, env: (["runner", *args], env))
    calls = []
    monkeypatch.setattr(wine, "stream", lambda *args, **kwargs: 0)
    monkeypatch.setattr(
        webview2_signin, "stream", lambda *args, **kwargs: calls.append((args, kwargs)) or 73
    )
    backend._ensure_webview2()

    assert captured["run_in_prefix"](["installer.exe"]) == 73
    assert calls == [
        (
            (["runner", "installer.exe"], {"PROTON_ENABLE_WAYLAND": "0"}),
            {"on_line": backend._on_line, "cancel": backend.ctx.cancel, "cwd": backend.prefix()},
        )
    ]


def test_both_module_import_orders_work():
    """Whatever is imported first, vars(direct) holds every export (Neon's review)."""
    import subprocess
    import sys

    check = (
        "from nadare.backends import direct; "
        "names = direct._GAME_EXPORTS; "
        "missing = [n for n in names if n not in vars(direct)]; "
        "assert not missing, missing; "
        "from nadare.backends import direct_bpsr, direct_games, direct_r1999, direct_strinova, "
        "direct_tof, direct_wuwa; "
        "mods = {'direct_games': direct_games, 'direct_wuwa': direct_wuwa, "
        "'direct_tof': direct_tof, 'direct_r1999': direct_r1999, "
        "'direct_strinova': direct_strinova, 'direct_bpsr': direct_bpsr}; "
        "assert all(vars(direct)[n] is getattr(mods[h], n) for n, h in names.items()); "
        "assert direct.WutheringWavesBackend is direct_wuwa.WutheringWavesBackend; "
        "assert 'WutheringWavesBackend' in dir(direct)"
    )
    for first in ("direct", *GAME_MODULES):
        result = subprocess.run(
            [sys.executable, "-c", f"import nadare.backends.{first}; {check}"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (first, result.stderr)
