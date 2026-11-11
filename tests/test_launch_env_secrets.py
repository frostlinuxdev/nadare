# SPDX-License-Identifier: GPL-3.0-or-later
"""#811: script and Steam launches do not hand credential-looking variables to what they start.

Wine/Proton games already went through ``process.sanitize_game_env``; a script game and a Steam
client that Nadare starts got Nadare's whole environment (``AWS_*``, ``*_TOKEN`` ...).
"""

from __future__ import annotations

from _backend_helpers import make_context, write_exe

from nadare.backends import script as script_mod
from nadare.backends import steam as steam_mod
from nadare.backends.script import ScriptBackend
from nadare.backends.steam import SteamBackend


def _capture(monkeypatch, module):
    seen: dict = {}

    def fake_stream(argv, env, **kwargs):
        seen["env"] = dict(env)
        return 0

    monkeypatch.setattr(module, "stream", fake_stream)
    return seen


def test_a_script_launch_drops_secrets_but_keeps_what_the_profile_sets(tmp_path, monkeypatch):
    write_exe(tmp_path / "run.sh", "#!/bin/sh\necho hi\n")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws")
    monkeypatch.setenv("GITHUB_TOKEN", "gh")
    monkeypatch.setenv("PATH", "/home/u/.pyenv/shims:/usr/bin:/bin")
    seen = _capture(monkeypatch, script_mod)
    ctx = make_context(tmp_path, "script", {
        "launch_exe": "run.sh", "install_dir": str(tmp_path),
        "extra_env": {"MY_GAME_TOKEN": "mine", "DXVK_HUD": "1"},
    })
    ScriptBackend(ctx).launch()
    env = seen["env"]
    assert "AWS_SECRET_ACCESS_KEY" not in env and "GITHUB_TOKEN" not in env
    assert env["MY_GAME_TOKEN"] == "mine" and env["DXVK_HUD"] == "1"  # the user's own choice
    assert env["PATH"].startswith("/home/u/.pyenv/shims")  # a user script may need its shims


def test_a_steam_launch_drops_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("CZN_REFRESH", "x")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("HOME", str(tmp_path))
    seen = _capture(monkeypatch, steam_mod)
    binary = write_exe(tmp_path / "steam", "#!/bin/sh\n")
    ctx = make_context(tmp_path, "steam", {"steam_app_id": "12345", "steam_binary": str(binary)})
    SteamBackend(ctx).launch()
    env = seen["env"]
    assert "CZN_REFRESH" not in env and "OPENAI_API_KEY" not in env
    assert env.get("HOME") == str(tmp_path)
