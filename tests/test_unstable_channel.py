# SPDX-License-Identifier: GPL-3.0-or-later
"""An unstable build never touches the stable install's data (unstable channel).

Research work (new stores, new games, per-game fixes still being tried) ships first in an
"unstable" AppImage. It must be safe to run next to the normal Nadare: its own config, data,
state and cache folders, its own sign-ins in the keyring, its own single-instance socket, a
title that says what it is, and no update check (an update would replace it with the stable
build, or offer the stable build to someone testing unstable). The stable build is unchanged:
every name stays ``nadare``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nadare.core import channel, paths, secrets


@pytest.fixture
def no_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(channel, "MARKER", tmp_path / "no-such-CHANNEL")


def _unstable(monkeypatch):
    monkeypatch.setenv("NADARE_CHANNEL", "unstable")


# ------------------------------------------------------------------ which channel
def test_default_is_stable(no_marker):
    assert channel.current() == channel.STABLE
    assert channel.is_unstable() is False


def test_env_selects_unstable(no_marker, monkeypatch):
    _unstable(monkeypatch)
    assert channel.current() == channel.UNSTABLE


def test_the_build_marker_selects_unstable(tmp_path, monkeypatch):
    marker = tmp_path / "CHANNEL"
    marker.write_text("unstable\n")
    monkeypatch.setattr(channel, "MARKER", marker)
    assert channel.is_unstable() is True


def test_env_stable_beats_the_marker(tmp_path, monkeypatch):
    marker = tmp_path / "CHANNEL"
    marker.write_text("unstable\n")
    monkeypatch.setattr(channel, "MARKER", marker)
    monkeypatch.setenv("NADARE_CHANNEL", "stable")
    assert channel.is_unstable() is False


@pytest.mark.parametrize("value", ["", "beta", "UNSTABLE ", "unstable;rm", "nightly"])
def test_unknown_values_mean_stable(no_marker, monkeypatch, tmp_path, value):
    monkeypatch.setenv("NADARE_CHANNEL", value)
    assert channel.current() == channel.STABLE
    marker = tmp_path / "CHANNEL"
    marker.write_text(value)
    monkeypatch.setattr(channel, "MARKER", marker)
    monkeypatch.delenv("NADARE_CHANNEL")
    assert channel.current() == channel.STABLE


def test_an_unreadable_marker_means_stable(tmp_path, monkeypatch):
    folder = tmp_path / "CHANNEL"
    folder.mkdir()  # reading a folder raises
    monkeypatch.setattr(channel, "MARKER", folder)
    assert channel.current() == channel.STABLE


# ------------------------------------------------------------------ separate data
def test_stable_folders_keep_their_names(no_marker):
    for folder in (paths.config_dir(), paths.data_dir(), paths.state_dir(), paths.cache_dir()):
        assert folder.name == "nadare"
    assert paths.app_name() == "nadare"


def test_unstable_folders_are_separate(no_marker, monkeypatch):
    stable = [paths.config_dir(), paths.data_dir(), paths.state_dir(), paths.cache_dir()]
    _unstable(monkeypatch)
    unstable = [paths.config_dir(), paths.data_dir(), paths.state_dir(), paths.cache_dir()]
    for before, after in zip(stable, unstable, strict=True):
        assert after.name == "nadare-unstable"
        assert after.parent == before.parent
        assert not str(after).startswith(str(before) + "/")
    assert paths.config_file().parent.name == "nadare-unstable"
    assert paths.runners_dir().parent.name == "nadare-unstable"


def test_unstable_sign_ins_use_their_own_keyring_service(no_marker, monkeypatch):
    """A token refreshed by one build must not invalidate the other's (rotating tokens)."""
    stable = secrets.service_name()
    assert stable == "nadare-launcher"
    _unstable(monkeypatch)
    assert secrets.service_name() == "nadare-launcher-unstable"


def test_keyring_calls_use_the_channel_service(no_marker, monkeypatch):
    _unstable(monkeypatch)
    seen: list[str] = []

    class FakeKeyring:
        def get_password(self, service, account):
            seen.append(service)
            return None

        def set_password(self, service, account, token):
            seen.append(service)

        def delete_password(self, service, account):
            seen.append(service)

    store = secrets.SecretStore.__new__(secrets.SecretStore)
    store._keyring = FakeKeyring()
    store._kget("acct")
    store._kset("acct", "value")
    store._kdel("acct")
    assert seen and set(seen) == {"nadare-launcher-unstable"}


def test_single_instance_sockets_differ(no_marker, monkeypatch):
    pytest.importorskip("PyQt6")
    from nadare.gui import single_instance

    stable = single_instance.server_name()
    _unstable(monkeypatch)
    unstable = single_instance.server_name()
    assert stable != unstable
    assert "nadare-unstable" in Path(unstable).name


# ------------------------------------------------------------------ what the user sees
def test_title(no_marker, monkeypatch):
    assert channel.title("Nadare") == "Nadare"
    _unstable(monkeypatch)
    assert channel.title("Nadare") == "Nadare (unstable)"


def test_unstable_never_checks_for_launcher_updates(no_marker, monkeypatch):
    pytest.importorskip("PyQt6")
    from nadare.core import updatecheck
    from nadare.gui.main_window_parts import selfupdate as gui_selfupdate

    _unstable(monkeypatch)
    monkeypatch.setattr(updatecheck, "managed_updates", lambda: False)
    started: list[object] = []

    class NoThread:
        def __init__(self, *a, **k):
            started.append(k.get("name"))

        def start(self):
            pass

    monkeypatch.setattr(gui_selfupdate.threading, "Thread", NoThread)

    class Window(gui_selfupdate.SelfUpdateMixin):
        class config:  # noqa: N801 - stands in for the Config object
            data = {"check_updates": True}

    Window()._start_update_check()
    assert started == []


def test_stable_still_checks_for_launcher_updates(no_marker, monkeypatch):
    pytest.importorskip("PyQt6")
    from nadare.core import updatecheck
    from nadare.gui.main_window_parts import selfupdate as gui_selfupdate

    monkeypatch.setattr(updatecheck, "managed_updates", lambda: False)
    started: list[object] = []

    class NoThread:
        def __init__(self, *a, **k):
            started.append(k.get("name"))

        def start(self):
            pass

    monkeypatch.setattr(gui_selfupdate.threading, "Thread", NoThread)

    class Window(gui_selfupdate.SelfUpdateMixin):
        class config:  # noqa: N801
            data = {"check_updates": True}

    Window()._start_update_check()
    assert started == ["nadare-update-check"]


def test_unstable_cannot_replace_itself(no_marker, monkeypatch, tmp_path):
    """Belt and braces: even a stray update offer cannot swap the unstable file."""
    from nadare.core import selfupdate

    image = tmp_path / "Nadare-unstable-x86_64.AppImage"
    image.write_text("")
    env = {"APPIMAGE": str(image)}
    monkeypatch.setattr(selfupdate.platform, "machine", lambda: "x86_64")
    _unstable(monkeypatch)
    assert selfupdate.why_not_replaceable(env) == selfupdate.MANAGED


# ------------------------------------------------------------------ the build
ROOT = Path(__file__).resolve().parents[1]


def _build(tmp_path, **env):
    import os
    import subprocess
    import sys

    full = dict(os.environ)
    full.update(
        PYTHON=sys.executable,
        PYTHONPATH=str(ROOT / "src"),
        NADARE_ALLOW_NO_KEYRING="1",
        OUTPUT_DIR=str(tmp_path / "out"),
        **env,
    )
    return subprocess.run(
        ["bash", str(ROOT / "scripts" / "build_appimage.sh")],
        cwd=ROOT, env=full, capture_output=True, text=True, timeout=60,
    )


def test_build_refuses_an_unknown_channel(tmp_path):
    result = _build(tmp_path, NADARE_CHANNEL="nightly")
    assert result.returncode != 0
    assert "NADARE_CHANNEL must be stable or unstable" in result.stderr
    assert "PyInstaller" not in result.stdout


def test_unstable_build_refuses_update_information(tmp_path):
    """Gear Lever / AppImageUpdate would replace the unstable file with the stable release."""
    result = _build(
        tmp_path, NADARE_CHANNEL="unstable", NADARE_UPDATE_INFO="zsync|https://example.invalid/x"
    )
    assert result.returncode != 0
    assert "carries no update information" in result.stderr
    assert not (ROOT / "src" / "nadare" / "assets" / "CHANNEL").exists()


def test_build_script_names_and_marks_the_unstable_image():
    script = (ROOT / "scripts" / "build_appimage.sh").read_text()
    assert 'image_stem="Nadare-unstable"' in script
    assert "printf 'unstable\\n' > \"$channel_marker\"" in script
    # Removed again right after PyInstaller copied it, so the source tree stays stable.
    after = script.split("-m PyInstaller --clean -y packaging/nadare.spec", 1)[1]
    assert after.lstrip().startswith('rm -f "$channel_marker"')


def test_marker_is_never_committed():
    ignore = (ROOT / ".gitignore").read_text().splitlines()
    assert "src/nadare/assets/CHANNEL" in ignore
    assert not (ROOT / "src" / "nadare" / "assets" / "CHANNEL").exists()


# ------------------------------------------------------------------ bug reports say which build
def test_version_and_report_name_the_unstable_build(no_marker, monkeypatch, capsys):
    from nadare import __version__, app
    from nadare.core import report

    with pytest.raises(SystemExit):
        app.build_parser().parse_args(["--version"])
    assert capsys.readouterr().out.strip() == f"nadare {__version__}"
    assert f"Nadare: {__version__}\n" in report.build_report({})

    _unstable(monkeypatch)
    with pytest.raises(SystemExit):
        app.build_parser().parse_args(["--version"])
    assert capsys.readouterr().out.strip() == f"nadare {__version__} (unstable)"
    assert f"Nadare: {__version__} (unstable)\n" in report.build_report({})
