# SPDX-License-Identifier: GPL-3.0-or-later
"""CLI messages that must not mislead: removal, Steam, status, doctor and profiles."""

from __future__ import annotations

from pathlib import Path

import pytest

from nadare import app
from nadare.core import graphics, hostlibs, process, runners, shortcuts
from nadare.core.backends import BackendError
from nadare.core.config import Config


def _config(tmp_path: Path, *profiles: tuple[str, dict]) -> tuple[Config, Path]:
    path = tmp_path / "config.json"
    cfg = Config.load(path)
    for name, values in profiles:
        cfg.add_profile(name, **values)
    cfg.save()
    return cfg, path


def test_remove_missing_install_dir_keeps_the_profile(tmp_path, capsys) -> None:
    _, path = _config(tmp_path, ("gone", {"backend": "wine"}))

    code = app.run_cli(["-c", str(path), "remove", "-p", "gone", "--yes", "--delete-install-dir"])

    out = capsys.readouterr().out
    assert code != 0
    assert "no configured install folder was found" in out
    assert "it was kept" in out
    assert "removed gone" not in out
    assert "gone" in Config.load(path).profiles


def test_remove_missing_prefix_keeps_profile_and_says_nothing_was_deleted(
    tmp_path, capsys
) -> None:
    _, path = _config(tmp_path, ("gone", {"backend": "wine"}))

    code = app.run_cli(["-c", str(path), "remove", "-p", "gone", "--yes", "--delete-prefix"])

    out = capsys.readouterr().out
    assert code != 0
    assert "no configured Wine prefix was found" in out
    assert "gone" in Config.load(path).profiles


class _NotInstalledBackend:
    def requires_notice_ack(self) -> bool:
        return False

    def launch(self) -> int:
        raise BackendError("the game is not installed", 1)

    def verify(self) -> dict:
        return {"verified": False, "message": "integrity check failed: prefix, game"}


def test_launch_and_verify_on_an_uninstalled_game_name_the_install_command(
    tmp_path, monkeypatch, capsys
) -> None:
    _, path = _config(tmp_path, ("game", {"backend": "wine"}))
    monkeypatch.setattr(app, "create_backend", lambda *a, **k: _NotInstalledBackend())

    assert app.run_cli(["-c", str(path), "launch", "-p", "game"]) == 1
    assert "nadare install -p game" in capsys.readouterr().err

    assert app.run_cli(["-c", str(path), "verify", "-p", "game"]) == 1
    out = capsys.readouterr().out
    assert "integrity check failed" in out
    assert "nadare install -p game" in out


def _steam_vdf(tmp_path: Path) -> Path:
    directory = Path.home() / ".local" / "share" / "Steam" / "userdata" / "42" / "config"
    directory.mkdir(parents=True)
    return directory / "shortcuts.vdf"


def test_shortcut_list_and_refresh_include_steam_entries(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(shortcuts, "steam_running", lambda: False)
    monkeypatch.setattr(app, "_desktop_dir", lambda: tmp_path / "Desktop")
    cfg, path = _config(tmp_path, ("game", {"backend": "script", "title": "Old Title"}))
    vdf = _steam_vdf(tmp_path)
    shortcuts.add_steam_shortcut(vdf, "game", "Old Title", ["/opt/nadare"])

    assert app.run_cli(["-c", str(path), "shortcut", "list"]) == 0
    out = capsys.readouterr().out
    assert str(vdf) in out
    assert "game" in out and "Steam: Old Title" in out

    cfg = Config.load(path)
    cfg.profiles["game"]["title"] = "New Title"
    cfg.save()
    assert app.run_cli(["-c", str(path), "shortcut", "refresh"]) == 0
    assert shortcuts.vdf_loads(vdf.read_bytes(), strict=True)[0]["AppName"] == "New Title"


def test_steam_add_without_a_steam_shortcuts_file_warns_and_fails(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(shortcuts, "find_steam_shortcut_files", lambda: [])
    monkeypatch.setattr(app, "_desktop_dir", lambda: tmp_path / "Desktop")
    _, path = _config(tmp_path, ("game", {"backend": "script"}))

    code = app.run_cli(["-c", str(path), "shortcut", "add", "-p", "game", "--steam"])

    err = capsys.readouterr().err
    assert code != 0
    assert "no Steam shortcut files found" in err
    assert "[warn]" in err


@pytest.mark.parametrize("kind", ["script", "steam"])
def test_misconfigured_profile_status_is_an_error_with_nonzero_exit(
    tmp_path, kind, capsys
) -> None:
    _, path = _config(tmp_path, ("broken", {"backend": kind}))

    code = app.run_cli(["-c", str(path), "status", "-p", "broken"])

    out = capsys.readouterr().out
    assert code == 1
    assert "broken: error" in out
    assert "[error]" in out and "[done]" not in out


def test_doctor_gives_a_next_step_for_vulkan_and_missing_runners(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(graphics, "vulkan_info", lambda **k: graphics.VulkanInfo(False))
    monkeypatch.setattr(graphics, "dxvk_ok", lambda: False)
    monkeypatch.setattr(hostlibs, "distro_family", lambda: "debian")
    monkeypatch.setattr(process, "which", lambda name, **k: "/usr/bin/umu-run")
    monkeypatch.setattr(runners, "all_runners", list)

    assert app.run_cli(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "fix:" in out and "mesa-vulkan-drivers:i386" in out
    assert "runners: none found" in out
    assert "nadare install" in out and "320 MB" in out


def test_quiet_and_verbose_work_after_the_command_or_nested_action() -> None:
    parser = app.build_parser()
    assert parser.parse_args(["-q", "fetch-art"]).quiet is True
    assert parser.parse_args(["fetch-art", "-q"]).quiet is True
    assert parser.parse_args(["shortcut", "list", "-q"]).quiet is True
    assert parser.parse_args(["shortcut", "-q", "list"]).quiet is True
    assert parser.parse_args(["status", "-p", "game", "-v"]).verbose is True
    assert parser.parse_args(["-v", "status", "-p", "game"]).verbose is True


def test_profile_option_is_not_offered_by_commands_that_ignore_it() -> None:
    parser = app.build_parser()
    for command in ("list", "doctor"):
        with pytest.raises(SystemExit):
            parser.parse_args([command, "-p", "nope"])
    # `gui -p` is valid: it opens that profile's page.
    assert parser.parse_args(["gui", "-p", "game"]).profile == "game"


def test_shortcut_without_a_profile_does_not_print_an_empty_name(
    tmp_path, capsys
) -> None:
    _, path = _config(tmp_path)

    assert app.run_cli(["-c", str(path), "shortcut", "add"]) == 1
    err = capsys.readouterr().err
    assert "no profile given; use -p <profile>" in err
    assert "no such profile: \n" not in err


def test_explicit_missing_config_is_reported_instead_of_looking_empty(
    tmp_path, capsys
) -> None:
    missing = tmp_path / "missing.json"

    assert app.run_cli(["-c", str(missing), "list"]) == 2

    err = capsys.readouterr().err
    assert "config file not found" in err and str(missing) in err
